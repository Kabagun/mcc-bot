"""Resolve favorite categories from Alfa-Bonus's published appendix."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache

from .catalog import Card
from .resources import PACKAGE_DATA_DIR
from .stores import normalize_store_name

ALFA_BONUS_CARD_ID = "alfa_bonus"
ALFA_BONUS_NAME = "Альфа-Бонус"


@dataclass(frozen=True, slots=True)
class AlfaCategory:
    name: str
    short_name: str
    rate_hint: str | None = None


@dataclass(frozen=True, slots=True)
class _CategoryIndex:
    by_mcc: dict[str, list[AlfaCategory]]
    by_merchant: dict[str, AlfaCategory]
    fallback: AlfaCategory
    excluded_mccs: frozenset[str]


@cache
def _category_index() -> _CategoryIndex:
    path = PACKAGE_DATA_DIR / "alfa_bonus_categories.json"
    rows = json.loads(path.read_text(encoding="utf-8"))["categories"]
    by_mcc = defaultdict(list)
    by_merchant = {}
    for row in rows:
        category = AlfaCategory(
            row["name"], row.get("short_name", row["name"]), row.get("rate_hint")
        )
        for mcc in row.get("mccs", ()):
            by_mcc[mcc].append(category)
        for merchant in row.get("merchants", ()):
            by_merchant[normalize_store_name(merchant)] = category
        if "excluded_mccs" in row:
            fallback = category
            excluded_mccs = frozenset(row["excluded_mccs"])
    return _CategoryIndex(by_mcc, by_merchant, fallback, excluded_mccs)


def alfa_bonus_categories(
    cards: Iterable[Card], mcc: str | None = None, *, merchant_names: Iterable[str] = ()
) -> tuple[AlfaCategory, ...]:
    """Return specific matches first, or the fallback for an eligible known MCC."""

    if not any(card.id == ALFA_BONUS_CARD_ID for card in cards):
        return ()
    index = _category_index()
    matches = dict.fromkeys(
        category
        for merchant in merchant_names
        if (category := index.by_merchant.get(normalize_store_name(merchant))) is not None
    )
    if mcc is not None:
        matches.update(dict.fromkeys(index.by_mcc.get(mcc, ())))
        if not matches and mcc not in index.excluded_mccs:
            return (index.fallback,)
    return tuple(matches)
