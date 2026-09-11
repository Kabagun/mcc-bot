from __future__ import annotations

import copy
import hashlib
import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from mcc_bot.partner_batch import (
    PartnerBatchError,
    StalePartnerPlanError,
    _backup_database,
    _write_json_result,
    apply_partner_batch,
    load_snapshot,
    preview_partner_batch,
)
from mcc_bot.partner_rewards import (
    PartnerExclusionInput,
    PartnerOfferInput,
    PartnerRepository,
    PartnerTierInput,
)
from mcc_bot.stores import StoreRepository


def _database(tmp_path):
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    partners = PartnerRepository(stores)
    partners.initialize()
    brand = stores.apply_change(
        "add_merchant", {"name": "Batch Acme", "channel": "offline"}, actor_id=1
    )
    assert brand.brand_id is not None
    return stores, partners, brand.brand_id


def _offer(source_key: str, brand_id: int, *, value: str = "2", card_id: str = "batch-card"):
    return {
        "source_key": source_key,
        "brand_id": brand_id,
        "card_id": card_id,
        "channel": "offline",
        "mode": "total",
        "reward_kind": "cash",
        "tiers": [{"value": value}],
    }


def _offer_guard(offer):
    return {
        "brand_id": offer.brand_id,
        "card_id": offer.card_id,
        "channel": offer.channel,
        "mode": offer.mode,
        "reward_kind": offer.reward_kind,
        "starts_on": offer.starts_on.isoformat() if offer.starts_on else None,
        "ends_on": offer.ends_on.isoformat() if offer.ends_on else None,
        "conditions": offer.conditions,
        "source_url": offer.source_url,
        "tiers": [
            {
                "value": format(tier.value, "f"),
                "min_purchase": format(tier.min_purchase, "f")
                if tier.min_purchase is not None
                else None,
                "max_purchase": format(tier.max_purchase, "f")
                if tier.max_purchase is not None
                else None,
                "per_transaction_cap": format(tier.per_transaction_cap, "f")
                if tier.per_transaction_cap is not None
                else None,
                "starts_on": tier.starts_on.isoformat() if tier.starts_on else None,
                "ends_on": tier.ends_on.isoformat() if tier.ends_on else None,
            }
            for tier in offer.tiers
        ],
    }


def _snapshot(*offers, exclusions=None, problems=None):
    result = {"version": 1, "offers": list(offers), "exclusions": exclusions or []}
    if problems is not None:
        result["problems"] = problems
    return result


def _offer_count(stores) -> int:
    with stores.connection() as connection:
        return connection.execute("SELECT count(*) FROM partner_offers").fetchone()[0]


def test_json_result_uses_utf8_when_windows_stream_defaults_to_cp1252():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")

    _write_json_result({"merchant": "Кактус"}, stream=stream)
    stream.flush()

    assert json.loads(raw.getvalue().decode("utf-8")) == {"merchant": "Кактус"}


def test_preview_is_deterministic_and_does_not_modify_target(tmp_path):
    stores, _partners, brand_id = _database(tmp_path)
    snapshot = _snapshot(_offer("batch:new", brand_id))
    before = hashlib.sha256(stores.path.read_bytes()).hexdigest()

    first = preview_partner_batch(stores, snapshot)
    second = preview_partner_batch(stores, snapshot)

    assert first == second
    assert first["counts"]["offers_new"] == 1
    assert first["counts"]["approved_inserts"] == 1
    assert hashlib.sha256(stores.path.read_bytes()).hexdigest() == before
    assert _offer_count(stores) == 0


def test_apply_rejects_stale_plan_without_creating_backup(tmp_path):
    stores, _partners, brand_id = _database(tmp_path)
    snapshot = _snapshot(_offer("batch:new", brand_id))
    plan = preview_partner_batch(stores, snapshot)
    stores.apply_change("add_merchant", {"name": "Unrelated", "channel": "offline"}, actor_id=2)

    with pytest.raises(StalePartnerPlanError):
        apply_partner_batch(
            stores,
            snapshot,
            actor_id=1,
            expected_plan_sha256=plan["plan_sha256"],
            backup_path=tmp_path / "must-not-exist.sqlite3",
        )

    assert not (tmp_path / "must-not-exist.sqlite3").exists()
    assert _offer_count(stores) == 0


def test_apply_creates_backup_and_replay_is_noop(tmp_path):
    stores, partners, brand_id = _database(tmp_path)
    snapshot = _snapshot(
        _offer("batch:new", brand_id),
        exclusions=[
            {
                "source_key": "batch:exclude",
                "brand_id": brand_id,
                "card_id": "batch-card",
                "reward_kind": "cash",
                "channel": "offline",
                "mcc": "5411",
                "suppress_base": True,
            }
        ],
    )
    plan = preview_partner_batch(stores, snapshot)
    backup = tmp_path / "before.sqlite3"
    result = apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=plan["plan_sha256"],
        backup_path=backup,
    )

    assert backup.exists()
    assert result["inserted"] == {"offers": 1, "exclusions": 1}
    assert _offer_count(stores) == 1
    with stores.connection() as connection:
        assert connection.execute("SELECT count(*) FROM partner_exclusions").fetchone()[0] == 1

    replay_plan = preview_partner_batch(stores, snapshot)
    assert replay_plan["counts"]["offers_unchanged"] == 1
    assert replay_plan["counts"]["exclusions_unchanged"] == 1
    replay_backup = tmp_path / "replay.sqlite3"
    replay = apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=replay_plan["plan_sha256"],
        backup_path=replay_backup,
    )
    assert replay["inserted"] == {"offers": 0, "exclusions": 0}
    assert replay_backup.exists()
    assert len(partners.list_offers(brand_id)) == 1


def test_apply_is_atomic_when_one_approved_insert_fails(tmp_path, monkeypatch):
    stores, _partners, brand_id = _database(tmp_path)
    snapshot = _snapshot(
        _offer("batch:first", brand_id, card_id="first"),
        _offer("batch:second", brand_id, card_id="second"),
    )
    plan = preview_partner_batch(stores, snapshot)
    original = PartnerRepository.create_offer
    calls = 0

    def fail_on_second(self, payload, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated insert failure")
        return original(self, payload, **kwargs)

    monkeypatch.setattr(PartnerRepository, "create_offer", fail_on_second)
    with pytest.raises(RuntimeError, match="simulated insert failure"):
        apply_partner_batch(
            stores,
            snapshot,
            actor_id=1,
            expected_plan_sha256=plan["plan_sha256"],
            backup_path=tmp_path / "atomic-backup.sqlite3",
        )

    assert _offer_count(stores) == 0


def test_preview_reports_conflicts_without_touching_existing_rows(tmp_path):
    stores, partners, brand_id = _database(tmp_path)
    partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="changed",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("1")),),
        ),
        actor_id=1,
        source_key="batch:changed",
    )
    archived = partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="archived",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("1")),),
        ),
        actor_id=1,
        source_key="batch:archived",
    )
    partners.delete_offer(archived.id, actor_id=2)
    with stores.transaction() as connection:
        connection.execute(
            "INSERT INTO partner_seed_tombstones(source_key,reason) VALUES(?,?)",
            ("batch:tombstoned", "reviewed removal"),
        )
    stores.apply_change(
        "add_merchant", {"name": "Duplicate Brand", "channel": "offline"}, actor_id=1
    )
    stores.apply_change(
        "add_merchant", {"name": "Duplicate Brand", "channel": "offline"}, actor_id=1
    )
    snapshot = _snapshot(
        _offer("batch:changed", brand_id, value="9", card_id="changed"),
        _offer("batch:archived", brand_id, card_id="archived"),
        _offer("batch:tombstoned", brand_id),
        _offer("batch:one", brand_id, card_id="duplicate"),
        _offer("batch:two", brand_id, card_id="duplicate"),
        {
            "source_key": "batch:missing",
            "brand": "No Such Brand",
            "aliases": [],
            "card_id": "missing",
            "channel": "offline",
            "mode": "total",
            "reward_kind": "cash",
            "tiers": [{"value": "2"}],
        },
        {
            "source_key": "batch:ambiguous",
            "brand": "Duplicate Brand",
            "aliases": [],
            "card_id": "ambiguous",
            "channel": "offline",
            "mode": "total",
            "reward_kind": "cash",
            "tiers": [{"value": "2"}],
        },
        problems=[{"source_key": "batch:problem", "reason": "collector rejected row"}],
    )

    report = preview_partner_batch(stores, snapshot)
    kinds = {item["kind"] for item in report["conflicts"]}
    assert {
        "changed",
        "archived",
        "tombstoned",
        "logical_duplicate",
        "missing_brand",
        "ambiguous_brand",
        "snapshot_problem",
    } <= kinds
    assert report["counts"]["offers_changed"] == 1
    assert report["counts"]["offers_archived"] == 1
    assert report["counts"]["offers_tombstoned"] == 1
    assert report["counts"]["offers_logical_duplicates"] == 2
    assert report["counts"]["offers_missing_brand"] == 1
    assert report["counts"]["offers_ambiguous_brand"] == 1
    assert report["counts"]["snapshot_problems"] == 1
    assert report["counts"]["approved_inserts"] == 0
    assert _offer_count(stores) == 2


def test_invalid_snapshot_version_is_rejected_before_database_access(tmp_path):
    with pytest.raises(PartnerBatchError, match="Snapshot version"):
        preview_partner_batch(
            tmp_path / "missing.sqlite3", {"version": 2, "offers": [], "exclusions": []}
        )


def test_partner_only_brand_creation_is_planned_and_mapped_atomically(tmp_path):
    stores, partners, _brand_id = _database(tmp_path)
    snapshot = _snapshot(
        {
            "source_key": "partner-only:offer",
            "brand_key": "partner-only:brand",
            "brand": "Partner Only Brand",
            "aliases": ["POB"],
            "card_id": "partner-card",
            "channel": "offline",
            "mode": "total",
            "reward_kind": "cash",
            "tiers": [{"value": "2.5"}],
        }
    )

    report = preview_partner_batch(stores, snapshot)
    assert report["counts"]["brands_new"] == 1
    assert report["counts"]["brand_mappings_new"] == 1
    assert report["counts"]["approved_partner_rows"] == 1
    assert report["counts"]["approved_inserts"] == 3
    assert report["conflicts"] == []
    assert stores.search("Partner Only Brand").matches == ()

    apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=report["plan_sha256"],
        backup_path=tmp_path / "partner-only-backup.sqlite3",
    )

    matches = stores.search("POB").matches
    assert len(matches) == 1
    assert matches[0].name == "Partner Only Brand"
    assert matches[0].aliases == ("POB",)
    assert len(partners.list_offers(matches[0].id, card_id="partner-card")) == 1
    with stores.connection() as connection:
        mapping = connection.execute(
            "SELECT brand_id FROM partner_seed_brands WHERE source_key=?",
            ("partner-only:brand",),
        ).fetchone()
    assert mapping["brand_id"] == matches[0].id


def test_multiple_partner_rows_reuse_one_planned_brand_and_replay_noop(tmp_path):
    stores, _partners, _brand_id = _database(tmp_path)
    common = {
        "brand_key": "grouped:brand",
        "brand": "Grouped Partner",
        "aliases": ["Grouped"],
        "channel": "offline",
        "mode": "total",
        "reward_kind": "cash",
    }
    snapshot = _snapshot(
        {**common, "source_key": "grouped:first", "card_id": "first", "tiers": [{"value": "1"}]},
        {**common, "source_key": "grouped:second", "card_id": "second", "tiers": [{"value": "2"}]},
    )
    report = preview_partner_batch(stores, snapshot)
    assert report["counts"]["brands_new"] == 1
    assert report["counts"]["brand_mappings_new"] == 1
    assert report["counts"]["approved_partner_rows"] == 2

    apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=report["plan_sha256"],
        backup_path=tmp_path / "grouped-before.sqlite3",
    )
    assert len(stores.search("Grouped Partner").matches) == 1
    replay = preview_partner_batch(stores, snapshot)
    assert replay["counts"]["brands_new"] == 0
    assert replay["counts"]["brand_mappings_new"] == 0
    assert replay["counts"]["offers_unchanged"] == 2
    result = apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=replay["plan_sha256"],
        backup_path=tmp_path / "grouped-replay.sqlite3",
    )
    assert result["inserted"] == {"offers": 0, "exclusions": 0}


def test_distinct_planned_brand_keys_with_same_payload_both_insert(tmp_path):
    stores, partners, _brand_id = _database(tmp_path)
    common = {
        "aliases": [],
        "card_id": "shared-card",
        "channel": "offline",
        "mode": "total",
        "reward_kind": "cash",
        "tiers": [{"value": "2"}],
    }
    snapshot = _snapshot(
        {
            **common,
            "source_key": "planned:cactus",
            "brand_key": "planned:brand:cactus",
            "brand": "Planned Cactus",
        },
        {
            **common,
            "source_key": "planned:paritet",
            "brand_key": "planned:brand:paritet",
            "brand": "Planned Paritet",
        },
    )

    report = preview_partner_batch(stores, snapshot)
    assert report["counts"]["offers_new"] == 2
    assert report["counts"]["offers_logical_duplicates"] == 0
    assert report["counts"]["offers_snapshot_logical_overlaps"] == 0
    assert report["counts"]["approved_partner_rows"] == 2
    assert {item["action"] for item in report["operations"] if item["kind"] == "offer"} == {
        "insert"
    }

    apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=report["plan_sha256"],
        backup_path=tmp_path / "distinct-planned-before.sqlite3",
    )
    for brand_name in ("Planned Cactus", "Planned Paritet"):
        matches = stores.search(brand_name).matches
        assert len(matches) == 1
        assert len(partners.list_offers(matches[0].id)) == 1


def test_same_planned_brand_key_still_holds_identical_payload_duplicates(tmp_path):
    stores, _partners, _brand_id = _database(tmp_path)
    common = {
        "brand_key": "planned:same-brand",
        "brand": "Same Planned Brand",
        "aliases": [],
        "card_id": "same-card",
        "channel": "offline",
        "mode": "total",
        "reward_kind": "cash",
        "tiers": [{"value": "2"}],
    }
    snapshot = _snapshot(
        {**common, "source_key": "planned:same:first"},
        {**common, "source_key": "planned:same:second"},
    )

    report = preview_partner_batch(stores, snapshot)
    offer_operations = [item for item in report["operations"] if item["kind"] == "offer"]
    assert report["counts"]["offers_logical_duplicates"] == 2
    assert report["counts"]["offers_snapshot_logical_overlaps"] == 0
    assert report["counts"]["approved_partner_rows"] == 0
    assert {item["status"] for item in offer_operations} == {"logical_duplicate"}
    assert {item["action"] for item in offer_operations} == {"none"}
    assert {
        item["source_key"] for item in report["conflicts"] if item["kind"] == "logical_duplicate"
    } == {"planned:same:first", "planned:same:second"}


def test_partner_only_creation_is_blocked_by_mcc_requirement_or_conflicting_mapping(tmp_path):
    stores, _partners, brand_id = _database(tmp_path)
    blocked_snapshot = _snapshot(
        {
            "source_key": "blocked:first",
            "brand_key": "blocked:brand",
            "brand": "Blocked Partner",
            "aliases": [],
            "card_id": "blocked-first",
            "channel": "offline",
            "mode": "total",
            "reward_kind": "cash",
            "tiers": [{"value": "1"}],
        },
        {
            "source_key": "blocked:second",
            "brand_key": "blocked:brand",
            "brand": "Blocked Partner",
            "aliases": [],
            "card_id": "blocked-second",
            "channel": "offline",
            "mode": "total",
            "reward_kind": "cash",
            "require_existing_mcc": True,
            "tiers": [{"value": "1"}],
        },
    )
    blocked = preview_partner_batch(stores, blocked_snapshot)
    assert blocked["counts"]["brands_new"] == 0
    assert blocked["counts"]["offers_missing_brand"] == 2
    assert blocked["counts"]["approved_inserts"] == 0

    candidate = stores.apply_change(
        "add_merchant", {"name": "Mapped Candidate", "channel": "offline"}, actor_id=1
    )
    assert candidate.brand_id is not None
    conflicting_snapshot = _snapshot(
        {
            "source_key": "conflict:offer",
            "brand_key": "conflict:brand",
            "brand": "Mapped Candidate",
            "aliases": [],
            "card_id": "conflict-card",
            "channel": "offline",
            "mode": "total",
            "reward_kind": "cash",
            "tiers": [{"value": "1"}],
        }
    )
    with stores.transaction() as connection:
        connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?)",
            ("conflict:brand", brand_id),
        )
    conflicting = preview_partner_batch(stores, conflicting_snapshot)
    assert conflicting["counts"]["offers_mapping_conflicts"] == 1
    assert conflicting["counts"]["approved_inserts"] == 0

    # A mapping written after a preview is a DB change and cannot be silently
    # replaced by the planned partner-only brand.
    snapshot = _snapshot(
        {
            "source_key": "stale:offer",
            "brand_key": "stale:brand",
            "brand": "Stale Partner",
            "aliases": [],
            "card_id": "stale-card",
            "channel": "offline",
            "mode": "total",
            "reward_kind": "cash",
            "tiers": [{"value": "1"}],
        }
    )
    plan = preview_partner_batch(stores, snapshot)
    with stores.transaction() as connection:
        connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?)",
            ("stale:brand", brand_id),
        )
    with pytest.raises(StalePartnerPlanError):
        apply_partner_batch(
            stores,
            snapshot,
            actor_id=1,
            expected_plan_sha256=plan["plan_sha256"],
            backup_path=tmp_path / "stale-mapping-backup.sqlite3",
        )
    assert not (tmp_path / "stale-mapping-backup.sqlite3").exists()


def test_imported_offer_overlap_is_a_deterministic_hold_and_manual_rows_are_preserved(tmp_path):
    # Partner-only brands may have no MCC facts; PartnerOffer still applies to
    # every MCC queried for the same brand/card.
    stores, partners, overlap_brand_id = _database(tmp_path)
    partners.create_offer(
        PartnerOfferInput(
            brand_id=overlap_brand_id,
            card_id="overlap-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("1")),),
            conditions="old conditions",
            source_url="https://old.example/offer",
        ),
        actor_id=1,
        source_key="old:offer",
    )
    imported = _offer("import:new", overlap_brand_id, value="2", card_id="overlap-card")
    imported.update(
        {
            "channel": "any",
            "conditions": "new conditions",
            "source_url": "https://new.example/offer",
            "mccs": ["5411"],
        }
    )

    first = preview_partner_batch(stores, _snapshot(imported))
    second = preview_partner_batch(stores, _snapshot(imported))
    assert first == second
    assert first["counts"]["offers_new"] == 1
    assert first["counts"]["offers_logical_overlaps"] == 1
    assert first["counts"]["approved_inserts"] == 0
    conflict = next(item for item in first["conflicts"] if item["kind"] == "logical_overlap")
    assert conflict["existing_source_key"] == "old:offer"
    assert conflict["mcc_scope"] == "all_brand_mccs"
    assert conflict["dimensions"] == [
        "source_identity",
        "source_url",
        "conditions",
        "channel",
        "rate_or_tiers",
    ]
    assert _offer_count(stores) == 1

    # A manually entered source-less row cannot be overwritten or silently
    # duplicated by an imported source identity.
    partners.create_offer(
        PartnerOfferInput(
            brand_id=overlap_brand_id,
            card_id="manual-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("3")),),
        ),
        actor_id=2,
    )
    manual_import = _offer("import:manual", overlap_brand_id, value="3", card_id="manual-card")
    manual_report = preview_partner_batch(stores, _snapshot(manual_import))
    manual_conflict = next(
        item for item in manual_report["conflicts"] if item["kind"] == "logical_overlap"
    )
    assert manual_conflict["existing_source_key"] is None
    assert manual_report["counts"]["approved_inserts"] == 0


def test_imported_offer_overlap_requires_mcc_and_date_channel_intersection(tmp_path):
    stores, partners, _brand_id = _database(tmp_path)
    result = stores.apply_change(
        "add_merchant",
        {"name": "Scoped Partner", "channel": "offline", "mcc": "5411"},
        actor_id=1,
    )
    assert result.brand_id is not None
    brand_id = result.brand_id
    partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="scoped-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("1")),),
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 1, 31),
        ),
        actor_id=1,
        source_key="scoped:old",
    )
    snapshot = _snapshot(
        {
            **_offer("scoped:outside", brand_id, value="2", card_id="scoped-card"),
            "starts_on": "2026-02-01",
            "ends_on": "2026-02-28",
            "mccs": ["5411"],
        },
        {
            **_offer("scoped:channel", brand_id, value="2", card_id="scoped-card"),
            "channel": "online",
            "mccs": ["5411"],
        },
    )
    report = preview_partner_batch(stores, snapshot)
    assert report["counts"]["offers_logical_overlaps"] == 0
    assert report["counts"]["offers_new"] == 2
    assert report["counts"]["approved_partner_rows"] == 2


def test_snapshot_overlap_holds_distinct_planned_izi_offers_by_brand_key(tmp_path):
    stores, _partners, _brand_id = _database(tmp_path)
    common = {
        "brand_key": "brand:izi",
        "brand": "IZI",
        "aliases": [],
        "card_id": "izi_card",
        "channel": "offline",
        "mode": "total",
        "reward_kind": "cash",
        "tiers": [{"value": "2"}],
    }
    snapshot = _snapshot(
        {**common, "source_key": "izi:one", "conditions": "first condition"},
        {**common, "source_key": "izi:two", "conditions": "second condition"},
    )

    report = preview_partner_batch(stores, snapshot)

    assert report["counts"]["offers_snapshot_logical_overlaps"] == 2
    assert report["counts"]["approved_partner_rows"] == 0
    conflicts = [item for item in report["conflicts"] if item["kind"] == "snapshot_logical_overlap"]
    assert [item["source_key"] for item in conflicts] == ["izi:one", "izi:two"]
    assert conflicts[0]["references"] == ["izi:two"]
    assert conflicts[1]["references"] == ["izi:one"]


def test_snapshot_overlap_allows_non_overlapping_channel_or_date(tmp_path):
    stores, _partners, brand_id = _database(tmp_path)
    snapshot = _snapshot(
        {
            **_offer("safe:offline-jan", brand_id, card_id="safe-card"),
            "starts_on": "2026-01-01",
            "ends_on": "2026-01-31",
        },
        {
            **_offer("safe:online-jan", brand_id, card_id="safe-card"),
            "channel": "online",
            "starts_on": "2026-01-01",
            "ends_on": "2026-01-31",
        },
        {
            **_offer("safe:offline-feb", brand_id, card_id="safe-card"),
            "starts_on": "2026-02-01",
            "ends_on": "2026-02-28",
        },
    )

    report = preview_partner_batch(stores, snapshot)

    assert report["counts"]["offers_snapshot_logical_overlaps"] == 0
    assert report["counts"]["approved_partner_rows"] == 3
    assert not any(item["kind"] == "snapshot_logical_overlap" for item in report["conflicts"])


def test_planned_brand_groups_offline_and_online_offers_and_replays(tmp_path):
    stores, partners, _brand_id = _database(tmp_path)
    common = {
        "brand_key": "brand:inglot-196588",
        "brand": "Inglot",
        "aliases": [],
        "card_id": "bnb_1_2_3",
        "mode": "total",
        "reward_kind": "cash",
    }
    snapshot = _snapshot(
        {
            **common,
            "source_key": "bnb-1-2-3:196588:offline",
            "channel": "offline",
            "tiers": [{"value": "3"}],
        },
        {
            **common,
            "source_key": "bnb-1-2-3:196588:online",
            "channel": "online",
            "tiers": [{"value": "5"}],
        },
    )

    report = preview_partner_batch(stores, snapshot)
    assert report["counts"]["brands_new"] == 1
    assert report["counts"]["brand_mappings_new"] == 1
    assert report["counts"]["offers_new"] == 2
    assert report["counts"]["offers_snapshot_logical_overlaps"] == 0
    assert report["counts"]["approved_partner_rows"] == 2

    apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=report["plan_sha256"],
        backup_path=tmp_path / "inglot-before.sqlite3",
    )
    matches = stores.search("Inglot").matches
    assert len(matches) == 1
    assert {offer.channel for offer in partners.list_offers(matches[0].id)} == {
        "offline",
        "online",
    }

    replay = preview_partner_batch(stores, snapshot)
    assert replay["counts"]["brands_new"] == 0
    assert replay["counts"]["brand_mappings_new"] == 0
    assert replay["counts"]["offers_unchanged"] == 2
    replay_result = apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=replay["plan_sha256"],
        backup_path=tmp_path / "inglot-replay.sqlite3",
    )
    assert replay_result["inserted"] == {"offers": 0, "exclusions": 0}


def test_exact_offer_retirement_allows_replacement_and_replays_noop(tmp_path):
    stores, partners, brand_id = _database(tmp_path)
    legacy = _offer("legacy:offer", brand_id, value="1", card_id="replace-card")
    legacy.update(
        {
            "starts_on": "2026-01-01",
            "ends_on": "2026-12-31",
            "conditions": "legacy terms",
            "source_url": "https://legacy.example/offer",
        }
    )
    partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="replace-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("1")),),
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
            conditions="legacy terms",
            source_url="https://legacy.example/offer",
        ),
        actor_id=1,
        source_key="legacy:offer",
    )
    replacement = _offer("current:offer", brand_id, value="2", card_id="replace-card")
    replacement.update(
        {
            "starts_on": "2026-01-01",
            "ends_on": "2026-12-31",
            "conditions": "current terms",
            "source_url": "https://current.example/offer",
        }
    )
    snapshot = _snapshot(replacement)
    snapshot["offer_retirements"] = [legacy]

    report = preview_partner_batch(stores, snapshot)
    retirement = next(item for item in report["operations"] if item["kind"] == "offer_retirement")
    assert retirement["status"] == "retire"
    assert retirement["action"] == "archive"
    assert report["counts"]["offers_new"] == 1
    assert report["counts"]["offers_logical_overlaps"] == 0
    assert report["counts"]["approved_archives"] == 1
    assert report["counts"]["approved_inserts"] == 1
    assert report["counts"]["approved_operations"] == 2

    result = apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=report["plan_sha256"],
        backup_path=tmp_path / "retirement-before.sqlite3",
    )
    assert result["archived"] == {"offers": 1}
    assert result["inserted"] == {"offers": 1, "exclusions": 0}
    with stores.connection() as connection:
        rows = connection.execute(
            "SELECT source_key,archived FROM partner_offers ORDER BY source_key"
        ).fetchall()
        assert [(row["source_key"], row["archived"]) for row in rows] == [
            ("current:offer", 0),
            ("legacy:offer", 1),
        ]
        assert (
            connection.execute(
                "SELECT action FROM partner_audit WHERE entity_type='offer' "
                "AND entity_id=(SELECT id FROM partner_offers WHERE source_key='legacy:offer') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()["action"]
            == "delete"
        )

    replay = preview_partner_batch(stores, snapshot)
    replay_retirement = next(
        item for item in replay["operations"] if item["kind"] == "offer_retirement"
    )
    assert replay_retirement["status"] == "already_archived"
    assert replay_retirement["action"] == "none"
    assert replay["counts"]["approved_archives"] == 0
    assert replay["counts"]["approved_inserts"] == 0
    assert replay["counts"]["approved_operations"] == 0
    replay_result = apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=replay["plan_sha256"],
        backup_path=tmp_path / "retirement-replay.sqlite3",
    )
    assert replay_result["archived"] == {"offers": 0}
    assert replay_result["inserted"] == {"offers": 0, "exclusions": 0}
    assert len(partners.list_offers(brand_id)) == 1


def test_exact_offer_retirement_holds_when_offer_has_active_linked_exclusion(tmp_path):
    stores, partners, brand_id = _database(tmp_path)
    legacy = _offer(
        "legacy:linked-retirement", brand_id, value="1", card_id="linked-retirement-card"
    )
    offer = partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="linked-retirement-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("1")),),
        ),
        actor_id=1,
        source_key="legacy:linked-retirement",
    )
    partners.create_exclusion(
        PartnerExclusionInput(
            brand_id=brand_id,
            card_id="linked-retirement-card",
            reward_kind="cash",
            channel="offline",
            mcc="5411",
            suppress_base=True,
        ),
        actor_id=1,
        offer_id=offer.id,
    )

    snapshot = _snapshot()
    snapshot["offer_retirements"] = [legacy]
    report = preview_partner_batch(stores, snapshot)
    retirement = next(item for item in report["operations"] if item["kind"] == "offer_retirement")
    assert (retirement["status"], retirement["action"]) == ("linked_exclusions", "none")
    assert report["counts"]["approved_archives"] == 0
    assert any(
        item["kind"] == "retirement_conflict"
        and item["reason"] == "guarded offer has active linked exclusions"
        for item in report["conflicts"]
    )


def test_safe_retirement_is_removed_from_duplicate_signature_checks(tmp_path):
    stores, partners, brand_id = _database(tmp_path)
    legacy = _offer("legacy:signature", brand_id, value="1", card_id="signature-card")
    partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="signature-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("1")),),
        ),
        actor_id=1,
        source_key="legacy:signature",
    )
    replacement = dict(legacy)
    replacement["source_key"] = "current:signature"
    snapshot = _snapshot(replacement)
    snapshot["offer_retirements"] = [legacy]

    report = preview_partner_batch(stores, snapshot)
    assert report["counts"]["offers_logical_duplicates"] == 0
    assert report["counts"]["offers_logical_overlaps"] == 0
    assert report["counts"]["approved_archives"] == 1
    assert report["counts"]["approved_inserts"] == 1
    assert report["counts"]["approved_operations"] == 2
    assert not any(item["kind"] == "logical_duplicate" for item in report["conflicts"])


def test_changed_legacy_offer_is_preserved_and_blocks_replacement(tmp_path):
    stores, partners, brand_id = _database(tmp_path)
    partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="changed-legacy",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("1")),),
        ),
        actor_id=1,
        source_key="legacy:changed",
    )
    expected = _offer("legacy:changed", brand_id, value="9", card_id="changed-legacy")
    replacement = _offer("current:changed", brand_id, value="2", card_id="changed-legacy")
    snapshot = _snapshot(replacement)
    snapshot["offer_retirements"] = [expected]

    report = preview_partner_batch(stores, snapshot)
    retirement = next(item for item in report["operations"] if item["kind"] == "offer_retirement")
    replacement_operation = next(
        item
        for item in report["operations"]
        if item["kind"] == "offer" and item["source_key"] == "current:changed"
    )
    assert retirement["status"] == "changed"
    assert retirement["action"] == "none"
    assert replacement_operation["status"] == "logical_overlap"
    assert replacement_operation["action"] == "none"
    assert report["counts"]["approved_archives"] == 0
    assert report["counts"]["approved_inserts"] == 0
    assert report["counts"]["approved_operations"] == 0
    assert any(item["kind"] == "retirement_conflict" for item in report["conflicts"])
    assert report["counts"]["offers_logical_overlaps"] == 1
    with stores.connection() as connection:
        assert (
            connection.execute(
                "SELECT archived FROM partner_offers WHERE source_key=?",
                ("legacy:changed",),
            ).fetchone()["archived"]
            == 0
        )
    assert len(partners.list_offers(brand_id)) == 1


def test_missing_and_already_archived_retirements_are_deterministic_noops(tmp_path):
    stores, partners, brand_id = _database(tmp_path)
    archived = _offer("legacy:archived", brand_id, value="1", card_id="archived-card")
    partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="archived-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("1")),),
        ),
        actor_id=1,
        source_key="legacy:archived",
    )
    archived_id = partners.list_offers(brand_id, include_archived=True)[0].id
    assert partners.delete_offer(archived_id, actor_id=2)
    missing = _offer("legacy:missing", brand_id, value="3", card_id="missing-card")
    snapshot = _snapshot()
    snapshot["offer_retirements"] = [missing, archived]

    first = preview_partner_batch(stores, snapshot)
    second = preview_partner_batch(stores, snapshot)
    assert first == second
    statuses = {
        item["source_key"]: (item["status"], item["action"])
        for item in first["operations"]
        if item["kind"] == "offer_retirement"
    }
    assert statuses == {
        "legacy:archived": ("already_archived", "none"),
        "legacy:missing": ("missing", "none"),
    }
    assert first["counts"]["approved_archives"] == 0
    assert first["counts"]["approved_inserts"] == 0
    assert first["counts"]["approved_operations"] == 0
    assert not any(item["kind"] == "retirement_conflict" for item in first["conflicts"])
    assert len(partners.list_offers(brand_id)) == 0


def test_retirement_archive_rolls_back_when_replacement_insert_fails(tmp_path, monkeypatch):
    stores, partners, brand_id = _database(tmp_path)
    legacy = _offer("legacy:atomic", brand_id, value="1", card_id="atomic-card")
    partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="atomic-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("1")),),
        ),
        actor_id=1,
        source_key="legacy:atomic",
    )
    snapshot = _snapshot(
        _offer("replacement:first", brand_id, value="2", card_id="atomic-card"),
        _offer("replacement:second", brand_id, value="3", card_id="other-card"),
    )
    snapshot["offer_retirements"] = [legacy]
    plan = preview_partner_batch(stores, snapshot)
    assert plan["counts"]["approved_archives"] == 1
    assert plan["counts"]["approved_inserts"] == 2
    original = PartnerRepository.create_offer
    calls = 0

    def fail_on_second(self, payload, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated replacement failure")
        return original(self, payload, **kwargs)

    monkeypatch.setattr(PartnerRepository, "create_offer", fail_on_second)
    with pytest.raises(RuntimeError, match="simulated replacement failure"):
        apply_partner_batch(
            stores,
            snapshot,
            actor_id=1,
            expected_plan_sha256=plan["plan_sha256"],
            backup_path=tmp_path / "atomic-retirement-backup.sqlite3",
        )

    with stores.connection() as connection:
        assert (
            connection.execute(
                "SELECT archived FROM partner_offers WHERE source_key=?",
                ("legacy:atomic",),
            ).fetchone()["archived"]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM partner_offers WHERE source_key LIKE 'replacement:%'"
            ).fetchone()[0]
            == 0
        )
    assert len(partners.list_offers(brand_id)) == 1


def test_retirement_duplicates_and_offer_key_collision_fail_closed(tmp_path):
    stores, _partners, brand_id = _database(tmp_path)
    legacy = _offer("legacy:collision", brand_id)
    snapshot = _snapshot(_offer("legacy:collision", brand_id, value="2"))
    snapshot["offer_retirements"] = [legacy, dict(legacy)]

    report = preview_partner_batch(stores, snapshot)
    retirement_operations = [
        item for item in report["operations"] if item["kind"] == "offer_retirement"
    ]
    offer_operation = next(item for item in report["operations"] if item["kind"] == "offer")
    assert len(retirement_operations) == 2
    assert {item["status"] for item in retirement_operations} == {"source_key_conflict"}
    assert all(item["action"] == "none" for item in retirement_operations)
    assert offer_operation["status"] == "source_key_conflict"
    assert offer_operation["action"] == "none"
    assert report["counts"]["approved_archives"] == 0
    assert report["counts"]["approved_inserts"] == 0
    assert report["counts"]["approved_operations"] == 0
    assert sum(item["kind"] == "source_key_conflict" for item in report["conflicts"]) >= 1


def test_offer_retirements_must_be_a_list(tmp_path):
    stores, _partners, brand_id = _database(tmp_path)
    snapshot = _snapshot(_offer("invalid:retirements", brand_id))
    snapshot["offer_retirements"] = {}

    with pytest.raises(PartnerBatchError, match="offer_retirements must be a list"):
        preview_partner_batch(stores, snapshot)


def test_structural_collision_blocks_malformed_retirement_before_validation(tmp_path):
    stores, _partners, brand_id = _database(tmp_path)
    snapshot = _snapshot(_offer("shared:source", brand_id))
    snapshot["offer_retirements"] = [{"source_key": " shared:source "}]
    original = copy.deepcopy(snapshot)

    report = preview_partner_batch(stores, snapshot)
    operations = [item for item in report["operations"] if item["source_key"] == "shared:source"]
    assert len(operations) == 2
    assert {item["status"] for item in operations} == {"source_key_conflict"}
    assert {item["action"] for item in operations} == {"none"}
    assert report["counts"]["source_key_conflicts"] == 1
    assert any(item["kind"] == "source_key_conflict" for item in report["conflicts"])
    assert snapshot == original


def test_padded_source_and_brand_keys_apply_and_replay_without_mutating_snapshot(tmp_path):
    stores, partners, _brand_id = _database(tmp_path)
    snapshot = _snapshot(
        {
            "source_key": "  padded:offer  ",
            "brand_key": "  padded:brand  ",
            "brand": "Padded Planned Brand",
            "aliases": ["Padded"],
            "card_id": "padded-card",
            "channel": "offline",
            "mode": "total",
            "reward_kind": "cash",
            "tiers": [{"value": "2"}],
        }
    )
    original = copy.deepcopy(snapshot)

    report = preview_partner_batch(stores, snapshot)
    offer = next(item for item in report["operations"] if item["kind"] == "offer")
    assert offer["source_key"] == "padded:offer"
    assert offer["brand_key"] == "padded:brand"
    assert snapshot == original

    apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=report["plan_sha256"],
        backup_path=tmp_path / "padded-before.sqlite3",
    )
    matches = stores.search("Padded").matches
    assert len(matches) == 1
    assert len(partners.list_offers(matches[0].id)) == 1

    replay = preview_partner_batch(stores, snapshot)
    assert replay["counts"]["offers_unchanged"] == 1
    assert replay["counts"]["brands_new"] == 0
    assert replay["counts"]["brand_mappings_new"] == 0
    replay_result = apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=replay["plan_sha256"],
        backup_path=tmp_path / "padded-replay.sqlite3",
    )
    assert replay_result["inserted"] == {"offers": 0, "exclusions": 0}
    assert snapshot == original


def test_planned_offer_and_exclusion_payloads_are_fully_validated_at_preview(tmp_path):
    stores, _partners, _brand_id = _database(tmp_path)
    invalid_offer = {
        "source_key": "invalid:planned:offer",
        "brand_key": "invalid:planned:offer:brand",
        "brand": "Invalid Planned Offer",
        "aliases": [],
        "card_id": "invalid-offer-card",
        "channel": "not-a-channel",
        "mode": "total",
        "reward_kind": "cash",
        "tiers": [{"value": "2"}],
    }
    invalid_exclusion = {
        "source_key": "invalid:planned:exclusion",
        "brand_key": "invalid:planned:exclusion:brand",
        "brand": "Invalid Planned Exclusion",
        "aliases": [],
        "card_id": "invalid-exclusion-card",
        "channel": "not-a-channel",
        "reward_kind": "cash",
    }

    report = preview_partner_batch(
        stores,
        _snapshot(invalid_offer, exclusions=[invalid_exclusion]),
    )
    assert report["counts"]["offers_snapshot_problems"] == 1
    assert report["counts"]["exclusions_snapshot_problems"] == 1
    assert report["counts"]["brands_new"] == 0
    assert report["counts"]["approved_operations"] == 0
    assert report["operations"] == []
    assert sum(item["kind"] == "snapshot_problem" for item in report["conflicts"]) == 2


def test_distinct_planned_exclusion_keys_are_independent(tmp_path):
    stores, partners, _brand_id = _database(tmp_path)
    common = {
        "aliases": [],
        "card_id": "planned-exclusion-card",
        "channel": "offline",
        "reward_kind": "cash",
        "mcc": "5411",
        "suppress_base": True,
    }
    snapshot = _snapshot(
        exclusions=[
            {
                **common,
                "source_key": "planned:exclusion:cactus",
                "brand_key": "planned:exclusion:brand:cactus",
                "brand": "Planned Exclusion Cactus",
            },
            {
                **common,
                "source_key": "planned:exclusion:paritet",
                "brand_key": "planned:exclusion:brand:paritet",
                "brand": "Planned Exclusion Paritet",
            },
        ]
    )

    report = preview_partner_batch(stores, snapshot)
    assert report["counts"]["exclusions_new"] == 2
    assert report["counts"]["exclusions_logical_duplicates"] == 0
    assert report["counts"]["approved_partner_rows"] == 2
    assert {item["action"] for item in report["operations"] if item["kind"] == "exclusion"} == {
        "insert"
    }

    apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=report["plan_sha256"],
        backup_path=tmp_path / "planned-exclusions-before.sqlite3",
    )
    for brand_name in ("Planned Exclusion Cactus", "Planned Exclusion Paritet"):
        matches = stores.search(brand_name).matches
        assert len(matches) == 1
        assert len(partners.list_exclusions(matches[0].id)) == 1


def test_global_exclusions_with_no_brand_key_still_dedupe(tmp_path):
    stores, _partners, _brand_id = _database(tmp_path)
    common = {
        "card_id": "global-exclusion-card",
        "channel": "offline",
        "reward_kind": "cash",
        "mcc": "5411",
        "suppress_base": True,
    }
    snapshot = _snapshot(
        exclusions=[
            {**common, "source_key": "global:exclusion:first"},
            {**common, "source_key": "global:exclusion:second"},
        ]
    )

    report = preview_partner_batch(stores, snapshot)
    assert report["counts"]["exclusions_logical_duplicates"] == 2
    assert report["counts"]["approved_inserts"] == 0
    assert report["counts"]["approved_operations"] == 0
    assert {item["status"] for item in report["operations"] if item["kind"] == "exclusion"} == {
        "logical_duplicate"
    }


def test_cross_kind_source_key_collision_blocks_all_actions(tmp_path):
    stores, _partners, brand_id = _database(tmp_path)
    source_key = "cross:kind"
    snapshot = _snapshot(
        _offer(source_key, brand_id),
        exclusions=[
            {
                "source_key": source_key,
                "brand_id": brand_id,
                "card_id": "cross-exclusion-card",
                "channel": "offline",
                "reward_kind": "cash",
                "mcc": "5411",
                "suppress_base": True,
            }
        ],
    )
    snapshot["offer_retirements"] = [{"source_key": source_key}]

    report = preview_partner_batch(stores, snapshot)
    operations = [item for item in report["operations"] if item["source_key"] == source_key]
    assert len(operations) == 3
    assert {item["status"] for item in operations} == {"source_key_conflict"}
    assert {item["action"] for item in operations} == {"none"}
    assert report["counts"]["source_key_conflicts"] == 1
    assert report["counts"]["approved_operations"] == 0


def test_partial_backup_is_removed_when_backup_fails(tmp_path):
    stores, _partners, _brand_id = _database(tmp_path)
    backup = tmp_path / "partial-backup.sqlite3"

    class FailingSource:
        def backup(self, destination):
            destination.execute("CREATE TABLE partial (id INTEGER)")
            raise RuntimeError("simulated backup failure")

    with pytest.raises(RuntimeError, match="simulated backup failure"):
        _backup_database(stores, FailingSource(), "partial", backup)
    assert not backup.exists()


def test_invalid_backup_is_removed_after_integrity_validation(tmp_path):
    stores, _partners, _brand_id = _database(tmp_path)
    backup = tmp_path / "invalid-backup.sqlite3"

    class InvalidSource:
        def backup(self, destination):
            destination.execute("PRAGMA foreign_keys=OFF")
            destination.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            destination.execute("CREATE TABLE child (parent_id INTEGER REFERENCES parent(id))")
            destination.execute("INSERT INTO child(parent_id) VALUES (99)")

    with pytest.raises(PartnerBatchError, match="foreign_key_check"):
        _backup_database(stores, InvalidSource(), "invalid", backup)
    assert not backup.exists()


def test_manual_source_less_retirement_replaces_exactly_and_replays_noop(tmp_path):
    stores, partners, brand_id = _database(tmp_path)
    manual = partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="manual-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("2")),),
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
            conditions="manual terms",
            source_url="https://manual.example/offer",
        ),
        actor_id=1,
    )
    replacement = _offer("official:replacement", brand_id, value="3", card_id="manual-card")
    replacement.update(
        {
            "starts_on": "2026-01-01",
            "ends_on": "2026-12-31",
            "conditions": "official terms",
            "source_url": "https://official.example/offer",
        }
    )
    snapshot = _snapshot(replacement)
    snapshot["manual_offer_retirements"] = [
        {"offer_id": manual.id, "expected_offer": _offer_guard(manual)}
    ]

    report = preview_partner_batch(stores, snapshot)
    retirement = next(
        item for item in report["operations"] if item["kind"] == "manual_offer_retirement"
    )
    assert (retirement["status"], retirement["action"]) == ("retire", "archive")
    assert report["counts"]["approved_archives"] == 1
    assert report["counts"]["approved_inserts"] == 1
    assert report["counts"]["approved_operations"] == 2

    result = apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=report["plan_sha256"],
        backup_path=tmp_path / "manual-before.sqlite3",
    )
    assert result["archived"] == {"offers": 1}
    assert result["inserted"] == {"offers": 1, "exclusions": 0}
    with stores.connection() as connection:
        rows = connection.execute(
            "SELECT source_key,archived FROM partner_offers ORDER BY id"
        ).fetchall()
    assert [(row["source_key"], row["archived"]) for row in rows] == [
        (None, 1),
        ("official:replacement", 0),
    ]

    replay = preview_partner_batch(stores, snapshot)
    replay_retirement = next(
        item for item in replay["operations"] if item["kind"] == "manual_offer_retirement"
    )
    assert (replay_retirement["status"], replay_retirement["action"]) == (
        "already_archived",
        "none",
    )
    assert replay["counts"]["approved_operations"] == 0
    replay_result = apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=replay["plan_sha256"],
        backup_path=tmp_path / "manual-replay.sqlite3",
    )
    assert replay_result["archived"] == {"offers": 0}
    assert replay_result["inserted"] == {"offers": 0, "exclusions": 0}


def test_manual_source_less_retirement_fails_closed_and_rolls_back_on_insert_failure(
    tmp_path, monkeypatch
):
    stores, partners, brand_id = _database(tmp_path)
    manual = partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="manual-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("2")),),
        ),
        actor_id=1,
    )
    replacement = _offer("official:replacement", brand_id, card_id="manual-card")
    snapshot = _snapshot(replacement)
    snapshot["manual_offer_retirements"] = [
        {"offer_id": manual.id, "expected_offer": _offer_guard(manual)}
    ]
    changed = copy.deepcopy(snapshot)
    changed["manual_offer_retirements"][0]["expected_offer"]["conditions"] = "changed"
    changed_report = preview_partner_batch(stores, changed)
    assert changed_report["counts"]["approved_operations"] == 0
    assert any(
        item["kind"] == "manual_offer_retirement_conflict" for item in changed_report["conflicts"]
    )

    report = preview_partner_batch(stores, snapshot)
    original = PartnerRepository.create_offer

    def fail_insert(self, payload, **kwargs):
        raise RuntimeError("simulated official replacement failure")

    monkeypatch.setattr(PartnerRepository, "create_offer", fail_insert)
    with pytest.raises(RuntimeError, match="official replacement failure"):
        apply_partner_batch(
            stores,
            snapshot,
            actor_id=1,
            expected_plan_sha256=report["plan_sha256"],
            backup_path=tmp_path / "manual-atomic.sqlite3",
        )
    monkeypatch.setattr(PartnerRepository, "create_offer", original)
    with stores.connection() as connection:
        assert (
            connection.execute(
                "SELECT archived FROM partner_offers WHERE id=?", (manual.id,)
            ).fetchone()["archived"]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM partner_offers WHERE source_key=?", ("official:replacement",)
            ).fetchone()[0]
            == 0
        )


def test_manual_retirement_holds_when_guarded_offer_has_active_linked_exclusion(tmp_path):
    stores, partners, brand_id = _database(tmp_path)
    manual = partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="manual-linked-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("2")),),
        ),
        actor_id=1,
    )
    partners.create_exclusion(
        PartnerExclusionInput(
            brand_id=brand_id,
            card_id="manual-linked-card",
            reward_kind="cash",
            channel="offline",
            mcc="5411",
            suppress_base=True,
        ),
        actor_id=1,
        offer_id=manual.id,
    )
    snapshot = _snapshot()
    snapshot["manual_offer_retirements"] = [
        {"offer_id": manual.id, "expected_offer": _offer_guard(manual)}
    ]

    report = preview_partner_batch(stores, snapshot)
    operation = next(
        item for item in report["operations"] if item["kind"] == "manual_offer_retirement"
    )
    assert (operation["status"], operation["action"]) == ("linked_exclusions", "none")
    assert report["counts"]["approved_operations"] == 0
    assert any(
        item["reason"] == "guarded offer has active linked exclusions"
        for item in report["conflicts"]
    )


def test_tombstone_guard_holds_linked_offer_and_missing_guarded_source(tmp_path):
    stores, partners, brand_id = _database(tmp_path)
    active = partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="tombstone-linked-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("2")),),
        ),
        actor_id=1,
        source_key="cashalot:linked",
    )
    partners.create_exclusion(
        PartnerExclusionInput(
            brand_id=brand_id,
            card_id="tombstone-linked-card",
            reward_kind="cash",
            channel="offline",
            mcc="5411",
            suppress_base=True,
        ),
        actor_id=1,
        offer_id=active.id,
    )
    linked_snapshot = _snapshot()
    linked_snapshot["partner_seed_tombstones"] = [
        {
            "source_key": "cashalot:linked",
            "reason": "removed",
            "offer_id": active.id,
            "expected_offer": _offer_guard(active),
        }
    ]
    linked_report = preview_partner_batch(stores, linked_snapshot)
    linked_operation = next(
        item for item in linked_report["operations"] if item["kind"] == "partner_seed_tombstone"
    )
    assert (linked_operation["status"], linked_operation["action"]) == (
        "linked_exclusions",
        "none",
    )

    missing_snapshot = _snapshot()
    missing_snapshot["partner_seed_tombstones"] = [
        {
            "source_key": "cashalot:missing",
            "reason": "removed",
            "offer_id": active.id,
            "expected_offer": _offer_guard(active),
        }
    ]
    missing_report = preview_partner_batch(stores, missing_snapshot)
    missing_operation = next(
        item for item in missing_report["operations"] if item["kind"] == "partner_seed_tombstone"
    )
    assert (missing_operation["status"], missing_operation["action"]) == (
        "missing_guarded_offer",
        "none",
    )
    assert any(
        item["reason"] == "guarded partner offer does not exist for the source key"
        for item in missing_report["conflicts"]
    )

    same_reason_snapshot = _snapshot()
    same_reason_snapshot["partner_seed_tombstones"] = [
        {
            "source_key": "cashalot:missing",
            "reason": "removed",
            "offer_id": active.id,
            "expected_offer": _offer_guard(active),
        }
    ]
    with stores.transaction() as connection:
        connection.execute(
            "INSERT INTO partner_seed_tombstones(source_key,reason) VALUES(?,?)",
            ("cashalot:missing", "removed"),
        )
    same_reason_report = preview_partner_batch(stores, same_reason_snapshot)
    same_reason_operation = next(
        item
        for item in same_reason_report["operations"]
        if item["kind"] == "partner_seed_tombstone"
    )
    assert (same_reason_operation["status"], same_reason_operation["action"]) == (
        "missing_guarded_offer",
        "none",
    )
    assert any(
        item["reason"] == "guarded partner offer does not exist for the source key"
        for item in same_reason_report["conflicts"]
    )

    unguarded_snapshot = _snapshot()
    unguarded_snapshot["partner_seed_tombstones"] = [
        {"source_key": "cashalot:missing", "reason": "removed"}
    ]
    unguarded_report = preview_partner_batch(stores, unguarded_snapshot)
    unguarded_operation = next(
        item for item in unguarded_report["operations"] if item["kind"] == "partner_seed_tombstone"
    )
    assert (unguarded_operation["status"], unguarded_operation["action"]) == (
        "already_tombstoned",
        "none",
    )


def test_partner_seed_tombstone_requires_guard_for_active_offer_and_replays_noop(tmp_path):
    stores, partners, brand_id = _database(tmp_path)
    active = partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="tombstone-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("2")),),
        ),
        actor_id=1,
        source_key="cashalot:active",
    )
    unguarded = _snapshot()
    unguarded["partner_seed_tombstones"] = [{"source_key": "cashalot:active", "reason": "removed"}]
    blocked = preview_partner_batch(stores, unguarded)
    assert blocked["counts"]["approved_operations"] == 0
    assert any(item["kind"] == "tombstone_conflict" for item in blocked["conflicts"])

    snapshot = _snapshot()
    snapshot["partner_seed_tombstones"] = [
        {
            "source_key": "cashalot:active",
            "reason": "removed",
            "offer_id": active.id,
            "expected_offer": _offer_guard(active),
        }
    ]
    report = preview_partner_batch(stores, snapshot)
    operation = next(
        item for item in report["operations"] if item["kind"] == "partner_seed_tombstone"
    )
    assert (operation["status"], operation["action"]) == ("retire", "archive_and_tombstone")
    result = apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=report["plan_sha256"],
        backup_path=tmp_path / "tombstone-before.sqlite3",
    )
    assert result["archived"] == {"offers": 1}
    assert result["tombstoned"] == 1
    assert stores.is_partner_seed_tombstoned("cashalot:active")

    replay = preview_partner_batch(stores, snapshot)
    replay_operation = next(
        item for item in replay["operations"] if item["kind"] == "partner_seed_tombstone"
    )
    assert (replay_operation["status"], replay_operation["action"]) == (
        "already_tombstoned",
        "none",
    )
    assert replay["counts"]["approved_operations"] == 0


def test_partner_seed_tombstone_archived_offer_is_tombstone_only(tmp_path):
    stores, partners, brand_id = _database(tmp_path)
    archived = partners.create_offer(
        PartnerOfferInput(
            brand_id=brand_id,
            card_id="tombstone-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("2")),),
        ),
        actor_id=1,
        source_key="cashalot:archived",
    )
    assert partners.delete_offer(archived.id, actor_id=1)
    snapshot = _snapshot()
    snapshot["partner_seed_tombstones"] = [{"source_key": "cashalot:archived", "reason": "removed"}]
    report = preview_partner_batch(stores, snapshot)
    operation = next(
        item for item in report["operations"] if item["kind"] == "partner_seed_tombstone"
    )
    assert (operation["status"], operation["action"]) == ("already_archived", "tombstone")
    apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=report["plan_sha256"],
        backup_path=tmp_path / "archived-tombstone.sqlite3",
    )
    assert stores.is_partner_seed_tombstoned("cashalot:archived")


def test_partner_mapping_repair_rebinds_exact_offer_and_replays_noop(tmp_path):
    stores, partners, _brand_id = _database(tmp_path)
    source = stores.apply_change(
        "add_merchant", {"name": "Archived Helix", "channel": "offline"}, actor_id=1
    )
    target = stores.apply_change(
        "add_merchant", {"name": "Active Helix", "channel": "offline"}, actor_id=1
    )
    assert source.brand_id is not None and target.brand_id is not None
    stores.apply_change(
        "merge_brand", {"brand_id": source.brand_id, "target_id": target.brand_id}, actor_id=1
    )
    offer = partners.create_offer(
        PartnerOfferInput(
            brand_id=source.brand_id,
            card_id="helix-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("2")),),
            conditions="unchanged terms",
            source_url="https://helix.example/offer",
        ),
        actor_id=1,
        source_key="helix:offer",
    )
    with stores.transaction() as connection:
        cursor = connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?)",
            ("helix:brand", source.brand_id),
        )
        mapping_id = cursor.lastrowid
    snapshot = _snapshot()
    snapshot["partner_mapping_repairs"] = [
        {
            "source_key": "helix:brand",
            "mapping_id": mapping_id,
            "expected_brand_id": source.brand_id,
            "target_brand_id": target.brand_id,
            "offer_id": offer.id,
            "offer_source_key": "helix:offer",
            "expected_offer": _offer_guard(offer),
        }
    ]

    report = preview_partner_batch(stores, snapshot)
    operation = next(
        item for item in report["operations"] if item["kind"] == "partner_mapping_repair"
    )
    assert (operation["status"], operation["action"]) == ("repair", "repair")
    assert report["counts"]["approved_repairs"] == 1
    assert report["counts"]["approved_operations"] == 1
    result = apply_partner_batch(
        stores,
        snapshot,
        actor_id=1,
        expected_plan_sha256=report["plan_sha256"],
        backup_path=tmp_path / "mapping-before.sqlite3",
    )
    assert result["repaired"] == {"mappings": 1, "offers": 1}
    with stores.connection() as connection:
        assert (
            connection.execute(
                "SELECT brand_id FROM partner_seed_brands WHERE id=?", (mapping_id,)
            ).fetchone()["brand_id"]
            == target.brand_id
        )
        assert (
            connection.execute(
                "SELECT brand_id FROM partner_offers WHERE id=?", (offer.id,)
            ).fetchone()["brand_id"]
            == target.brand_id
        )
        assert (
            connection.execute(
                "SELECT action FROM partner_audit WHERE entity_type='offer' AND entity_id=? "
                "ORDER BY id DESC LIMIT 1",
                (offer.id,),
            ).fetchone()["action"]
            == "rebind"
        )
        assert (
            connection.execute(
                "SELECT action FROM partner_audit WHERE entity_type='brand_mapping' "
                "AND entity_id=? "
                "ORDER BY id DESC LIMIT 1",
                (mapping_id,),
            ).fetchone()["action"]
            == "rebind"
        )

    replay = preview_partner_batch(stores, snapshot)
    replay_operation = next(
        item for item in replay["operations"] if item["kind"] == "partner_mapping_repair"
    )
    assert (replay_operation["status"], replay_operation["action"]) == (
        "already_repaired",
        "none",
    )
    assert replay["counts"]["approved_operations"] == 0


def test_partner_mapping_repair_stale_offer_is_rejected_without_backup(tmp_path):
    stores, partners, _brand_id = _database(tmp_path)
    source = stores.apply_change(
        "add_merchant", {"name": "Archived Helix", "channel": "offline"}, actor_id=1
    )
    target = stores.apply_change(
        "add_merchant", {"name": "Active Helix", "channel": "offline"}, actor_id=1
    )
    assert source.brand_id is not None and target.brand_id is not None
    stores.apply_change(
        "merge_brand", {"brand_id": source.brand_id, "target_id": target.brand_id}, actor_id=1
    )
    offer = partners.create_offer(
        PartnerOfferInput(
            brand_id=source.brand_id,
            card_id="helix-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("2")),),
        ),
        actor_id=1,
        source_key="helix:offer",
    )
    with stores.transaction() as connection:
        mapping_id = connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?) RETURNING id",
            ("helix:brand", source.brand_id),
        ).fetchone()[0]
    snapshot = _snapshot()
    snapshot["partner_mapping_repairs"] = [
        {
            "source_key": "helix:brand",
            "mapping_id": mapping_id,
            "expected_brand_id": source.brand_id,
            "target_brand_id": target.brand_id,
            "offer_id": offer.id,
            "offer_source_key": "helix:offer",
            "expected_offer": _offer_guard(offer),
        }
    ]
    report = preview_partner_batch(stores, snapshot)
    with stores.transaction() as connection:
        connection.execute(
            "UPDATE partner_offers SET conditions='moderator edit' WHERE id=?", (offer.id,)
        )
    backup = tmp_path / "must-not-exist.sqlite3"
    with pytest.raises(StalePartnerPlanError):
        apply_partner_batch(
            stores,
            snapshot,
            actor_id=1,
            expected_plan_sha256=report["plan_sha256"],
            backup_path=backup,
        )
    assert not backup.exists()
    with stores.connection() as connection:
        assert (
            connection.execute(
                "SELECT brand_id FROM partner_seed_brands WHERE id=?", (mapping_id,)
            ).fetchone()["brand_id"]
            == source.brand_id
        )


def _mapping_repair_fixture(tmp_path, *, target_offer=False, planned_offer=False):
    stores, partners, _brand_id = _database(tmp_path)
    source = stores.apply_change(
        "add_merchant", {"name": "Repair Source", "channel": "offline"}, actor_id=1
    )
    target = stores.apply_change(
        "add_merchant", {"name": "Repair Target", "channel": "offline"}, actor_id=1
    )
    assert source.brand_id is not None and target.brand_id is not None
    stores.apply_change(
        "merge_brand", {"brand_id": source.brand_id, "target_id": target.brand_id}, actor_id=1
    )
    offer = partners.create_offer(
        PartnerOfferInput(
            brand_id=source.brand_id,
            card_id="repair-card",
            channel="offline",
            mode="total",
            reward_kind="cash",
            tiers=(PartnerTierInput(Decimal("2")),),
        ),
        actor_id=1,
        source_key="repair:source-offer",
    )
    if target_offer:
        partners.create_offer(
            PartnerOfferInput(
                brand_id=target.brand_id,
                card_id="repair-card",
                channel="offline",
                mode="total",
                reward_kind="cash",
                tiers=(PartnerTierInput(Decimal("2")),),
            ),
            actor_id=1,
            source_key="repair:target-offer",
        )
    with stores.transaction() as connection:
        mapping_id = connection.execute(
            "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?) RETURNING id",
            ("repair:brand", source.brand_id),
        ).fetchone()[0]
    repair = {
        "source_key": "repair:brand",
        "mapping_id": mapping_id,
        "expected_brand_id": source.brand_id,
        "target_brand_id": target.brand_id,
        "offer_id": offer.id,
        "offer_source_key": "repair:source-offer",
        "expected_offer": _offer_guard(offer),
    }
    snapshot = _snapshot()
    if planned_offer:
        snapshot["offers"] = [
            _offer("repair:planned-offer", target.brand_id, card_id="repair-card")
        ]
    snapshot["partner_mapping_repairs"] = [repair]
    return stores, snapshot


def test_mapping_repair_holds_against_active_target_offer(tmp_path):
    stores, snapshot = _mapping_repair_fixture(tmp_path, target_offer=True)

    report = preview_partner_batch(stores, snapshot)
    operation = next(
        item for item in report["operations"] if item["kind"] == "partner_mapping_repair"
    )
    assert (operation["status"], operation["action"]) == ("logical_overlap", "none")
    assert report["counts"]["approved_repairs"] == 0
    assert any(item["kind"] == "mapping_repair_conflict" for item in report["conflicts"])


def test_mapping_repair_holds_when_guarded_offer_has_active_linked_exclusion(tmp_path):
    stores, snapshot = _mapping_repair_fixture(tmp_path)
    partners = PartnerRepository(stores)
    repair = snapshot["partner_mapping_repairs"][0]
    partners.create_exclusion(
        PartnerExclusionInput(
            brand_id=repair["expected_brand_id"],
            card_id="repair-card",
            reward_kind="cash",
            channel="offline",
            mcc="5411",
            suppress_base=True,
        ),
        actor_id=1,
        offer_id=repair["offer_id"],
    )

    report = preview_partner_batch(stores, snapshot)
    operation = next(
        item for item in report["operations"] if item["kind"] == "partner_mapping_repair"
    )
    assert (operation["status"], operation["action"]) == ("linked_exclusions", "none")
    assert report["counts"]["approved_repairs"] == 0
    assert any(
        item["kind"] == "mapping_repair_conflict"
        and item["reason"] == "guarded offer has active linked exclusions"
        for item in report["conflicts"]
    )


def test_mapping_repair_holds_involved_planned_offer(tmp_path):
    stores, snapshot = _mapping_repair_fixture(tmp_path, planned_offer=True)

    report = preview_partner_batch(stores, snapshot)
    repair = next(item for item in report["operations"] if item["kind"] == "partner_mapping_repair")
    planned = next(
        item for item in report["operations"] if item["source_key"] == "repair:planned-offer"
    )
    assert (repair["status"], repair["action"]) == ("logical_overlap", "none")
    assert (planned["status"], planned["action"]) == ("mapping_repair_overlap", "none")
    assert report["counts"]["approved_operations"] == 0


def test_mapping_repairs_hold_against_each_other_after_rebinding(tmp_path):
    stores, partners, _brand_id = _database(tmp_path)
    target = stores.apply_change(
        "add_merchant", {"name": "Shared Repair Target", "channel": "offline"}, actor_id=1
    )
    source_ids = []
    offers = []
    mapping_ids = []
    for index in (1, 2):
        source = stores.apply_change(
            "add_merchant",
            {"name": f"Shared Repair Source {index}", "channel": "offline"},
            actor_id=1,
        )
        assert source.brand_id is not None and target.brand_id is not None
        stores.apply_change(
            "merge_brand", {"brand_id": source.brand_id, "target_id": target.brand_id}, actor_id=1
        )
        offer = partners.create_offer(
            PartnerOfferInput(
                brand_id=source.brand_id,
                card_id="shared-repair-card",
                channel="offline",
                mode="total",
                reward_kind="cash",
                tiers=(PartnerTierInput(Decimal("2")),),
            ),
            actor_id=1,
            source_key=f"repair:{index}:offer",
        )
        with stores.transaction() as connection:
            mapping_id = connection.execute(
                "INSERT INTO partner_seed_brands(source_key,brand_id) VALUES(?,?) RETURNING id",
                (f"repair:{index}:brand", source.brand_id),
            ).fetchone()[0]
        source_ids.append(source.brand_id)
        offers.append(offer)
        mapping_ids.append(mapping_id)

    snapshot = _snapshot()
    snapshot["partner_mapping_repairs"] = [
        {
            "source_key": f"repair:{index}:brand",
            "mapping_id": mapping_id,
            "expected_brand_id": source_id,
            "target_brand_id": target.brand_id,
            "offer_id": offer.id,
            "offer_source_key": f"repair:{index}:offer",
            "expected_offer": _offer_guard(offer),
        }
        for index, mapping_id, source_id, offer in zip(
            (1, 2), mapping_ids, source_ids, offers, strict=True
        )
    ]
    report = preview_partner_batch(stores, snapshot)
    repairs = [item for item in report["operations"] if item["kind"] == "partner_mapping_repair"]
    assert len(repairs) == 2
    assert {(item["status"], item["action"]) for item in repairs} == {("logical_overlap", "none")}
    assert report["counts"]["approved_repairs"] == 0


def test_mapping_repair_on_legacy_mapping_schema_is_deterministic_conflict(tmp_path):
    stores, snapshot = _mapping_repair_fixture(tmp_path)
    with stores.transaction() as connection:
        connection.execute("ALTER TABLE partner_seed_brands RENAME TO partner_seed_brands_legacy")
        connection.execute(
            """CREATE TABLE partner_seed_brands (
            source_key TEXT PRIMARY KEY,
            brand_id INTEGER NOT NULL REFERENCES store_brands(id))"""
        )
        connection.execute(
            """INSERT INTO partner_seed_brands(source_key,brand_id)
            SELECT source_key,brand_id FROM partner_seed_brands_legacy"""
        )
        connection.execute("DROP TABLE partner_seed_brands_legacy")
        connection.execute(
            "UPDATE partner_seed_brands SET brand_id=? WHERE source_key=?",
            (snapshot["partner_mapping_repairs"][0]["target_brand_id"], "repair:brand"),
        )

    first = preview_partner_batch(stores, snapshot)
    second = preview_partner_batch(stores, snapshot)
    assert first == second
    operation = next(
        item for item in first["operations"] if item["kind"] == "partner_mapping_repair"
    )
    assert (operation["status"], operation["action"]) == ("schema_conflict", "none")
    assert first["counts"]["approved_repairs"] == 0

    ordinary = _snapshot(
        _offer("legacy:ordinary", snapshot["partner_mapping_repairs"][0]["target_brand_id"])
    )
    ordinary_report = preview_partner_batch(stores, ordinary)
    assert ordinary_report["counts"]["offers_new"] == 1
    assert ordinary_report["counts"]["approved_inserts"] == 1

    mapped_ordinary = _snapshot(
        {
            "source_key": "legacy:mapped",
            "brand_key": "repair:brand",
            "brand": "Repair Target",
            "aliases": [],
            "card_id": "legacy-mapped-card",
            "channel": "offline",
            "mode": "total",
            "reward_kind": "cash",
            "tiers": [{"value": "1"}],
        }
    )
    mapped_report = preview_partner_batch(stores, mapped_ordinary)
    assert mapped_report["counts"]["offers_new"] == 1
    assert mapped_report["counts"]["approved_inserts"] == 1


def test_bundled_partner_migration_20260911_keeps_reviewed_decisions():
    snapshot = load_snapshot(
        Path(__file__).parents[1] / "src" / "mcc_bot" / "data" / "partner_migration_20260911.json"
    )

    assert snapshot["reviewed"] is True
    assert len(snapshot["offers"]) == 659
    assert len(snapshot["exclusions"]) == 7
    assert len(snapshot["offer_retirements"]) == 165
    assert [item["offer_id"] for item in snapshot["manual_offer_retirements"]] == [209, 213]
    assert len(snapshot["partner_mapping_repairs"]) == 1
    repair = snapshot["partner_mapping_repairs"][0]
    assert {key: repair[key] for key in repair if key != "expected_offer"} == {
        "source_key": "brand:plushki:62ac1687",
        "mapping_id": 178,
        "expected_brand_id": 263,
        "target_brand_id": 375,
        "offer_id": 179,
        "offer_source_key": "plushki:promo:03",
    }
    assert repair["expected_offer"]["brand_id"] == 263
    assert repair["expected_offer"]["card_id"] == "vitamin_d"
    assert snapshot["partner_seed_tombstones"][0]["source_key"] == "cashalot:21vek-by"
    assert snapshot["partner_seed_tombstones"][0]["offer_id"] == 115
    assert snapshot["problems"] == [
        {
            "action": "hold",
            "brand": "ORO",
            "kind": "ambiguous_reward",
            "observed_combo_rate": "8",
            "reason": "Плитка ORO и видимый popup содержат разные ставки; offer не создаётся.",
            "source_id": 104700,
            "source_key": "paritet:104700:paritet_combo:offline",
        }
    ]

    offer_keys = {item["source_key"] for item in snapshot["offers"]}
    assert "cashalot:21vek-by" not in offer_keys
    assert "plushki:promo:03" not in offer_keys
    assert {
        "bnb:199999:online",
        "paritet:111407:paritet_combo:online",
        "plushki:promo:01",
    } <= offer_keys
