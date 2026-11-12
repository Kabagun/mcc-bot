"""Current-schema card and partner corrections, with synthetic store databases."""

# ruff: noqa: RUF001

from datetime import date
from decimal import Decimal

import pytest

from mcc_bot.catalog import CardCatalog
from mcc_bot.formatting import format_matches, format_moneyback
from mcc_bot.partner_rewards import PartnerRepository, resolve_store_matches
from mcc_bot.partner_seed_20260830 import apply_partner_seed, load_seed
from mcc_bot.resources import DEFAULT_CATALOG_PATH, default_data_path
from mcc_bot.stores import StoreRepository

SEED_PATH = default_data_path("partner_seed_20261112.json")
TODAY = date(2026, 11, 12)


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    stores = StoreRepository(tmp_path_factory.mktemp("november-partners") / "stores.sqlite3")
    stores.initialize()
    partners = PartnerRepository(stores)
    first = apply_partner_seed(stores, partners, actor_id=1, path=SEED_PATH)
    catalog = CardCatalog.from_file(DEFAULT_CATALOG_PATH)
    return stores, partners, catalog, first


def _matches(prepared, name, *, mcc="5411", channel="online", amount="100", on_date=TODAY):
    stores, partners, catalog, _first = prepared
    brand = next(brand for brand in stores.list_brands() if brand.name == name)
    return resolve_store_matches(
        catalog,
        partners,
        brand.id,
        channel,
        mcc,
        amount=Decimal(amount),
        on_date=on_date,
    )


def test_snapshot_uses_existing_partner_rules_and_import_is_idempotent(prepared):
    stores, partners, _catalog, first = prepared
    raw = load_seed(SEED_PATH)
    assert len(raw["offers"]) == first["offers_added"] == 367
    assert len(raw["exclusions"]) == first["exclusions_added"] == 22
    assert {item["card_id"] for item in raw["offers"]} == {"belarusbank_shchodraya"}
    assert sum(item["reward_kind"] == "points" for item in raw["offers"]) == 28
    assert all(item["mode"] == "additional" for item in raw["offers"])
    second = apply_partner_seed(stores, partners, actor_id=1, path=SEED_PATH)
    assert second["offers_added"] == second["exclusions_added"] == second["brands_created"] == 0


def test_shchodraya_base_matches_the_approved_existing_card_format(prepared):
    catalog = prepared[2]
    card = next(card for card in catalog.cards if card.id == "belarusbank_shchodraya")
    assert len(card.reward_programs) == 1
    program = card.reward_programs[0]
    assert program.kind == "points" and program.default_value == Decimal("2")
    assert program.minimum_payment.amount == Decimal("20")
    assert program.maximum_reward.amount == Decimal("40")
    assert program.maximum_reward.unit == "points"


def test_online_partner_points_apply_and_expire_without_changing_base(prepared):
    def reward(**kwargs):
        return next(
            match
            for match in _matches(prepared, "21век", **kwargs)
            if match.card.id == "belarusbank_shchodraya"
        )

    assert format_moneyback(reward()) == "2% + 5% баллами"
    assert format_moneyback(reward(amount="99")) == "2% баллами"
    assert format_moneyback(reward(channel="offline")) == "2% баллами"
    assert format_moneyback(reward(on_date=date(2026, 12, 1))) == "2% баллами"
    match = reward()
    compact = format_matches("5411", (match,), html=True)
    assert "Шчодрая</b> — 2% + 5% баллами" in compact
    assert "30.11.2026" not in compact and "регистрац" not in compact


@pytest.mark.parametrize("name", ["Белавиа", "Белорусская железная дорога", "Атлас", "Unistore"])
def test_named_exclusions_remove_points_keep_cash_and_hide_captions(prepared, name):
    matches = _matches(prepared, name)
    vitamin = next(match for match in matches if match.card.id == "vitamin_d")
    assert format_moneyback(vitamin) == "1% деньгами"
    assert [component.kind for component in vitamin.components] == ["cash"]
    assert vitamin.context_lines == ()
    rendered = format_matches("5411", (vitamin,), details=True)
    assert "не начисляются" not in rendered and "15.11.2021" not in rendered
    shchodraya = next(
        (match for match in matches if match.card.id == "belarusbank_shchodraya"), None
    )
    assert shchodraya is None or all(c.kind == "cash" for c in shchodraya.components)


@pytest.mark.parametrize("mcc", ["4112", "4121", "4131", "4511"])
def test_transport_mcc_exclusions_keep_vitamin_cash_only_for_other_carriers(prepared, mcc):
    match = next(
        match for match in _matches(prepared, "21век", mcc=mcc) if match.card.id == "vitamin_d"
    )
    assert format_moneyback(match) == "1% деньгами"
    assert match.context_lines == ()
