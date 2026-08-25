from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from mcc_bot.catalog import (
    CardCatalog,
    CatalogError,
    InvalidMccError,
    Moneyback,
    calculate_net_moneyback,
    normalize_mcc,
)


def test_lookup_sorts_moneyback_descending_and_breaks_ties_by_name(catalog_path: Path) -> None:
    catalog = CardCatalog.from_file(catalog_path)

    matches = catalog.lookup("5411")

    assert [match.card.id for match in matches] == ["beta", "gamma", "alpha"]
    assert [match.offer.moneyback.value for match in matches] == [
        Decimal("5"),
        Decimal("5"),
        Decimal("2.5"),
    ]


def test_lookup_accepts_friendly_mcc_forms(catalog_path: Path) -> None:
    catalog = CardCatalog.from_file(catalog_path)

    assert [match.card.id for match in catalog.lookup("MCC:5411")] == [
        "beta",
        "gamma",
        "alpha",
    ]


def test_lookup_omits_cards_without_requested_offer(catalog_path: Path) -> None:
    catalog = CardCatalog.from_file(catalog_path)

    assert [match.card.id for match in catalog.lookup("5812")] == ["alpha"]
    assert catalog.lookup("0000") == ()


def test_percentage_tax_formula_is_applied_only_above_two_percent() -> None:
    assert calculate_net_moneyback(Moneyback(Decimal("2"))) == Decimal("2")
    assert calculate_net_moneyback(Moneyback(Decimal("3"))) == Decimal("2.87")
    assert calculate_net_moneyback(Moneyback(Decimal("2.5"))) == Decimal("2.435")
    assert calculate_net_moneyback(
        Moneyback(Decimal("3"), unit="currency", currency="BYN")
    ) == Decimal("3")


@pytest.mark.parametrize("raw_value", ["", "541", "54111", "abc1", "MCC 12", "5411 5812"])
def test_normalize_mcc_rejects_invalid_values(raw_value: str) -> None:
    with pytest.raises(InvalidMccError):
        normalize_mcc(raw_value)


@pytest.mark.parametrize(
    "payload",
    [[], {"version": 2, "cards": []}, {"version": 1}, {"cards": []}],
)
def test_catalog_root_contract_is_validated(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "cards.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogError):
        CardCatalog.from_file(path)


def test_catalog_requires_explicit_version(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text('{"cards": []}', encoding="utf-8")

    with pytest.raises(CatalogError, match=r"catalog\.version is required"):
        CardCatalog.from_file(path)


def test_catalog_allows_empty_live_catalog(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text('{"version": 1, "cards": []}', encoding="utf-8")

    assert CardCatalog.from_file(path).cards == ()


def test_catalog_rejects_boolean_version(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text('{"version": true, "cards": []}', encoding="utf-8")

    with pytest.raises(CatalogError, match="version"):
        CardCatalog.from_file(path)


def test_catalog_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_bytes(b'{"version": 1, "cards": [\xff]}')

    with pytest.raises(CatalogError, match="UTF-8"):
        CardCatalog.from_file(path)


@pytest.mark.parametrize(
    ("offer", "message"),
    [
        ({"mcc": "5411", "moneyback": -1}, "non-negative"),
        ({"mcc": "5411", "moneyback": "1.5"}, "JSON number"),
        ({"mcc": "5411", "moneyback": True}, "JSON number"),
        ({"mcc": "5411", "moneyback": 1, "unit": "points"}, "unit"),
        ({"mcc": "5411", "moneyback": 1, "unit": "currency"}, "currency"),
        ({"mcc": "5411", "moneyback": 1, "unit": "percent", "currency": "BYN"}, "currency"),
        ({"mcc": "5411", "moneyback": 1, "notes": ["not a string"]}, "notes"),
    ],
)
def test_offer_contract_is_validated(
    tmp_path: Path, offer: dict[str, object], message: str
) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "cards": [{"id": "card", "name": "Card", "offers": [offer]}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match=message):
        CardCatalog.from_file(path)


def test_currency_moneyback_is_parsed_and_preserved(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "cards": [
                    {
                        "id": "card",
                        "name": "Card",
                        "offers": [
                            {
                                "mcc": "5411",
                                "moneyback": 2,
                                "unit": "currency",
                                "currency": "byn",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    match = CardCatalog.from_file(path).lookup("5411")[0]
    assert match.offer.moneyback.currency == "BYN"


@pytest.mark.parametrize(
    ("second_offer", "expected_message"),
    [
        ({"mcc": "5411", "moneyback": 2, "unit": "currency", "currency": "BYN"}, "incompatible"),
        ({"mcc": "5411", "moneyback": 2, "unit": "currency", "currency": "USD"}, "USD"),
    ],
)
def test_catalog_rejects_cross_card_moneyback_dimensions(
    tmp_path: Path, second_offer: dict[str, object], expected_message: str
) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "cards": [
                    {
                        "id": "percent-card",
                        "name": "Percent Card",
                        "offers": [{"mcc": "5411", "moneyback": 5, "unit": "percent"}],
                    },
                    {
                        "id": "other-card",
                        "name": "Other Card",
                        "offers": [second_offer],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match=expected_message):
        CardCatalog.from_file(path)


def test_default_offer_fallback_and_explicit_precedence(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "cards": [
                    {
                        "id": "kufar",
                        "name": "Kufar card",
                        "default_offer": {"moneyback": 2, "unit": "percent"},
                        "excluded_mccs": ["7999"],
                        "offers": [
                            {"mcc": "5411", "moneyback": 1, "unit": "percent"},
                            {"mcc": "5812", "moneyback": 0, "unit": "percent"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    catalog = CardCatalog.from_file(path)

    assert catalog.lookup("5411")[0].offer.moneyback.value == Decimal("1")
    assert catalog.lookup("5812")[0].offer.moneyback.value == Decimal("0")
    assert catalog.lookup("5999")[0].offer.moneyback.value == Decimal("2")
    assert catalog.lookup("7999") == ()


def test_default_offer_can_be_used_without_explicit_offers(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "cards": [
                    {
                        "id": "card",
                        "name": "Card",
                        "default_offer": {"moneyback": 2, "unit": "percent"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert CardCatalog.from_file(path).lookup("5411")[0].offer.moneyback.value == Decimal("2")


def test_explicit_offer_wins_over_exclusion(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "cards": [
                    {
                        "id": "card",
                        "name": "Card",
                        "default_offer": {"moneyback": 2},
                        "excluded_mccs": ["5411"],
                        "offers": [{"mcc": "5411", "moneyback": 0}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert CardCatalog.from_file(path).lookup("5411")[0].offer.moneyback.value == Decimal("0")


def test_default_offer_dimension_conflicts_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "cards": [
                    {
                        "id": "percent-card",
                        "name": "Percent Card",
                        "default_offer": {"moneyback": 2, "unit": "percent"},
                        "offers": [],
                    },
                    {
                        "id": "currency-card",
                        "name": "Currency Card",
                        "default_offer": {
                            "moneyback": 2,
                            "unit": "currency",
                            "currency": "BYN",
                        },
                        "offers": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match=r"MCC 0000.*incompatible"):
        CardCatalog.from_file(path)


def test_duplicate_card_or_offer_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "cards": [
                    {
                        "id": "card",
                        "name": "Card",
                        "offers": [
                            {"mcc": "5411", "moneyback": 1},
                            {"mcc": 5411, "moneyback": 2},
                        ],
                    },
                    {"id": "card", "name": "Other", "offers": []},
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="duplicate"):
        CardCatalog.from_file(path)
