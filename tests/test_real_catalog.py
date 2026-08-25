from __future__ import annotations

import json
import re
from decimal import Decimal

from mcc_bot.catalog import CardCatalog
from mcc_bot.config import DEFAULT_CATALOG_PATH, DEFAULT_DESCRIPTIONS_PATH
from mcc_bot.descriptions import DescriptionCatalog
from mcc_bot.formatting import format_matches, format_moneyback

DATA_PATH = DEFAULT_CATALOG_PATH
DESCRIPTION_PATH = DEFAULT_DESCRIPTIONS_PATH
KUFAR_EXCLUSIONS = {
    "6010",
    "6011",
    "6012",
    "4900",
    "4814",
    "4829",
    "4899",
    "5960",
    "6050",
    "6051",
    "6211",
    "6531",
    "6536",
    "6537",
    "6538",
    "6539",
    "6540",
    "7311",
    "7399",
    "8398",
    "8999",
    "9222",
    "9223",
    "9311",
    "9399",
    "7995",
    "9402",
}
SUPPORTED_CARD_KEYS = {"id", "name", "issuer", "emoji", "condition", "reward_programs"}
SUPPORTED_PROGRAM_KEYS = {
    "id",
    "kind",
    "tax_exempt",
    "offers",
    "rules",
    "default",
    "excluded_mccs",
}


def _catalog() -> CardCatalog:
    return CardCatalog.from_file(DATA_PATH)


def _descriptions() -> DescriptionCatalog:
    return DescriptionCatalog.from_file(DESCRIPTION_PATH)


def test_real_catalog_has_expected_card_and_offer_counts() -> None:
    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))

    assert list(raw) == ["version", "cards"]
    assert raw["version"] == 2
    assert len(raw["cards"]) == 11
    assert (
        sum(
            len(program.get("offers", []))
            for card in raw["cards"]
            for program in card["reward_programs"]
        )
        == 2815
    )
    assert all(set(card) <= SUPPORTED_CARD_KEYS for card in raw["cards"])
    assert all(
        set(program) <= SUPPORTED_PROGRAM_KEYS
        for card in raw["cards"]
        for program in card["reward_programs"]
    )
    assert all("notes" not in card for card in raw["cards"])
    assert all(
        "notes" not in offer
        for card in raw["cards"]
        for program in card["reward_programs"]
        for offer in program.get("offers", [])
    )
    assert all(card.get("issuer") != "не указан" for card in raw["cards"])
    kufar = next(card for card in raw["cards"] if card["id"] == "kufar")
    social = next(card for card in raw["cards"] if card["id"] == "mtbank_social")
    vitamin = next(card for card in raw["cards"] if card["id"] == "vitamin_d")
    yarkaya = next(card for card in raw["cards"] if card["id"] == "yarkaya_karta")
    assert set(kufar["reward_programs"][0]["excluded_mccs"]) == KUFAR_EXCLUSIONS
    assert social["reward_programs"][0]["default"] == {"value": 1}
    assert set(social["reward_programs"][0]["excluded_mccs"]) == KUFAR_EXCLUSIONS
    assert set(vitamin["reward_programs"][1]["excluded_mccs"]) == KUFAR_EXCLUSIONS
    assert set(yarkaya["reward_programs"][0]["excluded_mccs"]) == KUFAR_EXCLUSIONS


def test_real_catalog_5411_has_expected_sorted_output() -> None:
    matches = _catalog().lookup("5411")

    assert [match.card.id for match in matches] == [
        "vitamin_d",
        "oplati",
        "shopper_mtbank",
        "zepter_card",
        "zepter_plus",
        "belveb_dvizhenie",
        "kufar",
        "mtkarta",
        "mtbank_social",
        "yarkaya_karta",
    ]
    vitamin = matches[0]
    assert vitamin.gross_percent == Decimal("4")
    assert [component.gross_percent for component in vitamin.components] == [
        Decimal("1"),
        Decimal("3"),
    ]
    assert [component.net_percent for component in vitamin.components] == [
        Decimal("1"),
        Decimal("3"),
    ]
    assert format_moneyback(vitamin) == "1% + 3% баллами"

    shopper = next(match for match in matches if match.card.id == "shopper_mtbank")
    assert shopper.gross_percent == Decimal("2.5")
    assert format_moneyback(shopper) == "2,5% (2,44% после налога)"
    oplati = next(match for match in matches if match.card.id == "oplati")
    assert oplati.gross_percent == Decimal("3")
    assert oplati.components[0].kind == "cash"
    assert oplati.components[0].tax_exempt is True
    zepter_card = next(match for match in matches if match.card.id == "zepter_card")
    assert zepter_card.gross_percent == Decimal("2")
    zepter = next(match for match in matches if match.card.id == "zepter_plus")
    assert zepter.gross_percent == Decimal("2")
    kufar = next(match for match in matches if match.card.id == "kufar")
    assert kufar.gross_percent == Decimal("1")
    assert kufar.components[0].kind == "points"

    rendered = format_matches("5411", matches, _descriptions())
    assert rendered == (
        "🛒 MCC 5411 — Продуктовые магазины\n\n"
        "1. 🟢💳 Витамин Д — 1% + 3% баллами\n"
        "2. ⚫💳 Оплати — 3%\n"
        "3. 🔵💳 Шоппер — 2,5% (2,44% после налога)\n"
        "4. 🔴💳 Цептер Card — 2%\n"
        "5. 🔴💳 Цептер PLUS — 2%\n"
        "6. ⚫💳 Движение — 1%\n"
        "7. 🔵💳 Куфар карта — 1%\n"
        "8. 🔵💳 МТКАРТА — 1%\n"  # noqa: RUF001
        "9. 🔵💳 Социальная карта — 1%\n"
        "10. 🟡💳 Яркая карта — 1%"
    )


def test_real_catalog_new_cards_have_exact_program_shapes_and_no_duplicates() -> None:
    cards = {
        card["id"]: card for card in json.loads(DATA_PATH.read_text(encoding="utf-8"))["cards"]
    }

    zepter_card = cards["zepter_card"]
    zepter_offers = zepter_card["reward_programs"][0]["offers"]
    assert len(zepter_offers) == 40
    assert {offer["mcc"] for offer in zepter_offers} == {
        "3351",
        "3501",
        "4111",
        "4112",
        "4121",
        "4511",
        "5094",
        "5262",
        "5300",
        "5309",
        "5311",
        "5411",
        "5462",
        "5499",
        "5532",
        "5541",
        "5542",
        "5552",
        "5719",
        "5732",
        "5812",
        "5814",
        "5912",
        "5944",
        "5945",
        "5962",
        "5977",
        "5995",
        "5999",
        "7011",
        "7298",
        "7512",
        "7534",
        "7538",
        "7542",
        "7832",
        "7996",
        "7998",
        "7999",
        "8099",
    }
    assert {offer["value"] for offer in zepter_offers} == {2}

    oplati = cards["oplati"]
    exempt_program, one_percent_program = oplati["reward_programs"]
    exempt_mccs = {offer["mcc"] for offer in exempt_program["offers"]}
    one_percent_mccs = {offer["mcc"] for offer in one_percent_program["offers"]}
    assert exempt_program["kind"] == "cash"
    assert exempt_program["tax_exempt"] is True
    assert exempt_mccs == {"5411", "5422", "5441", "5451", "5462", "5499"}
    assert len(one_percent_mccs) == len(one_percent_program["offers"]) == 262
    assert exempt_mccs.isdisjoint(one_percent_mccs)
    assert "5921" not in exempt_mccs | one_percent_mccs
    assert all(match.card.id != "oplati" for match in _catalog().lookup("5921"))
    assert all(program.get("default") is None for program in oplati["reward_programs"])
    assert all(offer["value"] == 1 for offer in one_percent_program["offers"])

    yarkaya_program = cards["yarkaya_karta"]["reward_programs"][0]
    assert yarkaya_program["kind"] == "cash"
    assert yarkaya_program["default"] == {"value": 1}
    assert yarkaya_program.get("offers", []) == []
    assert set(yarkaya_program["excluded_mccs"]) == KUFAR_EXCLUSIONS

    for card_id in ("zepter_card", "oplati"):
        for program in cards[card_id]["reward_programs"]:
            mccs = [offer["mcc"] for offer in program["offers"]]
            assert len(mccs) == len(set(mccs))


def test_real_catalog_5912_has_taxed_five_percent_without_internal_conditions() -> None:
    matches = _catalog().lookup("5912")
    social = next(match for match in matches if match.card.id == "mtbank_social")
    assert social.gross_percent == Decimal("5")
    assert social.net_percent == Decimal("4.61")
    assert format_moneyback(social) == "5% (4,61% после налога)"
    assert next(match for match in matches if match.card.id == "mtkarta").card.condition.kind == (
        "max_connected_categories"
    )
    assert next(
        match for match in matches if match.card.id == "cactus_mtbank"
    ).card.condition.kind == ("selected_category")
    rendered = format_matches("5912", matches, _descriptions())
    assert "⚫💳 Движение — 5% (4,61% после налога)" in rendered
    assert "подключённых категорий" not in rendered
    assert "выбранная категория" not in rendered


def test_real_catalog_social_uses_one_percent_fallback_with_kufar_exclusions() -> None:
    catalog = _catalog()
    matches = catalog.lookup("7297")

    social = next(match for match in matches if match.card.id == "mtbank_social")
    assert social.gross_percent == Decimal("1")
    assert social.net_percent == Decimal("1")
    assert format_moneyback(social) == "1%"
    assert all(match.card.id != "mtbank_social" for match in catalog.lookup("6010"))


def test_real_catalog_kufar_fallback_exclusions_vitamin_and_leading_zero() -> None:
    catalog = _catalog()

    assert all(
        match.card.id not in {"kufar", "vitamin_d", "yarkaya_karta"}
        for match in catalog.lookup("6010")
    )
    fallback = next(match for match in catalog.lookup("1234") if match.card.id == "kufar")
    assert fallback.gross_percent == Decimal("2")
    vitamin = next(match for match in catalog.lookup("1234") if match.card.id == "vitamin_d")
    assert vitamin.components[0].kind == "points"
    assert vitamin.gross_percent == Decimal("3")
    vitamin_zero = next(match for match in catalog.lookup("0742") if match.card.id == "vitamin_d")
    assert [component.gross_percent for component in vitamin_zero.components] == [
        Decimal("1"),
        Decimal("3"),
    ]
    yarkaya = next(match for match in catalog.lookup("1234") if match.card.id == "yarkaya_karta")
    assert yarkaya.gross_percent == Decimal("1")


def test_real_catalog_uses_requested_display_metadata() -> None:
    cards = {card.id: card for card in _catalog().cards}

    assert cards["belveb_dvizhenie"].name == "Движение"
    assert cards["belveb_dvizhenie"].issuer == "Банк БелВЭБ"
    assert cards["belveb_dvizhenie"].emoji == "⚫💳"
    assert cards["zepter_plus"].issuer == "Цептер Банк"
    assert cards["zepter_card"].name == "Цептер Card"
    assert cards["zepter_card"].issuer == "Цептер Банк"
    assert cards["vitamin_d"].issuer == "Белинвестбанк"
    assert cards["kufar"].issuer == "МТБанк / Visa / Kufar"
    assert cards["yarkaya_karta"].issuer == "Приорбанк"
    for card_id in ("mtkarta", "mtbank_social", "shopper_mtbank", "cactus_mtbank"):
        assert cards[card_id].issuer == "МТБанк"


def test_mcc_descriptions_are_pinned_shape_with_fallback() -> None:
    descriptions = _descriptions()
    assert descriptions.get("5411") == "Продуктовые магазины"
    assert descriptions.get("0742")
    assert descriptions.get("0000") == "описание не найдено"
    assert descriptions.get("5661") == "Обувь"
    assert descriptions.get("5309") == "Магазины беспошлинной торговли"
    assert descriptions.get("7297") == "Массаж"
    assert descriptions.get("7392") == (
        "Консультации по связям с общественностью"  # noqa: RUF001
    )
    assert all(
        re.search(r"[А-Яа-яЁё]", label)  # noqa: RUF001
        for label in descriptions.labels.values()
    )
