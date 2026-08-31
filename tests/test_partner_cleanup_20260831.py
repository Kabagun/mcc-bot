# ruff: noqa: RUF001

from __future__ import annotations

from decimal import Decimal

import pytest

from mcc_bot.partner_cleanup_20260830 import _input
from mcc_bot.partner_cleanup_20260831 import (
    PartnerCleanupError,
    _exclusion_input,
    apply_partner_cleanup,
)
from mcc_bot.partner_rewards import PartnerRepository
from mcc_bot.partner_seed_20260830 import SEED_PATH as PREVIOUS_SEED_PATH
from mcc_bot.partner_seed_20260830 import load_seed
from mcc_bot.partner_seed_20260831 import SEED_PATH
from mcc_bot.stores import StoreRepository


def _brand(stores: StoreRepository, name: str, channel: str, mcc: str | None):
    payload = {"name": name, "channel": channel}
    if mcc is not None:
        payload["mcc"] = mcc
    result = stores.apply_change("add_merchant", payload, 1)
    assert result.brand_id is not None
    return result


def _seed_rows(stores: StoreRepository, partners: PartnerRepository) -> tuple[int, int, int]:
    previous = load_seed(PREVIOUS_SEED_PATH)
    desired = load_seed(SEED_PATH)
    previous_offers = {item["source_key"]: item for item in previous["offers"]}
    desired_offers = {item["source_key"]: item for item in desired["offers"]}

    vek_online = _brand(stores, "21vek", "online", "5300")
    vek_offline = _brand(stores, "21 век", "offline", "5300")
    unistore = _brand(stores, "Unistore", "offline", "5411")
    unistore_variant = _brand(stores, "Unistore опт&розница", "offline", "5411")
    other = _brand(stores, "Другой партнёр", "online", None)

    placements = {
        "cashalot:21vek-by": (vek_online.brand_id, previous_offers["cashalot:21vek-by"]),
        "bnb-1-2-3:a72c0ccd": (
            vek_offline.brand_id,
            desired_offers["bnb-1-2-3:a72c0ccd"],
        ),
        "plushki:promo:01": (vek_online.brand_id, desired_offers["plushki:promo:01"]),
        "statuskarta:21vek:2026-08-30": (
            vek_offline.brand_id,
            desired_offers["statuskarta:21vek:2026-08-30"],
        ),
        "cashalot:unistore-opt-roznitsa": (
            unistore_variant.brand_id,
            desired_offers["cashalot:unistore-opt-roznitsa"],
        ),
    }
    for source_key, (brand_id, item) in placements.items():
        partners.create_offer(
            _input(item, brand_id), actor_id=1, source_key=source_key
        )
    exclusion_item = desired["exclusions"][0]
    partners.create_exclusion(
        _exclusion_input(exclusion_item, unistore.brand_id),
        actor_id=1,
        source_key=exclusion_item["source_key"],
    )
    extra_status = partners.create_offer(
        _input(desired_offers["statuskarta:21vek:2026-08-30"], other.brand_id),
        actor_id=1,
        source_key="statuskarta:other:2026-08-30",
    )
    return vek_online.brand_id, unistore.brand_id, extra_status.id


def test_cleanup_canonicalizes_reviewed_variants_preserves_mcc_and_is_idempotent(
    tmp_path,
) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    partners = PartnerRepository(stores)
    partners.initialize()
    vek_id, unistore_id, extra_status_id = _seed_rows(stores, partners)

    first = apply_partner_cleanup(stores, partners, actor_id=1)

    assert first == {
        "brands_merged": 2,
        "brands_renamed": 2,
        "offers_updated": 1,
        "exclusions_updated": 0,
        "rules_already_current": 5,
        "status_offers_archived": 1,
    }
    vek = stores.get_brand(vek_id)
    assert vek is not None
    assert vek.name == "21век"
    assert {"21vek", "21vek.by", "21 век"} <= set(vek.aliases)
    assert {(fact.channel, fact.mcc) for fact in stores.list_brand_mcc(vek.id)} == {
        ("offline", "5300"),
        ("online", "5300"),
    }
    vek_offers = {offer.card_id: offer for offer in partners.list_offers(vek.id)}
    assert vek_offers["belgazprombank_cashalot"].channel == "any"
    assert vek_offers["belgazprombank_cashalot"].maximum_value == Decimal("1.01")
    assert vek_offers["statusbank_statuskarta"].maximum_value == Decimal("2.5")
    unistore = stores.get_brand(unistore_id)
    assert unistore is not None
    assert unistore.name == "Unistore"
    assert {
        "Unistore опт&розница",
        "Юнистор",
        "Юнисторе",
        "Uni store",
    } <= set(unistore.aliases)
    assert {(fact.channel, fact.mcc) for fact in stores.list_brand_mcc(unistore.id)} == {
        ("offline", "5411")
    }
    assert partners.list_offers(unistore.id, card_id="belgazprombank_cashalot")[
        0
    ].maximum_value == Decimal("1.01")
    assert partners.list_exclusions(unistore.id, card_id="vitamin_d")[0].suppress_base
    assert partners.get_offer(extra_status_id) is None
    with stores.connection() as connection:
        audit_count = sum(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("store_audit", "partner_audit")
        )

    second = apply_partner_cleanup(stores, partners, actor_id=1)

    assert second == {
        "brands_merged": 0,
        "brands_renamed": 0,
        "offers_updated": 0,
        "exclusions_updated": 0,
        "rules_already_current": 6,
        "status_offers_archived": 0,
    }
    with stores.connection() as connection:
        assert audit_count == sum(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("store_audit", "partner_audit")
        )


def test_cleanup_rejects_human_changes_before_any_merge(tmp_path) -> None:
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    partners = PartnerRepository(stores)
    partners.initialize()
    previous = load_seed(PREVIOUS_SEED_PATH)
    item = next(item for item in previous["offers"] if item["source_key"] == "cashalot:21vek-by")
    first = _brand(stores, "21vek", "online", "5300")
    second = _brand(stores, "21 век", "offline", "5300")
    payload = _input(item, first.brand_id)
    payload = type(payload)(
        brand_id=payload.brand_id,
        card_id=payload.card_id,
        channel=payload.channel,
        mode=payload.mode,
        reward_kind=payload.reward_kind,
        tiers=(type(payload.tiers[0])(Decimal("9")),),
        starts_on=payload.starts_on,
        ends_on=payload.ends_on,
        conditions="Изменено помощником",
        source_url=payload.source_url,
    )
    partners.create_offer(payload, actor_id=2, source_key=item["source_key"])

    with pytest.raises(PartnerCleanupError, match="cashalot:21vek-by"):
        apply_partner_cleanup(stores, partners, actor_id=1)

    assert stores.get_brand(first.brand_id).name == "21vek"
    assert stores.get_brand(second.brand_id).name == "21 век"
    assert len(stores.search("21vek").matches) == 2
