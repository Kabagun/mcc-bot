# Published Russian category names are intentional.
# ruff: noqa: RUF001

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from mcc_bot import bnb_vzaimno
from mcc_bot.bnb_vzaimno import VzaimnoRate, erip_rate, rate_options

NOVEMBER = date(2026, 11, 22)
ALL_PURCHASES = VzaimnoRate("Все покупки", Decimal(1))


@pytest.mark.parametrize(
    ("mcc", "category"),
    [
        ("5983", "АЗС"),
        ("5552", "Зарядные станции"),
        ("5300", "Маркетплейсы"),
        ("0742", "Питомцы"),
        ("7251", "Одежда и обувь"),
        ("5818", "Развлечения и досуг"),
        ("4121", "Такси"),
    ],
)
def test_specific_and_fallback_choices_are_alternatives(mcc: str, category: str) -> None:
    assert rate_options(mcc, on_date=NOVEMBER) == (
        VzaimnoRate(category, Decimal(5)),
        ALL_PURCHASES,
    )


def test_november_supermarkets_only_match_all_purchases() -> None:
    assert rate_options("5411", on_date=NOVEMBER) == (ALL_PURCHASES,)


@pytest.mark.parametrize("mcc", ["4814", "4900", "6531", "6540", "7995"])
def test_exclusions_do_not_receive_the_fallback(mcc: str) -> None:
    assert rate_options(mcc, on_date=NOVEMBER) == ()
    assert erip_rate(on_date=NOVEMBER) == Decimal(2)


@pytest.mark.parametrize("on_date", [date(2026, 11, 2), date(2026, 11, 30)])
def test_verified_period_includes_both_boundaries(on_date: date) -> None:
    assert rate_options("5411", on_date=on_date) == (ALL_PURCHASES,)
    assert erip_rate(on_date=on_date) == Decimal(2)


@pytest.mark.parametrize("on_date", [date(2026, 11, 1), date(2026, 12, 1)])
def test_unverified_period_has_no_rates(on_date: date) -> None:
    assert rate_options("5411", on_date=on_date) == ()
    assert erip_rate(on_date=on_date) is None


def test_default_date_rolls_over_at_midnight_in_minsk(monkeypatch: pytest.MonkeyPatch) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 30, 21, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(bnb_vzaimno, "datetime", FrozenDatetime)
    assert rate_options("5411") == ()
    assert erip_rate() is None
