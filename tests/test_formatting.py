from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from html import escape
from pathlib import Path
from xml.etree import ElementTree

import pytest

from mcc_bot.catalog import CardCatalog, MoneyAmount, RewardCap, RewardComponent, RewardLimit
from mcc_bot.descriptions import DescriptionCatalog
from mcc_bot.formatting import (
    SAFE_MESSAGE_LENGTH,
    format_limits,
    format_match_pages,
    format_matches,
    format_moneyback,
    split_message,
)


def test_format_matches_renders_only_cards_and_rewards(catalog_path: Path, tmp_path: Path) -> None:
    matches = CardCatalog.from_file(catalog_path).lookup("5411")
    descriptions_path = tmp_path / "descriptions.json"
    descriptions_path.write_text('{"5411": "Продуктовые магазины"}', encoding="utf-8")
    descriptions = DescriptionCatalog.from_file(descriptions_path)

    rendered = format_matches("5411", matches, descriptions)

    assert rendered.startswith("🛒 MCC 5411 — Продуктовые магазины")
    assert "1. 🅱️ Beta Card — 5% (4,61%)" in rendered
    assert "3. 🅰️ Alpha Card — 2,5% (2,44%)" in rendered
    assert "Beta Bank" not in rendered
    assert "Alpha Bank" not in rendered
    assert "мин. платёж" not in rendered
    assert "макс. в месяц" not in rendered
    assert "after tax" not in rendered


def test_format_limits_renders_all_cards_and_program_terms(catalog_path: Path) -> None:
    rendered = format_limits(CardCatalog.from_file(catalog_path).cards)

    assert rendered.startswith("📊 Лимиты по картам\n\n")
    assert "1. 🅰️ Alpha Card — 💵 мин. платёж не указан · макс. в месяц не указан" in rendered
    assert "2. 🅱️ Beta Card — 💵 мин. платёж не указан · макс. в месяц не указан" in rendered
    assert "3. 🌀 Gamma Card — 💵 мин. платёж не указан · макс. в месяц не указан" in rendered
    assert "Alpha Bank" not in rendered


def test_format_matches_reports_no_cards_in_russian() -> None:
    assert format_matches("5411", ()) == (
        "🧾 MCC 5411 — описание не найдено\n\n❌ Доступных карт нет."
    )


def test_format_moneyback_tax_and_points_exemption(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "cards": [
                    {
                        "id": "cash",
                        "name": "Cash",
                        "emoji": "💳",
                        "reward_programs": [
                            {
                                "kind": "cash",
                                "tax_exempt": False,
                                "offers": [{"mcc": "5411", "value": 3}],
                            }
                        ],
                    },
                    {
                        "id": "points",
                        "name": "Points",
                        "emoji": "⭐",
                        "reward_programs": [
                            {
                                "kind": "points",
                                "tax_exempt": True,
                                "offers": [{"mcc": "5411", "value": 3}],
                            }
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    matches = CardCatalog.from_file(path).lookup("5411")
    by_kind = {match.components[0].kind: match for match in matches}
    assert format_moneyback(by_kind["points"]) == "3% баллами"
    assert format_moneyback(by_kind["cash"]) == "3% (2,87%)"


def test_format_matches_does_not_render_internal_conditions(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "cards": [
                    {
                        "id": "card",
                        "name": "Card",
                        "emoji": "💳",
                        "condition": {"kind": "max_connected_categories", "count": 3},
                        "reward_programs": [
                            {
                                "kind": "points",
                                "tax_exempt": False,
                                "offers": [{"mcc": "5411", "value": 1}],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rendered = format_matches("5411", CardCatalog.from_file(path).lookup("5411"))
    assert "подключённых категорий" not in rendered
    assert "Note" not in rendered
    assert "1% баллами" in rendered


def test_split_message_prefers_line_boundaries() -> None:
    chunks = split_message("header\n" + "x" * 10 + "\nfooter", max_length=12)
    assert chunks == ("header", "xxxxxxxxxx", "footer")


def test_default_format_remains_exact_and_single_page_compact_is_identical(catalog_path) -> None:
    matches = CardCatalog.from_file(catalog_path).lookup("5411")
    descriptions = {"5411": "Продуктовые магазины"}
    compact = format_matches("5411", matches, descriptions)

    assert compact == (
        "🛒 MCC 5411 — Продуктовые магазины\n\n"
        "1. 🅱️ Beta Card — 5% (4,61%)\n"
        "2. 🌀 Gamma Card — 5% (4,61%)\n"
        "3. 🅰️ Alpha Card — 2,5% (2,44%)"
    )
    pages = format_match_pages("5411", matches, descriptions)
    assert len(pages) == 1
    assert pages[0].compact == compact
    assert pages[0].expanded == format_matches("5411", matches, descriptions, details=True)


def test_details_show_unknown_terms_and_issuer_explicitly(catalog_path) -> None:
    matches = CardCatalog.from_file(catalog_path).lookup("5411")
    rendered = format_matches("5411", matches, details=True)

    assert "   Beta Bank\n   Мин. платёж не указан · макс. в месяц не указан" in rendered
    assert "   Банк не указан\n   Мин. платёж не указан · макс. в месяц не указан" in rendered
    assert "без минимума" not in rendered
    assert "без лимита" not in rendered
    assert "Банк:" not in rendered


def test_details_keep_cash_and_points_terms_separate_and_select_effective_programs(catalog_path):
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    cash = replace(
        base.card.reward_programs[0],
        id="cash",
        minimum_payment=MoneyAmount(Decimal(10), "BYN"),
        maximum_reward=RewardCap(Decimal(50), "currency", "BYN"),
    )
    points = replace(
        cash,
        id="points",
        kind="points",
        minimum_payment=MoneyAmount(Decimal(0), "BYN"),
        maximum_reward=RewardCap(Decimal(200), "points"),
    )
    match = replace(
        base,
        card=replace(base.card, reward_programs=(cash, points)),
        components=(
            RewardComponent("cash", "cash", Decimal(1), False),
            RewardComponent("points", "points", Decimal(3), True),
        ),
    )

    rendered = format_matches("5411", (match,), details=True)

    assert "1% + 3% баллами" in rendered
    assert "   Деньги: мин. платёж 10 BYN · макс. 50 BYN/мес." in rendered
    assert "   Баллы: без минимума · макс. 200 баллов/мес." in rendered

    points_only = replace(match, mcc="1234", components=(match.components[1],))
    rendered = format_matches("1234", (points_only,), details=True)
    assert "Без минимума · макс. кэшбэк 200 баллов/мес." in rendered
    assert "Деньги:" not in rendered
    assert "10 BYN" not in rendered
    assert "50 BYN" not in rendered


def test_details_show_bank_only_without_mutating_issuer(catalog_path) -> None:
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    issuer = "МТБанк / Visa / Kufar"
    match = replace(base, card=replace(base.card, issuer=issuer))

    rendered = format_matches("5411", (match,), details=True)

    assert "\n   МТБанк\n" in rendered
    assert "Visa" not in rendered
    assert "Kufar" not in rendered
    assert match.card.issuer == issuer


@pytest.mark.parametrize("monthly_known", [True, False])
def test_details_keep_weekly_and_transaction_caps_separate_from_monthly(
    catalog_path, monthly_known
):
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    program = replace(
        base.card.reward_programs[0],
        minimum_payment=MoneyAmount(Decimal(3), "BYN"),
        maximum_reward=RewardCap(Decimal(50), "currency", "BYN") if monthly_known else None,
        monthly_maximum_not_defined=not monthly_known,
    )
    match = replace(
        base,
        card=replace(
            base.card,
            reward_programs=(program,),
            reward_limits=(
                RewardLimit(Decimal(20), "currency", "week", "BYN"),
                RewardLimit(Decimal(2), "currency", "transaction", "BYN"),
            ),
        ),
    )

    rendered = format_matches("5411", (match,), details=True)

    assert "Мин. платёж 3 BYN" in rendered
    assert "макс. кэшбэк 20 BYN/неделю · макс. кэшбэк 2 BYN/операцию" in rendered
    assert ("макс. кэшбэк 50 BYN/мес." in rendered) == monthly_known
    assert "месячный лимит не установлен" not in rendered
    assert "20 BYN/мес." not in rendered


def test_details_do_not_treat_unknown_monthly_cap_as_absent_when_weekly_exists(catalog_path):
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    match = replace(
        base,
        card=replace(
            base.card, reward_limits=(RewardLimit(Decimal(20), "currency", "week", "BYN"),)
        ),
    )

    rendered = format_matches("5411", (match,), details=True)

    assert "макс. в месяц не указан · макс. кэшбэк 20 BYN/неделю" in rendered


def test_details_keep_all_monthly_cap_currency_alternatives(catalog_path) -> None:
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    program = replace(
        base.card.reward_programs[0],
        maximum_reward=RewardCap(Decimal(150), "currency", "BYN"),
        maximum_reward_alternatives=(
            RewardCap(Decimal(50), "currency", "USD"),
            RewardCap(Decimal(50), "currency", "EUR"),
        ),
    )
    match = replace(base, card=replace(base.card, reward_programs=(program,)))

    rendered = format_matches("5411", (match,), details=True)

    assert "макс. кэшбэк 150 BYN / 50 USD / 50 EUR/мес." in rendered


def test_details_preserve_finite_alternatives_when_primary_currency_is_unlimited(catalog_path):
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    program = replace(
        base.card.reward_programs[0],
        maximum_reward=RewardCap(None, "currency", "BYN"),
        maximum_reward_alternatives=(RewardCap(Decimal(50), "currency", "USD"),),
    )
    match = replace(base, card=replace(base.card, reward_programs=(program,)))

    rendered = format_matches("5411", (match,), details=True)

    assert "макс. кэшбэк без лимита BYN / 50 USD/мес." in rendered


@pytest.mark.parametrize("explicit_unlimited", [True, False])
def test_details_distinguish_explicit_no_monthly_limit_from_unknown(
    catalog_path, explicit_unlimited
):
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    program = replace(
        base.card.reward_programs[0],
        maximum_reward=RewardCap(None, "currency", "BYN") if explicit_unlimited else None,
        monthly_maximum_not_defined=not explicit_unlimited,
    )
    match = replace(base, card=replace(base.card, reward_programs=(program,)))

    rendered = format_matches("5411", (match,), details=True)

    assert "макс. в месяц не указан" not in rendered
    expected = "без месячного лимита" if explicit_unlimited else "месячный лимит не установлен"
    assert expected in rendered


def test_shared_card_limits_are_not_presented_as_independent_program_caps(catalog_path):
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    cash = replace(base.card.reward_programs[0], id="cash")
    points = replace(cash, id="points", kind="points")
    match = replace(
        base,
        card=replace(
            base.card,
            reward_programs=(cash, points),
            reward_limits=(RewardLimit(Decimal(20), "currency", "week", "BYN"),),
        ),
        components=(
            RewardComponent("cash", "cash", Decimal(1), False),
            RewardComponent("points", "points", Decimal(3), True),
        ),
    )

    rendered = format_matches("5411", (match,), details=True)

    assert "   По карте: макс. кэшбэк 20 BYN/неделю" in rendered
    assert rendered.count("20 BYN") == 1


def test_match_pages_keep_card_boundaries_global_order_and_headers(catalog_path) -> None:
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    matches = tuple(
        replace(base, card=replace(base.card, id=f"card-{i}", name=f"Card {i:03d}"))
        for i in range(100)
    )

    pages = format_match_pages("5411", matches)

    assert len(pages) > 1
    summaries = []
    for page in pages:
        assert len(page.compact.encode("utf-16-le")) // 2 <= SAFE_MESSAGE_LENGTH
        assert len(page.expanded.encode("utf-16-le")) // 2 <= SAFE_MESSAGE_LENGTH
        assert page.compact.startswith("🧾 MCC 5411 — описание не найдено\n\n")
        assert page.expanded.startswith("🧾 MCC 5411 — описание не найдено\n\n")
        compact_summaries = page.compact.splitlines()[2:]
        expanded_summaries = [block.splitlines()[0] for block in page.expanded.split("\n\n")[1:]]
        assert compact_summaries == expanded_summaries
        assert len(expanded_summaries) == page.expanded.count("   Beta Bank\n")
        summaries.extend(compact_summaries)
    assert summaries == format_matches("5411", matches).splitlines()[2:]


def test_match_pages_count_astral_emoji_at_exact_utf16_boundary(catalog_path) -> None:
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    match = replace(base, card=replace(base.card, name="💳" * 100))
    expanded = format_matches("5411", (match,), details=True)
    units = len(expanded.encode("utf-16-le")) // 2

    assert format_match_pages("5411", (match,), max_length=units)[0].expanded == expanded
    with pytest.raises(ValueError, match="exceeds"):
        format_match_pages("5411", (match,), max_length=units - 1)


@pytest.mark.parametrize("oversized_field", ["name", "issuer", "description"])
def test_match_pages_reject_an_oversized_card_or_header_without_truncation(
    catalog_path, oversized_field
) -> None:
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    descriptions = {"5411": "x" * 5000} if oversized_field == "description" else None
    if oversized_field != "description":
        base = replace(base, card=replace(base.card, **{oversized_field: "x" * 5000}))

    with pytest.raises(ValueError, match="exceeds"):
        format_match_pages("5411", (base,), descriptions)


def test_empty_match_pages_have_the_normal_no_cards_message() -> None:
    pages = format_match_pages("5411", ())

    assert len(pages) == 1
    assert pages[0].compact == pages[0].expanded == format_matches("5411", ())


@pytest.mark.parametrize("bound", [0, -1, 4097])
def test_match_pages_reject_invalid_length_bounds(bound) -> None:
    with pytest.raises(ValueError, match="Invalid"):
        format_match_pages("5411", (), max_length=bound)


def _html_text(text: str) -> str:
    """Check balanced supported markup and return its visible text."""

    root = ElementTree.fromstring(f"<message>{text}</message>")
    for element in root.iter():
        assert element.tag in {"message", "b", "i"}
        assert not element.attrib
    return "".join(root.itertext())


@pytest.mark.parametrize("details", [False, True])
def test_html_matches_style_only_header_names_and_issuers(catalog_path, details) -> None:
    matches = CardCatalog.from_file(catalog_path).lookup("5411")
    descriptions = {"5411": "Продуктовые магазины"}

    rendered = format_matches("5411", matches, descriptions, details=details, html=True)

    assert rendered.startswith("<b>🛒 MCC 5411 — Продуктовые магазины</b>\n\n")
    assert "1. 🅱️ <b>Beta Card</b> — 5️⃣% (4,61%)" in rendered
    assert "3. 🅰️ <b>Alpha Card</b> — 2️⃣,5️⃣% (2,44%)" in rendered
    assert rendered.count("<b>") == len(matches) + 1
    assert rendered.count("<i>") == (len(matches) if details else 0)
    if details:
        assert "   <i>Beta Bank</i>\n   Мин. платёж не указан" in rendered
        assert "   <i>Банк не указан</i>\n" in rendered
    assert _html_text(rendered).replace("\ufe0f\u20e3", "") == format_matches(
        "5411", matches, descriptions, details=details
    )


@pytest.mark.parametrize(
    ("gross", "reward"),
    [
        ("0.5", "0️⃣,5️⃣%"),
        ("1", "1️⃣%"),
        ("1.11", "1️⃣,1️⃣1️⃣%"),
        ("2.50", "2️⃣,5️⃣% (2,44%)"),
        ("19.8765432", "1️⃣9️⃣,8️⃣7️⃣6️⃣5️⃣4️⃣3️⃣2️⃣% (17,55%)"),
    ],
)
def test_html_matches_use_keycaps_only_for_gross_percent_digits(catalog_path, gross, reward):
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    match = replace(
        base,
        card=replace(base.card, name="Card 10", emoji="💳"),
        components=(replace(base.components[0], gross_value=Decimal(gross)),),
    )

    rendered = format_matches("5411", (match,), html=True)

    assert rendered.endswith(f"1. 💳 <b>Card 10</b> — {reward}")
    assert format_moneyback(match, emoji=True) == reward
    assert format_moneyback(match) == reward.replace("\ufe0f\u20e3", "")


def test_html_matches_keep_mixed_rewards_and_limits_in_their_original_units(catalog_path):
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    cash = replace(
        base.card.reward_programs[0],
        id="cash",
        minimum_payment=MoneyAmount(Decimal("3.5"), "BYN"),
        maximum_reward=RewardCap(Decimal(150), "currency", "BYN"),
        maximum_reward_alternatives=(RewardCap(Decimal(50), "currency", "USD"),),
    )
    points = replace(
        cash,
        id="points",
        kind="points",
        minimum_payment=MoneyAmount(Decimal(0), "BYN"),
        maximum_reward=RewardCap(Decimal(200), "points"),
        maximum_reward_alternatives=(),
    )
    match = replace(
        base,
        card=replace(
            base.card,
            reward_programs=(cash, points),
            reward_limits=(
                RewardLimit(Decimal(20), "currency", "week", "BYN"),
                RewardLimit(Decimal(2), "points", "transaction"),
            ),
        ),
        components=(
            RewardComponent("cash", "cash", Decimal("12.5"), False, "currency", "BYN"),
            RewardComponent("points", "points", Decimal("1.11"), True),
        ),
    )

    rendered = format_matches("5411", (match,), details=True, html=True)

    assert " — 12,5 BYN + 1️⃣,1️⃣1️⃣% баллами\n" in rendered
    assert "   Деньги: мин. платёж 3,5 BYN · макс. 150 BYN / 50 USD/мес." in rendered
    assert "   Баллы: без минимума · макс. 200 баллов/мес." in rendered
    assert "   По карте: макс. кэшбэк 20 BYN/неделю · макс. кэшбэк 2 баллов/операцию" in rendered
    assert _html_text(rendered).replace("\ufe0f\u20e3", "") == format_matches(
        "5411", (match,), details=True
    )
    points_only = replace(match, components=(match.components[1],))
    rendered = format_matches("5411", (points_only,), details=True, html=True)
    assert "Без минимума · макс. кэшбэк 200 баллов/мес." in rendered
    assert "3,5 BYN" not in rendered and "150 BYN" not in rendered


@pytest.mark.parametrize("descriptions_as_catalog", [False, True])
def test_html_matches_escape_all_dynamic_fields_before_adding_markup(
    catalog_path, descriptions_as_catalog
):
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    name = '<b title="card">Card & "name"</b>'
    issuer = 'Bank & <u title="issuer">'
    marker = "💳<s>&"
    currency = '<b>BYN</b> & "currency"'
    description = '<script>alert("MCC")</script> & &#x31;'
    mcc = "5411" if descriptions_as_catalog else '<5411&">'
    descriptions = {mcc: description}
    if descriptions_as_catalog:
        descriptions = DescriptionCatalog(descriptions)
    program = replace(
        base.card.reward_programs[0],
        minimum_payment=MoneyAmount(Decimal(3), currency),
        maximum_reward=RewardCap(Decimal(150), "currency", currency),
        maximum_reward_alternatives=(RewardCap(None, "currency", currency),),
    )
    match = replace(
        base,
        card=replace(
            base.card,
            name=name,
            issuer=issuer,
            emoji=marker,
            reward_programs=(program,),
            reward_limits=(RewardLimit(Decimal(20), "currency", "week", currency),),
        ),
        components=(replace(base.components[0], unit="currency", currency=currency),),
    )

    pages = format_match_pages(mcc, (match,), descriptions, html=True)

    assert len(pages) == 1
    for details, rendered in ((False, pages[0].compact), (True, pages[0].expanded)):
        assert rendered == format_matches(mcc, (match,), descriptions, details=details, html=True)
        assert _html_text(rendered) == format_matches(mcc, (match,), descriptions, details=details)
        assert rendered.startswith(f"<b>🧾 MCC {escape(mcc)} — {escape(description)}</b>")
        assert f"1. {escape(marker)} <b>{escape(name)}</b> — 5 {escape(currency)}" in rendered
        if details:
            assert f"   <i>{escape(issuer)}</i>\n" in rendered
            assert f"Мин. платёж 3 {escape(currency)}" in rendered
            assert (
                f"макс. кэшбэк 150 {escape(currency)} / без лимита {escape(currency)}" in rendered
            )
            assert f"макс. кэшбэк 20 {escape(currency)}/неделю" in rendered
    assert match.card.name == name and match.card.issuer == issuer


def test_html_match_pages_keep_whole_cards_balanced_tags_and_global_ranks(catalog_path):
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    matches = tuple(
        replace(base, card=replace(base.card, id=f"card-{i}", name=f"💳 Card <{i:03d}> &"))
        for i in range(100)
    )
    descriptions = {"5411": "Продукты & <магазины>"}

    pages = format_match_pages("5411", matches, descriptions, html=True)

    assert len(pages) > 1
    all_summaries = []
    for page in pages:
        for rendered in (page.compact, page.expanded):
            assert len(rendered.encode("utf-16-le")) // 2 <= SAFE_MESSAGE_LENGTH
            assert _html_text(rendered).startswith("🛒 MCC 5411 — Продукты & <магазины>\n\n")
        summaries = page.compact.splitlines()[2:]
        expanded_blocks = page.expanded.split("\n\n")[1:]
        assert summaries == [block.splitlines()[0] for block in expanded_blocks]
        assert len(summaries) == page.expanded.count("   <i>Beta Bank</i>\n")
        assert all(block.endswith("макс. в месяц не указан") for block in expanded_blocks)
        all_summaries.extend(summaries)
    assert (
        all_summaries == format_matches("5411", matches, descriptions, html=True).splitlines()[2:]
    )
    assert all_summaries[-1].startswith("100. 🅱️ <b>💳 Card &lt;099&gt; &amp;</b>")


@pytest.mark.parametrize("card_count", [1, 2])
def test_html_match_pages_respect_exact_raw_utf16_boundaries(catalog_path, card_count):
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    match = replace(base, card=replace(base.card, name="💳<&" * 20))
    matches = (match,) * card_count
    expanded = format_matches("5411", matches, details=True, html=True)
    units = len(expanded.encode("utf-16-le")) // 2

    assert format_match_pages("5411", matches, max_length=units, html=True)[0].expanded == expanded
    if card_count == 1:
        with pytest.raises(ValueError, match="exceeds"):
            format_match_pages("5411", matches, max_length=units - 1, html=True)
    else:
        pages = format_match_pages("5411", matches, max_length=units - 1, html=True)
        assert len(pages) == 2
        assert pages[1].compact.splitlines()[2].startswith("2. ")
        for page in pages:
            for rendered in (page.compact, page.expanded):
                _html_text(rendered)
                assert len(rendered.encode("utf-16-le")) // 2 <= units - 1


def test_html_empty_match_pages_style_and_escape_header_at_exact_bound() -> None:
    descriptions = {"5411": 'Food <b>& "shop"</b>'}
    expected = "<b>🧾 MCC 5411 — Food &lt;b&gt;&amp; &quot;shop&quot;&lt;/b&gt;</b>"
    expected += "\n\n❌ Доступных карт нет."
    units = len(expected.encode("utf-16-le")) // 2

    assert format_matches("5411", (), descriptions, html=True) == expected
    pages = format_match_pages("5411", (), descriptions, max_length=units, html=True)
    assert len(pages) == 1
    assert pages[0].compact == pages[0].expanded == expected
    assert _html_text(expected) == format_matches("5411", (), descriptions)
    with pytest.raises(ValueError, match="MCC header exceeds"):
        format_match_pages("5411", (), descriptions, max_length=units - 1, html=True)


@pytest.mark.parametrize("oversized_field", ["name", "issuer", "description"])
def test_html_match_pages_reject_oversized_escaped_fields(catalog_path, oversized_field):
    base = CardCatalog.from_file(catalog_path).lookup("5411")[0]
    descriptions = {"5411": "&" * 1000} if oversized_field == "description" else None
    if oversized_field != "description":
        base = replace(base, card=replace(base.card, **{oversized_field: "&" * 1000}))

    with pytest.raises(ValueError, match="exceeds"):
        format_match_pages("5411", (base,), descriptions, html=True)
