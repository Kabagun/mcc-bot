from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal

from mcc_bot.partner_rewards import PartnerOfferInput, PartnerRepository, PartnerTierInput
from mcc_bot.partner_seed_20260830 import apply_partner_seed, load_seed
from mcc_bot.stores import StoreRepository


def test_official_snapshot_has_reviewed_counts_and_no_conflicts() -> None:
    seed = load_seed()
    offers = seed["offers"]
    counts = Counter(item["source_key"].split(":", 1)[0] for item in offers)

    assert len(offers) == 203
    assert counts == {
        "cactus": 114,
        "cashalot": 21,
        "bnb-1-2-3": 39,
        "plushki": 13,
        "izi": 4,
        "combo": 11,
        "statuskarta": 1,
    }
    assert len(seed["exclusions"]) == 7
    assert all(item["suppress_base"] is True for item in seed["exclusions"])
    global_transport = [item for item in seed["exclusions"] if item["brand_key"] is None]
    assert {item["mcc"] for item in global_transport} == {"4111", "4112", "4121", "4131"}
    assert len({item["source_key"] for item in offers}) == len(offers)
    cactus = [item for item in offers if item["source_key"].startswith("cactus:")]
    assert sum("streetcult" in item["brand"].casefold() for item in cactus) == 0
    assert sum("армтек" in {alias.casefold() for alias in item["aliases"]} for item in cactus) == 1
    bnb_brands = {item["brand"].casefold() for item in offers if item["card_id"] == "bnb_1_2_3"}
    assert len(bnb_brands) == 38
    cashalot = [item for item in offers if item["source_key"].startswith("cashalot:")]
    assert {item["source_key"] for item in cashalot}.isdisjoint(
        {"cashalot:7-karat", "cashalot:xistore-by"}
    )
    assert all(item["require_existing_mcc"] is True for item in cashalot)
    assert {item["source_key"] for item in cashalot if item["channel"] == "online"} == {
        "cashalot:21vek-by",
        "cashalot:7745-bolshoy-magazin",
    }
    assert all(
        item["channel"] == "offline"
        for item in cashalot
        if item["source_key"] not in {"cashalot:21vek-by", "cashalot:7745-bolshoy-magazin"}
    )
    assert all(item["match_channel"] == item["channel"] for item in cashalot)
    status = next(item for item in offers if item["source_key"].startswith("statuskarta:"))
    assert status["aliases"] == ["21vek", "21vek.by"]
    assert status["channel"] == status["match_channel"] == "online"
    assert status["require_existing_mcc"] is True
    assert status["tiers"] == [{"value": "2.5"}]
    assert status["source_url"] == (
        "https://stbank.by/private-client/payment-cards/debetovye-karty/statuskarta-deb/"
    )


def test_seed_is_insert_missing_and_preserves_manual_edits(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    partners = PartnerRepository(stores)

    first = apply_partner_seed(stores, partners, actor_id=1)
    assert first == {
        "brands_created": 164,
        "brands_reused": 20,
        "brands_skipped_missing": 22,
        "brands_skipped_ambiguous": 0,
        "brand_mappings_repaired": 0,
        "brand_mapping_conflicts": 0,
        "offers_added": 181,
        "offers_existing": 0,
        "offers_skipped": 22,
        "exclusions_added": 7,
        "exclusions_existing": 0,
        "exclusions_skipped": 0,
    }
    vek = stores.search("21vek").matches[0]
    offer = next(item for item in partners.list_offers(vek.id) if item.card_id == "vitamin_d")
    partners.delete_offer(offer.id, actor_id=2)

    second = apply_partner_seed(stores, partners, actor_id=1)
    assert second == {
        "brands_created": 0,
        "brands_reused": 184,
        "brands_skipped_missing": 22,
        "brands_skipped_ambiguous": 0,
        "brand_mappings_repaired": 0,
        "brand_mapping_conflicts": 0,
        "offers_added": 0,
        "offers_existing": 181,
        "offers_skipped": 22,
        "exclusions_added": 0,
        "exclusions_existing": 7,
        "exclusions_skipped": 0,
    }
    assert partners.get_offer(offer.id) is None
    assert partners.get_offer(offer.id, include_archived=True).source_key == offer.source_key


def test_seed_creates_searchable_partner_only_store(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    partners = PartnerRepository(stores)
    apply_partner_seed(stores, partners, actor_id=1)

    brand = stores.search("Cyber X Green").matches[0]
    assert partners.list_active_offers(brand.id)[0][0].card_id == "paritet_combo"
    assert stores.list_brand_mcc(brand.id) == ()


def test_seed_reuses_exact_legacy_duplicates_without_merging_or_fuzzy_matching(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()

    def imported(store_id: int, *, online: bool):
        return stores.import_store(
            {
                "id": store_id,
                "network_id": None,
                "network_name": None,
                "name": "Legacy Partner",
                "is_online": online,
                "address": f"Address {store_id}",
            },
            [
                {
                    "mcc": "5411",
                    "payment_date": "2026-08",
                    "merchant_type": "Shop",
                    "address_extra": None,
                }
            ],
        )

    first = imported(1, online=False)
    imported(2, online=False)
    online = imported(3, online=True)
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(
        json.dumps(
            {
                "version": 1,
                "offers": [
                    {
                        "source_key": "test:any",
                        "brand_key": "test-brand:any",
                        "brand": "Legacy Partner",
                        "aliases": [],
                        "card_id": "card-any",
                        "channel": "any",
                        "mode": "total",
                        "reward_kind": "cash",
                        "tiers": [{"value": "2"}],
                    },
                    {
                        "source_key": "test:online",
                        "brand_key": "test-brand:online",
                        "brand": "Legacy Partner",
                        "aliases": [],
                        "card_id": "card-online",
                        "channel": "online",
                        "mode": "total",
                        "reward_kind": "cash",
                        "tiers": [{"value": "3"}],
                    },
                ],
                "exclusions": [],
            }
        ),
        encoding="utf-8",
    )
    partners = PartnerRepository(stores)

    apply_partner_seed(stores, partners, actor_id=1, path=seed_path)

    assert partners.list_offers(first.brand_id, card_id="card-any")
    assert partners.list_offers(online.brand_id, card_id="card-online")
    assert len(stores.search("Legacy Partner").matches) == 3


def test_catalog_bound_seed_skips_missing_and_ambiguous_brands(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()

    def imported(store_id: int, name: str, mcc: str):
        return stores.import_store(
            {
                "id": store_id,
                "network_id": None,
                "network_name": None,
                "name": name,
                "is_online": False,
                "address": f"Address {store_id}",
            },
            [
                {
                    "mcc": mcc,
                    "payment_date": "2026-08",
                    "merchant_type": "Shop",
                    "address_extra": None,
                }
            ],
        )

    unique = imported(1, "Unique Partner", "5411")
    imported(2, "Ambiguous Partner", "5211")
    imported(3, "Ambiguous Partner", "5211")
    with stores.connection() as connection:
        brands_before = connection.execute("SELECT count(*) FROM store_brands").fetchone()[0]
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(
        json.dumps(
            {
                "version": 1,
                "offers": [
                    {
                        "source_key": "test:unique",
                        "brand_key": "test-brand:unique",
                        "brand": "Unique Partner",
                        "aliases": [],
                        "card_id": "test-card",
                        "channel": "offline",
                        "require_existing_mcc": True,
                        "match_channel": "offline",
                        "match_mcc": "5411",
                        "mode": "total",
                        "reward_kind": "cash",
                        "tiers": [{"value": "2"}],
                    },
                    {
                        "source_key": "test:missing",
                        "brand_key": "test-brand:missing",
                        "brand": "Missing Partner",
                        "aliases": [],
                        "card_id": "test-card",
                        "channel": "offline",
                        "require_existing_mcc": True,
                        "match_channel": "offline",
                        "mode": "total",
                        "reward_kind": "cash",
                        "tiers": [{"value": "2"}],
                    },
                    {
                        "source_key": "test:ambiguous",
                        "brand_key": "test-brand:ambiguous",
                        "brand": "Ambiguous Partner",
                        "aliases": [],
                        "card_id": "test-card",
                        "channel": "offline",
                        "require_existing_mcc": True,
                        "match_channel": "offline",
                        "match_mcc": "5211",
                        "mode": "total",
                        "reward_kind": "cash",
                        "tiers": [{"value": "2"}],
                    },
                ],
                "exclusions": [],
            }
        ),
        encoding="utf-8",
    )
    partners = PartnerRepository(stores)

    counters = apply_partner_seed(stores, partners, actor_id=1, path=seed_path)

    assert counters == {
        "brands_created": 0,
        "brands_reused": 1,
        "brands_skipped_missing": 1,
        "brands_skipped_ambiguous": 1,
        "brand_mappings_repaired": 0,
        "brand_mapping_conflicts": 0,
        "offers_added": 1,
        "offers_existing": 0,
        "offers_skipped": 2,
        "exclusions_added": 0,
        "exclusions_existing": 0,
        "exclusions_skipped": 0,
    }
    assert partners.list_offers(unique.brand_id, card_id="test-card")
    with stores.connection() as connection:
        brands_after = connection.execute("SELECT count(*) FROM store_brands").fetchone()[0]
    assert brands_after == brands_before


def test_catalog_bound_seed_repairs_stale_mapping_without_overwriting_offer(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()

    def imported(store_id: int, *, online: bool):
        return stores.import_store(
            {
                "id": store_id,
                "network_id": None,
                "network_name": None,
                "name": "21vek.by",
                "is_online": online,
                "address": f"Address {store_id}",
            },
            [
                {
                    "mcc": "5300",
                    "payment_date": "2026-08",
                    "merchant_type": "Shop",
                    "address_extra": None,
                }
            ],
        )

    imported(1, online=False)
    online = imported(2, online=True)
    stale = stores.apply_change(
        "add_merchant",
        {"name": "21vek", "aliases": (), "channel": "online"},
        actor_id=1,
    )
    partners = PartnerRepository(stores)
    partners.initialize()
    with stores.transaction() as connection:
        connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?)",
            ("test-brand:21vek", stale.brand_id),
        )
    seed_path = tmp_path / "seed.json"
    payload = {
        "version": 1,
        "offers": [
            {
                "source_key": "test:21vek",
                "brand_key": "test-brand:21vek",
                "brand": "21 век",
                "aliases": ["21vek.by"],
                "card_id": "test-card",
                "channel": "online",
                "require_existing_mcc": True,
                "match_channel": "online",
                "match_mcc": "5300",
                "mode": "total",
                "reward_kind": "cash",
                "conditions": "Original seed text",
                "tiers": [{"value": "2"}],
            }
        ],
        "exclusions": [],
    }
    seed_path.write_text(json.dumps(payload), encoding="utf-8")
    existing = partners.create_offer(
        PartnerOfferInput(
            brand_id=stale.brand_id,
            card_id="test-card",
            channel="online",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("2")),),
            conditions="Original seed text",
        ),
        actor_id=1,
        source_key="test:21vek",
    )

    first = apply_partner_seed(stores, partners, actor_id=1, path=seed_path)

    assert first["brand_mappings_repaired"] == 1
    assert first["offers_added"] == 0
    assert first["offers_existing"] == 1
    with stores.connection() as connection:
        mapping = connection.execute(
            "SELECT brand_id FROM partner_seed_brands WHERE source_key=?",
            ("test-brand:21vek",),
        ).fetchone()
    assert mapping["brand_id"] == online.brand_id
    offer = partners.list_offers(online.brand_id, card_id="test-card")[0]
    assert offer.id == existing.id
    assert partners.delete_offer(offer.id, actor_id=2) is True
    payload["offers"][0]["conditions"] = "Changed official text"
    payload["offers"][0]["tiers"] = [{"value": "3"}]
    seed_path.write_text(json.dumps(payload), encoding="utf-8")

    second = apply_partner_seed(stores, partners, actor_id=1, path=seed_path)

    archived = partners.get_offer(offer.id, include_archived=True)
    assert archived is not None and archived.archived is True
    assert archived.conditions == "Original seed text"
    assert archived.tiers[0].value == Decimal("2")
    assert second["brand_mappings_repaired"] == 0
    assert second["offers_existing"] == 1


def test_catalog_bound_seed_reports_conflict_for_human_edited_stale_offer(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    eligible = stores.apply_change(
        "add_merchant",
        {"name": "Strict store", "channel": "offline", "mcc": "5411"},
        actor_id=1,
    )
    stale = stores.apply_change(
        "add_merchant", {"name": "Strict store", "channel": "offline"}, actor_id=1
    )
    assert eligible.brand_id is not None and stale.brand_id is not None
    partners = PartnerRepository(stores)
    partners.initialize()
    with stores.transaction() as connection:
        connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?)",
            ("strict:brand", stale.brand_id),
        )
    offer = partners.create_offer(
        PartnerOfferInput(
            brand_id=stale.brand_id,
            card_id="test-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("9")),),
            conditions="Изменено помощником",
        ),
        actor_id=2,
        source_key="strict:offer",
    )
    seed_path = tmp_path / "strict.json"
    seed_path.write_text(
        json.dumps(
            {
                "version": 1,
                "offers": [
                    {
                        "source_key": "strict:offer",
                        "brand_key": "strict:brand",
                        "brand": "Strict store",
                        "aliases": [],
                        "card_id": "test-card",
                        "channel": "offline",
                        "require_existing_mcc": True,
                        "match_channel": "offline",
                        "match_mcc": "5411",
                        "mode": "total",
                        "reward_kind": "cash",
                        "conditions": "Official seed text",
                        "tiers": [{"value": "2"}],
                    }
                ],
                "exclusions": [],
            }
        ),
        encoding="utf-8",
    )

    result = apply_partner_seed(stores, partners, actor_id=1, path=seed_path)

    assert result["brand_mapping_conflicts"] == 1
    assert result["brand_mappings_repaired"] == 0
    assert result["offers_skipped"] == 1
    with stores.connection() as connection:
        mapping = connection.execute(
            "SELECT brand_id FROM partner_seed_brands WHERE source_key='strict:brand'"
        ).fetchone()
    assert mapping["brand_id"] == stale.brand_id
    preserved = partners.get_offer(offer.id, include_archived=True)
    assert preserved is not None and preserved.brand_id == stale.brand_id
    assert preserved.maximum_value == Decimal("9")
    assert preserved.conditions == "Изменено помощником"
