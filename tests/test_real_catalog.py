from __future__ import annotations

import json
from pathlib import Path

from mcc_bot.catalog import CardCatalog
from mcc_bot.formatting import format_matches, format_moneyback

DATA_PATH = Path(__file__).parents[1] / "data" / "cards.json"
SUPPORTED_CARD_KEYS = {
    "id",
    "name",
    "issuer",
    "notes",
    "default_offer",
    "excluded_mccs",
    "offers",
}


def _catalog() -> CardCatalog:
    return CardCatalog.from_file(DATA_PATH)


def test_real_catalog_has_expected_card_and_offer_counts() -> None:
    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))

    assert list(raw) == ["version", "cards"]
    assert raw["version"] == 1
    assert len(raw["cards"]) == 8
    assert sum(len(card["offers"]) for card in raw["cards"]) == 2507
    assert all(set(card) <= SUPPORTED_CARD_KEYS for card in raw["cards"])
    assert all("notes" not in offer for card in raw["cards"] for offer in card["offers"])
    assert all(
        "notes" not in card.get("default_offer", {})
        for card in raw["cards"]
        if "default_offer" in card
    )
    assert all(card.get("issuer") != "не указан" for card in raw["cards"])


def test_real_catalog_5411_orders_shopper_then_zepter_and_kufar_is_explicit() -> None:
    catalog = _catalog()
    matches = catalog.lookup("5411")

    assert matches[0].card.id == "shopper_mtbank"
    assert matches[0].offer.moneyback.value == 2.5
    assert format_moneyback(matches[0]) == "2.5% (2.44% after tax)"
    assert (
        next(match for match in matches if match.card.id == "zepter_plus").offer.moneyback.value
        == 2
    )

    kufar = next(match for match in matches if match.card.id == "kufar")
    assert kufar.offer.moneyback.value == 1

    rendered = format_matches("5411", matches)
    assert "Шоппер МТБанк — МТБанк: 2.5% (2.44% after tax)" in rendered
    assert "Цептер PLUS" in rendered


def test_real_catalog_5912_has_taxed_five_percent_and_condition_notes() -> None:
    catalog = _catalog()
    matches = catalog.lookup("5912")

    screenshot = next(match for match in matches if match.card.id == "screenshot_base_unknown")
    assert screenshot.offer.moneyback.value == 5
    assert format_moneyback(screenshot) == "5% (4.61% after tax)"
    assert screenshot.card.notes == "Название карты нужно уточнить."
    assert next(match for match in matches if match.card.id == "mtkarta").card.notes == (
        "Только 3 группы можно подключить."
    )
    assert next(match for match in matches if match.card.id == "cactus_mtbank").card.notes == (
        "Одна группа на выбор — 3%."
    )


def test_real_catalog_kufar_fallback_exclusions_and_leading_zero_mcc() -> None:
    catalog = _catalog()

    assert all(match.card.id != "kufar" for match in catalog.lookup("6010"))
    fallback = next(match for match in catalog.lookup("1234") if match.card.id == "kufar")
    assert fallback.offer.moneyback.value == 2
    vitamin = next(match for match in catalog.lookup("0742") if match.card.id == "vitamin_d")
    assert vitamin.offer.moneyback.value == 1


def test_real_catalog_keeps_placeholder_and_actual_issuers() -> None:
    catalog = _catalog()
    cards = {card.id: card for card in catalog.cards}

    assert cards["screenshot_base_unknown"].name == "Карта из таблицы (название уточняется)"
    assert cards["screenshot_base_unknown"].notes == "Название карты нужно уточнить."
    assert cards["vitamin_d"].notes is None
    for card_id in ("mtkarta", "mtbank_social", "shopper_mtbank", "cactus_mtbank"):
        assert cards[card_id].issuer == "МТБанк"
