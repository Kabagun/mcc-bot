from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

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
