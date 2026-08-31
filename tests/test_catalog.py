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
    calculate_net_percent,
    normalize_mcc,
)


def test_lookup_sorts_gross_sum_descending_and_breaks_ties_by_name(catalog_path: Path) -> None:
    catalog = CardCatalog.from_file(catalog_path)
    matches = catalog.lookup("5411")

    assert [match.card.id for match in matches] == ["beta", "gamma", "alpha"]
    assert [match.gross_percent for match in matches] == [
        Decimal("5"),
        Decimal("5"),
        Decimal("2.5"),
    ]
    assert [component.kind for component in matches[0].components] == ["cash"]


def test_lookup_accepts_friendly_and_leading_zero_forms(catalog_path: Path) -> None:
    catalog = CardCatalog.from_file(catalog_path)
    assert [match.card.id for match in catalog.lookup("MCC:5411")] == ["beta", "gamma", "alpha"]

    # A zero-padded MCC remains four digits throughout resolution.
    assert catalog.lookup("0742") == ()


def test_lookup_omits_cards_without_requested_offer(catalog_path: Path) -> None:
    catalog = CardCatalog.from_file(catalog_path)
    assert [match.card.id for match in catalog.lookup("5812")] == ["alpha"]
    assert catalog.lookup("0000") == ()


def test_percentage_tax_formula_and_exemption() -> None:
    assert calculate_net_percent(Decimal("2")) == Decimal("2")
    assert calculate_net_percent(Decimal("3")) == Decimal("2.87")
    assert calculate_net_percent(Decimal("5")) == Decimal("4.61")
    assert calculate_net_percent(Decimal("3"), tax_exempt=True) == Decimal("3")
    assert calculate_net_moneyback(Moneyback(Decimal("2.5"))) == Decimal("2.435")


@pytest.mark.parametrize("raw_value", ["", "541", "54111", "abc1", "MCC 12", "5411 5812"])
def test_normalize_mcc_rejects_invalid_values(raw_value: str) -> None:
    with pytest.raises(InvalidMccError):
        normalize_mcc(raw_value)


def _write_payload(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "cards.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _minimal_card(program: dict[str, object]) -> dict[str, object]:
    return {
        "id": "card",
        "name": "Card",
        "emoji": "💳",
        "reward_programs": [program],
    }


def test_partner_policy_fallback_is_readable_but_not_explicit(tmp_path: Path) -> None:
    card = _minimal_card(
        {"id": "cash", "kind": "cash", "tax_exempt": False, "offers": []}
    )
    catalog = CardCatalog.from_file(_write_payload(tmp_path, {"version": 2, "cards": [card]}))

    assert catalog.cards[0].partner_policy.mode == "total"
    assert catalog.cards[0].partner_policy.reward_kind == "cash"
    assert catalog.cards[0].partner_policy_explicit is False


def test_provided_partner_policy_is_marked_explicit(tmp_path: Path) -> None:
    card = _minimal_card(
        {"id": "points", "kind": "points", "tax_exempt": True, "offers": []}
    )
    card["partner_policy"] = {"mode": "additional", "reward_kind": "points"}
    catalog = CardCatalog.from_file(_write_payload(tmp_path, {"version": 2, "cards": [card]}))

    assert catalog.cards[0].partner_policy_explicit is True


@pytest.mark.parametrize(
    "payload", [[], {"version": 1, "cards": []}, {"version": 3, "cards": []}, {"cards": []}]
)
def test_catalog_root_contract_is_validated(tmp_path: Path, payload: object) -> None:
    with pytest.raises(CatalogError):
        CardCatalog.from_file(_write_payload(tmp_path, payload))


def test_catalog_requires_explicit_v2_and_allows_empty_live_catalog(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match=r"version: 2"):
        CardCatalog.from_file(_write_payload(tmp_path, {"cards": []}))
    assert CardCatalog.from_file(_write_payload(tmp_path, {"version": 2, "cards": []})).cards == ()


def test_catalog_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_bytes(b'{"version": 2, "cards": [\xff]}')
    with pytest.raises(CatalogError, match="UTF-8"):
        CardCatalog.from_file(path)


@pytest.mark.parametrize(
    ("program", "message"),
    [
        (
            {"kind": "cash", "tax_exempt": False, "offers": [{"mcc": "5411", "value": -1}]},
            "неотрицательным",
        ),
        (
            {"kind": "cash", "tax_exempt": False, "offers": [{"mcc": "5411", "value": "1.5"}]},
            "JSON",
        ),
        ({"kind": "cash", "tax_exempt": False, "offers": [{"mcc": "5411", "value": True}]}, "JSON"),
        (
            {"kind": "cash", "tax_exempt": "false", "offers": [{"mcc": "5411", "value": 1}]},
            "boolean",
        ),
        (
            {"kind": "other", "tax_exempt": False, "offers": [{"mcc": "5411", "value": 1}]},
            "cash.*points",
        ),
        (
            {
                "kind": "cash",
                "tax_exempt": False,
                "offers": [{"mcc": "5411", "value": 1}],
                "unit": "currency",
            },
            "обязателен",
        ),
    ],
)
def test_reward_program_contract_is_strict(
    tmp_path: Path, program: dict[str, object], message: str
) -> None:
    payload = {"version": 2, "cards": [_minimal_card(program)]}
    with pytest.raises(CatalogError, match=message):
        CardCatalog.from_file(_write_payload(tmp_path, payload))


def test_grouped_rules_are_expanded_and_duplicate_mccs_are_rejected(tmp_path: Path) -> None:
    payload = {
        "version": 2,
        "cards": [
            _minimal_card(
                {
                    "kind": "cash",
                    "tax_exempt": False,
                    "rules": [{"mccs": ["0742", "5411"], "value": 3}],
                }
            )
        ],
    }
    catalog = CardCatalog.from_file(_write_payload(tmp_path, payload))
    assert catalog.lookup("0742")[0].gross_percent == Decimal("3")

    duplicate = {
        "version": 2,
        "cards": [
            _minimal_card(
                {
                    "kind": "cash",
                    "tax_exempt": False,
                    "offers": [{"mcc": "5411", "value": 1}],
                    "rules": [{"mccs": ["5411"], "value": 2}],
                }
            )
        ],
    }
    with pytest.raises(CatalogError, match="дублирующий"):
        CardCatalog.from_file(_write_payload(tmp_path, duplicate))


def test_program_precedence_explicit_exclusion_default_and_zero(tmp_path: Path) -> None:
    payload = {
        "version": 2,
        "cards": [
            _minimal_card(
                {
                    "kind": "points",
                    "tax_exempt": False,
                    "default": {"value": 2},
                    "excluded_mccs": ["7999"],
                    "offers": [
                        {"mcc": "5411", "value": 1},
                        {"mcc": "5812", "value": 0},
                        {"mcc": "7999", "value": 0},
                    ],
                }
            )
        ],
    }
    catalog = CardCatalog.from_file(_write_payload(tmp_path, payload))
    assert catalog.lookup("5411")[0].gross_percent == Decimal("1")
    assert catalog.lookup("5812")[0].gross_percent == Decimal("0")
    assert catalog.lookup("7999")[0].gross_percent == Decimal("0")
    assert catalog.lookup("5999")[0].gross_percent == Decimal("2")


def test_currency_programs_are_supported_but_mixed_dimensions_are_rejected(tmp_path: Path) -> None:
    valid = {
        "version": 2,
        "cards": [
            _minimal_card(
                {
                    "kind": "cash",
                    "tax_exempt": False,
                    "unit": "currency",
                    "currency": "byn",
                    "offers": [{"mcc": "5411", "value": 2}],
                }
            )
        ],
    }
    match = CardCatalog.from_file(_write_payload(tmp_path, valid)).lookup("5411")[0]
    assert match.components[0].moneyback.currency == "BYN"

    mixed = {
        "version": 2,
        "cards": [
            _minimal_card(
                {
                    "kind": "cash",
                    "tax_exempt": False,
                    "unit": "currency",
                    "currency": "BYN",
                    "offers": [{"mcc": "5411", "value": 2}],
                }
            ),
            {
                **_minimal_card(
                    {
                        "kind": "cash",
                        "tax_exempt": False,
                        "unit": "currency",
                        "currency": "USD",
                        "offers": [{"mcc": "5411", "value": 2}],
                    }
                ),
                "id": "other",
            },
        ],
    }
    with pytest.raises(CatalogError, match="нельзя сравнить"):
        CardCatalog.from_file(_write_payload(tmp_path, mixed))


def test_cash_and_points_stack_and_tax_is_component_wise(tmp_path: Path) -> None:
    payload = {
        "version": 2,
        "cards": [
            {
                "id": "stacked",
                "name": "Stacked",
                "emoji": "✨",
                "reward_programs": [
                    {
                        "id": "cash",
                        "kind": "cash",
                        "tax_exempt": False,
                        "offers": [{"mcc": "5411", "value": 3}],
                    },
                    {
                        "id": "points",
                        "kind": "points",
                        "tax_exempt": True,
                        "offers": [{"mcc": "5411", "value": 3}],
                    },
                ],
            }
        ],
    }
    match = CardCatalog.from_file(_write_payload(tmp_path, payload)).lookup("5411")[0]
    assert match.gross_percent == Decimal("6")
    assert [component.net_percent for component in match.components] == [
        Decimal("2.87"),
        Decimal("3"),
    ]


def test_conditions_are_typed(tmp_path: Path) -> None:
    payload = {
        "version": 2,
        "cards": [
            {
                "id": "card",
                "name": "Card",
                "emoji": "💳",
                "condition": {"kind": "max_connected_categories", "count": 3},
                "reward_programs": [
                    {"kind": "cash", "tax_exempt": False, "offers": [{"mcc": "5411", "value": 1}]}
                ],
            }
        ],
    }
    card = CardCatalog.from_file(_write_payload(tmp_path, payload)).cards[0]
    assert card.condition is not None and card.condition.count == 3


def test_reward_program_payment_and_cap_terms_are_typed(tmp_path: Path) -> None:
    payload = {
        "version": 2,
        "cards": [
            _minimal_card(
                {
                    "kind": "points",
                    "tax_exempt": False,
                    "minimum_payment": {"amount": 10, "currency": "byn"},
                    "maximum_reward": {"amount": 200, "unit": "points"},
                    "offers": [{"mcc": "5411", "value": 1}],
                }
            ),
            {
                **_minimal_card(
                    {
                        "kind": "cash",
                        "tax_exempt": False,
                        "maximum_reward": {
                            "unlimited": True,
                            "unit": "currency",
                            "currency": "byn",
                        },
                        "offers": [{"mcc": "5411", "value": 1}],
                    }
                ),
                "id": "unlimited",
            },
        ],
    }
    catalog = CardCatalog.from_file(_write_payload(tmp_path, payload))
    points = catalog.cards[0].reward_programs[0]
    assert points.minimum_payment is not None
    assert points.minimum_payment.amount == Decimal("10")
    assert points.minimum_payment.currency == "BYN"
    assert points.maximum_reward is not None
    assert points.maximum_reward.amount == Decimal("200")
    assert points.maximum_reward.unit == "points"
    unlimited = catalog.cards[1].reward_programs[0].maximum_reward
    assert unlimited is not None and unlimited.unlimited
    assert unlimited.currency == "BYN"


def test_reward_program_supports_currency_cap_alternatives_and_foreign_rate(
    tmp_path: Path,
) -> None:
    payload = {
        "version": 2,
        "cards": [
            _minimal_card(
                {
                    "kind": "cash",
                    "tax_exempt": False,
                    "maximum_reward": {
                        "amount": 150,
                        "unit": "currency",
                        "currency": "byn",
                    },
                    "maximum_reward_alternatives": [
                        {"amount": 50, "unit": "currency", "currency": "usd"},
                        {"amount": 50, "unit": "currency", "currency": "eur"},
                    ],
                    "domestic_country": "by",
                    "foreign_value": 1,
                    "offers": [{"mcc": "5411", "value": 2}],
                }
            )
        ],
    }

    program = CardCatalog.from_file(_write_payload(tmp_path, payload)).cards[0].reward_programs[0]

    assert program.maximum_reward is not None
    assert program.maximum_reward.currency == "BYN"
    assert [cap.currency for cap in program.maximum_reward_alternatives] == ["USD", "EUR"]
    assert program.domestic_country == "BY"
    assert program.foreign_value == Decimal("1")


def test_card_supports_non_monthly_reward_limits(tmp_path: Path) -> None:
    card = _minimal_card(
        {
            "kind": "cash",
            "tax_exempt": False,
            "offers": [{"mcc": "5411", "value": 1}],
        }
    )
    card["reward_limits"] = [
        {"amount": 20, "unit": "currency", "currency": "byn", "period": "week"},
        {"amount": 20, "unit": "currency", "currency": "byn", "period": "transaction"},
    ]

    parsed = CardCatalog.from_file(_write_payload(tmp_path, {"version": 2, "cards": [card]}))

    assert [limit.period for limit in parsed.cards[0].reward_limits] == ["week", "transaction"]
    assert all(limit.amount == Decimal("20") for limit in parsed.cards[0].reward_limits)


@pytest.mark.parametrize(
    "maximum_reward",
    [
        {"amount": "200", "unit": "points"},
        {"amount": 200, "unlimited": True, "unit": "points"},
        {"amount": 200, "unlimited": False, "unit": "points"},
        {"amount": 200, "unit": "points", "currency": "BYN"},
        {"unlimited": False, "unit": "currency", "currency": "BYN"},
    ],
)
def test_reward_cap_contract_is_strict(tmp_path: Path, maximum_reward: dict[str, object]) -> None:
    payload = {
        "version": 2,
        "cards": [
            _minimal_card(
                {
                    "kind": "cash",
                    "tax_exempt": False,
                    "maximum_reward": maximum_reward,
                    "offers": [{"mcc": "5411", "value": 1}],
                }
            )
        ],
    }
    with pytest.raises(CatalogError):
        CardCatalog.from_file(_write_payload(tmp_path, payload))


def test_duplicate_card_program_and_offer_are_rejected(tmp_path: Path) -> None:
    card = _minimal_card(
        {
            "id": "cash",
            "kind": "cash",
            "tax_exempt": False,
            "offers": [{"mcc": "5411", "value": 1}, {"mcc": 5411, "value": 2}],
        }
    )
    payload = {"version": 2, "cards": [card, {**card, "name": "Other"}]}
    with pytest.raises(CatalogError, match="дублиру"):
        CardCatalog.from_file(_write_payload(tmp_path, payload))
