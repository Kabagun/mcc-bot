from __future__ import annotations

import json
import sqlite3

import pytest

import mcc_bot.location_review_20260901 as location_review
from mcc_bot.location_review_20260901 import (
    MANIFEST_PATH,
    LocationReviewError,
    apply_location_review,
    source_fingerprint,
)
from mcc_bot.stores import StoreRepository


def _setup(tmp_path):
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    stores.initialize()
    result = stores.apply_change(
        "add_merchant",
        {"name": "Локальный", "channel": "offline", "mcc": "5411"},
        1,
    )
    with stores.transaction() as connection:
        merchant_id = stores.list_brand_members(result.brand_id, connection=connection)[0].id
        connection.execute(
            """INSERT INTO store_sources
               (source,store_id,merchant_id,network_id,metadata_json)
               VALUES('tannei','123',?,NULL,?)""",
            (
                merchant_id,
                json.dumps(
                    {
                        "id": 123,
                        "name": "Локальный",
                        "address": "Минск, Локальная улица, 1",
                        "network_id": None,
                        "network_name": None,
                        "is_online": False,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        fingerprint = source_fingerprint(connection, result.brand_id)
    return stores, result.brand_id, fingerprint


def _manifest(tmp_path, brand_id, fingerprint, *, name="Локальный"):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "manifest": "manual-store-locations-2026-09-01",
                "review": {
                    "source_address_rows": 1,
                    "address_bearing_active_brands": 1,
                    "expected_entry_count": 1,
                    "expected_brand_ids": [brand_id],
                    "explicit_network_without_location": [1025],
                },
                "entries": [
                    {
                        "brand_id": brand_id,
                        "expected_name": name,
                        "expected_revision": 1,
                        "expected_location": None,
                        "desired_location": "Минск, Локальная улица, 1",
                        "source_fingerprint": fingerprint,
                        "decision": "Одна подтверждённая локальная точка",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _use_test_manifest_contract(monkeypatch, brand_id):
    monkeypatch.setattr(location_review, "EXPECTED_BRAND_IDS", (brand_id,))
    monkeypatch.setattr(location_review, "EXPECTED_SOURCE_ADDRESS_ROWS", 1)
    monkeypatch.setattr(location_review, "EXPECTED_ADDRESS_BEARING_ACTIVE_BRANDS", 1)


def test_location_review_dry_run_apply_and_idempotent_repeat(tmp_path, monkeypatch):
    stores, brand_id, fingerprint = _setup(tmp_path)
    manifest = _manifest(tmp_path, brand_id, fingerprint)
    _use_test_manifest_contract(monkeypatch, brand_id)

    assert apply_location_review(stores, actor_id=1, apply=False, path=manifest) == {
        "mode": "dry-run",
        "reviewed": 1,
        "pending": 1,
        "changed": 0,
        "already_current": 0,
    }
    assert stores.get_brand(brand_id).location is None

    applied = apply_location_review(stores, actor_id=1, apply=True, path=manifest)
    assert applied == {
        "mode": "apply",
        "reviewed": 1,
        "pending": 0,
        "changed": 1,
        "already_current": 0,
    }
    brand = stores.get_brand(brand_id)
    assert brand.location == "Минск, Локальная улица, 1"
    assert brand.revision == 2

    repeated = apply_location_review(stores, actor_id=1, apply=True, path=manifest)
    assert repeated["changed"] == 0
    assert repeated["already_current"] == 1
    with stores.connection() as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_location_review_fails_closed_and_keeps_batch_atomic(tmp_path, monkeypatch):
    stores, brand_id, fingerprint = _setup(tmp_path)
    manifest = _manifest(tmp_path, brand_id, fingerprint, name="Другое имя")
    _use_test_manifest_contract(monkeypatch, brand_id)

    with pytest.raises(LocationReviewError, match="больше не соответствует"):
        apply_location_review(stores, actor_id=1, apply=True, path=manifest)

    assert stores.get_brand(brand_id).location is None


def test_location_review_rejects_changed_source_metadata(tmp_path, monkeypatch):
    stores, brand_id, fingerprint = _setup(tmp_path)
    manifest = _manifest(tmp_path, brand_id, fingerprint)
    _use_test_manifest_contract(monkeypatch, brand_id)
    with stores.transaction() as connection:
        connection.execute(
            "UPDATE store_sources SET metadata_json=? WHERE store_id='123'",
            (
                json.dumps(
                    {
                        "id": 123,
                        "name": "Локальный",
                        "address": "Другой адрес",
                        "network_id": None,
                        "network_name": None,
                        "is_online": False,
                    },
                    ensure_ascii=False,
                ),
            ),
        )

    with pytest.raises(LocationReviewError, match="Источники"):
        apply_location_review(stores, actor_id=1, apply=True, path=manifest)

    assert stores.get_brand(brand_id).location is None


def test_default_manifest_rejects_a_missing_entry(tmp_path):
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    raw["entries"].pop()
    path = tmp_path / "incomplete.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(LocationReviewError, match="brand ID"):
        location_review.load_manifest(path)


def test_dry_run_does_not_initialize_an_older_database(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_only(id INTEGER PRIMARY KEY)")
    stores = StoreRepository(path)

    with pytest.raises(LocationReviewError, match="additive-миграции"):
        apply_location_review(stores, actor_id=0, apply=False)

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert tables == {"legacy_only"}
