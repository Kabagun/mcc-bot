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
    assert len(raw["cards"]) == 8
    assert (
        sum(
            len(program.get("offers", []))
            for card in raw["cards"]
            for program in card["reward_programs"]
        )
        == 2507
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
    vitamin = next(card for card in raw["cards"] if card["id"] == "vitamin_d")
    assert set(kufar["reward_programs"][0]["excluded_mccs"]) == KUFAR_EXCLUSIONS
    assert set(vitamin["reward_programs"][1]["excluded_mccs"]) == KUFAR_EXCLUSIONS


def test_real_catalog_5411_starts_with_vitamin_shopper_zepter_and_kufar_explicit() -> None:
    matches = _catalog().lookup("5411")

    assert [match.card.id for match in matches[:3]] == [
        "vitamin_d",
        "shopper_mtbank",
        "zepter_plus",
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
    assert format_moneyback(vitamin) == "1% деньгами + 3% баллами"

    shopper = next(match for match in matches if match.card.id == "shopper_mtbank")
    assert shopper.gross_percent == Decimal("2.5")
    assert format_moneyback(shopper) == "2,5% деньгами (2,44% после налога)"
    zepter = next(match for match in matches if match.card.id == "zepter_plus")
    assert zepter.gross_percent == Decimal("2")
    kufar = next(match for match in matches if match.card.id == "kufar")
    assert kufar.gross_percent == Decimal("1")
    assert kufar.components[0].kind == "points"

    rendered = format_matches("5411", matches, _descriptions())
    assert rendered.startswith("🛒 MCC 5411 — Продуктовые магазины")
    assert "манибэк от 0" not in rendered
    assert "after tax" not in rendered and "Note" not in rendered


def test_real_catalog_5912_has_taxed_five_percent_and_typed_conditions() -> None:
    matches = _catalog().lookup("5912")
    screenshot = next(match for match in matches if match.card.id == "screenshot_base_unknown")
    assert screenshot.gross_percent == Decimal("5")
    assert screenshot.net_percent == Decimal("4.61")
    assert format_moneyback(screenshot) == "5% деньгами (4,61% после налога)"
    assert next(match for match in matches if match.card.id == "mtkarta").card.condition.kind == (
        "max_connected_categories"
    )
    assert next(
        match for match in matches if match.card.id == "cactus_mtbank"
    ).card.condition.kind == ("selected_category")
    rendered = format_matches("5912", matches, _descriptions())
    assert "   ↳ до 3 подключённых категорий" in rendered
    assert "   ↳ только выбранная категория" in rendered


def test_real_catalog_kufar_fallback_exclusions_vitamin_and_leading_zero() -> None:
    catalog = _catalog()

    assert all(match.card.id not in {"kufar", "vitamin_d"} for match in catalog.lookup("6010"))
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


def test_real_catalog_uses_requested_display_metadata() -> None:
    cards = {card.id: card for card in _catalog().cards}

    assert cards["screenshot_base_unknown"].name == "Карта (название уточняется)"
    assert cards["screenshot_base_unknown"].issuer is None
    assert cards["screenshot_base_unknown"].emoji == "❓💳"
    assert cards["zepter_plus"].issuer == "Цептер Банк"
    assert cards["vitamin_d"].issuer == "Белинвестбанк"
    assert cards["kufar"].issuer == "МТБанк / Visa / Kufar"
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
