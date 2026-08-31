# ruff: noqa: RUF001

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from mcc_bot.partner_rewards import PartnerRepository
from mcc_bot.partner_seed_20260830 import load_seed
from mcc_bot.partner_seed_20260831 import SEED_PATH, apply_partner_seed
from mcc_bot.stores import StoreRepository


def test_correction_snapshot_contains_only_the_reviewed_partner_rules() -> None:
    seed = load_seed(SEED_PATH)
    offers = seed["offers"]

    assert [item["source_key"] for item in offers] == [
        "cashalot:21vek-by",
        "bnb-1-2-3:a72c0ccd",
        "plushki:promo:01",
        "statuskarta:21vek:2026-08-30",
        "cashalot:unistore-opt-roznitsa",
    ]
    assert Counter(item["card_id"] for item in offers) == {
        "belgazprombank_cashalot": 2,
        "bnb_1_2_3": 1,
        "vitamin_d": 1,
        "statusbank_statuskarta": 1,
    }
    assert {item["card_id"] for item in offers}.isdisjoint(
        {"sber", "alfa", "paritet_combo", "vtb"}
    )
    status = next(item for item in offers if item["card_id"] == "statusbank_statuskarta")
    assert status["brand"] == "21век"
    assert status["channel"] == "online"
    assert status["mode"] == "total"
    assert status["reward_kind"] == "cash"
    assert status["tiers"] == [{"value": "2.5"}]
    vitamin = next(item for item in offers if item["card_id"] == "vitamin_d")
    assert vitamin["channel"] == "online"
    assert vitamin["mode"] == "additional"
    assert vitamin["reward_kind"] == "points"
    assert vitamin["tiers"] == [
        {"value": "5", "min_purchase": "100", "per_transaction_cap": "50"}
    ]
    assert seed["exclusions"] == [
        {
            "source_key": "plushki:exclusion:1",
            "brand_key": "brand:plushki-exclusion:1",
            "brand": "Unistore",
            "aliases": ["Unistore опт&розница", "Юнистор", "Юнисторе", "Uni store"],
            "card_id": "vitamin_d",
            "reward_kind": "points",
            "channel": "any",
            "mcc": None,
            "starts_on": "2021-11-15",
            "ends_on": None,
            "suppress_base": True,
            "reason": "С 15.11.2021 бонусные баллы не начисляются",
            "source_url": (
                "file:D:/Перечень компаний, при осуществлении операций в которых "
                "не начисляются Бонусы.pdf"
            ),
        }
    ]


def test_correction_seed_is_insert_missing_and_second_run_adds_nothing(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    partners = PartnerRepository(stores)

    first = apply_partner_seed(stores, partners, actor_id=1)

    assert first == {
        "brands_created": 2,
        "brands_reused": 4,
        "brands_skipped_missing": 0,
        "brands_skipped_ambiguous": 0,
        "brand_mappings_repaired": 0,
        "brand_mapping_conflicts": 0,
        "offers_added": 5,
        "offers_existing": 0,
        "offers_skipped": 0,
        "exclusions_added": 1,
        "exclusions_existing": 0,
        "exclusions_skipped": 0,
    }
    vek = stores.search("21век").matches[0]
    offers = {offer.card_id: offer for offer in partners.list_offers(vek.id)}
    assert offers["statusbank_statuskarta"].maximum_value == Decimal("2.5")
    assert offers["belgazprombank_cashalot"].maximum_value == Decimal("1.01")
    assert offers["bnb_1_2_3"].maximum_value == Decimal("2")
    assert offers["vitamin_d"].maximum_value == Decimal("5")
    with stores.connection() as connection:
        audit_count = sum(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("store_audit", "partner_audit")
        )

    second = apply_partner_seed(stores, partners, actor_id=1)

    assert second == {
        "brands_created": 0,
        "brands_reused": 6,
        "brands_skipped_missing": 0,
        "brands_skipped_ambiguous": 0,
        "brand_mappings_repaired": 0,
        "brand_mapping_conflicts": 0,
        "offers_added": 0,
        "offers_existing": 5,
        "offers_skipped": 0,
        "exclusions_added": 0,
        "exclusions_existing": 1,
        "exclusions_skipped": 0,
    }
    with stores.connection() as connection:
        assert audit_count == sum(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("store_audit", "partner_audit")
        )
