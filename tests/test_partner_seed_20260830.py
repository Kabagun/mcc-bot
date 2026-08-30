from __future__ import annotations

import json
from collections import Counter

from mcc_bot.partner_rewards import PartnerRepository
from mcc_bot.partner_seed_20260830 import apply_partner_seed, load_seed
from mcc_bot.stores import StoreRepository


def test_official_snapshot_has_reviewed_counts_and_no_conflicts() -> None:
    seed = load_seed()
    offers = seed["offers"]
    counts = Counter(item["source_key"].split(":", 1)[0] for item in offers)

    assert len(offers) == 205
    assert counts == {
        "cactus": 114,
        "cashalot": 23,
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


def test_seed_is_insert_missing_and_preserves_manual_edits(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    partners = PartnerRepository(stores)

    first = apply_partner_seed(stores, partners, actor_id=1)
    assert first == {
        "brands_created": 181,
        "brands_reused": 27,
        "offers_added": 205,
        "offers_existing": 0,
        "exclusions_added": 7,
        "exclusions_existing": 0,
    }
    vek = stores.search("21vek").matches[0]
    offer = next(item for item in partners.list_offers(vek.id) if item.card_id == "vitamin_d")
    partners.delete_offer(offer.id, actor_id=2)

    second = apply_partner_seed(stores, partners, actor_id=1)
    assert second == {
        "brands_created": 0,
        "brands_reused": 208,
        "offers_added": 0,
        "offers_existing": 205,
        "exclusions_added": 0,
        "exclusions_existing": 7,
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
