"""Resolve the bank's published monthly «Взаимно!» category choices."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import cache
from zoneinfo import ZoneInfo

from .resources import PACKAGE_DATA_DIR

BNB_VZAIMNO_CARD_ID = "bnb_vzaimno"


@dataclass(frozen=True, slots=True)
class VzaimnoRate:
    name: str
    value: Decimal


@dataclass(frozen=True, slots=True)
class _MonthlyRates:
    starts_on: date
    ends_on: date
    excluded_mccs: frozenset[str]
    by_mcc: dict[str, list[VzaimnoRate]]
    fallback: VzaimnoRate | None
    erip: Decimal | None


@cache
def _published_rates() -> tuple[_MonthlyRates, ...]:
    path = PACKAGE_DATA_DIR / "bnb_vzaimno.json"
    data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    periods = []
    for period in data["periods"]:
        by_mcc = defaultdict(list)
        for category in period["categories"]:
            rate = VzaimnoRate(category["name"], Decimal(category["rate"]))
            for mcc in category["mccs"]:
                by_mcc[mcc].append(rate)
        fallback = period.get("fallback")
        erip = period.get("erip_rate")
        periods.append(
            _MonthlyRates(
                starts_on=date.fromisoformat(period["starts_on"]),
                ends_on=date.fromisoformat(period["ends_on"]),
                excluded_mccs=frozenset(period["excluded_mccs"]),
                by_mcc=by_mcc,
                fallback=VzaimnoRate(fallback["name"], Decimal(fallback["rate"]))
                if fallback is not None
                else None,
                erip=Decimal(erip) if erip is not None else None,
            )
        )
    return tuple(periods)


def _rates_on(on_date: date | None) -> _MonthlyRates | None:
    if on_date is None:
        on_date = datetime.now(ZoneInfo("Europe/Minsk")).date()
    return next(
        (period for period in _published_rates() if period.starts_on <= on_date <= period.ends_on),
        None,
    )


def rate_options(mcc: str, *, on_date: date | None = None) -> tuple[VzaimnoRate, ...]:
    """Return alternative choices for an eligible MCC in a verified period."""
    rates = _rates_on(on_date)
    if rates is None or mcc in rates.excluded_mccs:
        return ()
    if len(mcc) != 4 or not mcc.isascii() or not mcc.isdecimal():
        return ()
    options = list(rates.by_mcc.get(mcc, ()))
    if rates.fallback is not None:
        options.append(rates.fallback)
    return tuple(sorted(options, key=lambda rate: rate.value, reverse=True))


def erip_rate(*, on_date: date | None = None) -> Decimal | None:
    """Return the selectable ERIP rate when the bank has published one."""
    rates = _rates_on(on_date)
    return rates.erip if rates is not None else None
