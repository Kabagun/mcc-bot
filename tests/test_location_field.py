from __future__ import annotations

import sqlite3

import pytest

from mcc_bot.community import CommunityError, CommunityService
from mcc_bot.community_handlers import _form_complete
from mcc_bot.stores import StoreRepository, normalize_location


def _submit(service: CommunityService, user_id: int, payload: dict):
    draft = service.begin(
        user_id,
        stage="preview",
        data={"kind": "mcc_save", "payload": payload, "draft_mode": True},
    )
    return service.submit(user_id, draft.id, draft.version)


def test_brand_location_migrates_and_is_rendered_for_manual_store(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE store_merchants (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, channel TEXT NOT NULL,
              aliases_json TEXT NOT NULL DEFAULT '[]', archived INTEGER NOT NULL DEFAULT 0,
              revision INTEGER NOT NULL DEFAULT 1, merged_into INTEGER, source_identity TEXT UNIQUE
            );
            CREATE TABLE store_facts (
              id INTEGER PRIMARY KEY, merchant_id INTEGER NOT NULL, mcc TEXT NOT NULL,
              archived INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
              UNIQUE(merchant_id,mcc)
            );
            CREATE TABLE store_evidence (
              id INTEGER PRIMARY KEY, fact_id INTEGER NOT NULL, source TEXT NOT NULL,
              source_key TEXT NOT NULL, details_json TEXT NOT NULL,
              revoked INTEGER NOT NULL DEFAULT 0, UNIQUE(source,source_key)
            );
            CREATE TABLE store_sources (
              id INTEGER PRIMARY KEY, source TEXT NOT NULL, store_id TEXT NOT NULL,
              merchant_id INTEGER NOT NULL, network_id TEXT, metadata_json TEXT NOT NULL,
              UNIQUE(source,store_id)
            );
            CREATE TABLE store_audit (
              id INTEGER PRIMARY KEY, kind TEXT NOT NULL, merchant_id INTEGER NOT NULL,
              actor_id INTEGER NOT NULL, changes_json TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reverted_by INTEGER
            );
            CREATE TABLE store_brands (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL,
              aliases_json TEXT NOT NULL DEFAULT '[]',
              archived INTEGER NOT NULL DEFAULT 0,
              revision INTEGER NOT NULL DEFAULT 1, merged_into INTEGER
            );
            """
        )
    repository = StoreRepository(path)
    repository.initialize()
    with repository.connection() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(store_brands)")}
    assert "location" in columns

    created = repository.apply_change(
        "add_merchant",
        {
            "name": "Point Shop",
            "channel": "offline",
            "mcc": "5411",
            "location": "Минск, ул. Ленина, 1",
        },
        1,
    )
    assert repository.get_brand(created.brand_id).location == "Минск, ул. Ленина, 1"


def test_same_name_requires_location_and_same_normalized_location_is_duplicate(tmp_path):
    service = CommunityService(StoreRepository(tmp_path / "stores.sqlite3"), owner_id=1)
    service.initialize()
    service.set_role(1, 2, True)
    service.stores.apply_change(
        "add_merchant",
        {"name": "Coffee", "channel": "offline", "mcc": "5411", "location": "Минск, ул. Ленина, 1"},
        1,
    )

    with pytest.raises(CommunityError, match="где он находится"):
        _submit(
            service,
            10,
            {"name": "coffee", "channel": "offline", "mcc": "5812", "location": None},
        )
    with pytest.raises(CommunityError, match="таким местом"):
        _submit(
            service,
            10,
            {
                "name": "COFFEE",
                "channel": "offline",
                "mcc": "5812",
                "location": " Минск ул Ленина 1 ",
            },
        )

    accepted = _submit(
        service,
        10,
        {
            "name": "Coffee",
            "channel": "offline",
            "mcc": "5812",
            "location": "Брест, ул. Советская, 2",
        },
    )
    assert accepted.status == "pending"


def test_form_marks_location_required_only_for_colliding_name(tmp_path):
    service = CommunityService(StoreRepository(tmp_path / "stores.sqlite3"), owner_id=1)
    service.initialize()
    service.stores.apply_change(
        "add_merchant",
        {"name": "Existing", "channel": "offline", "mcc": "5411"},
        1,
    )
    duplicate = {
        "form": "store_create",
        "values": {"name": "existing", "mcc": "5411", "channel": "offline"},
    }
    unique = {
        "form": "store_create",
        "values": {"name": "Different", "mcc": "5411", "channel": "offline"},
    }
    assert not _form_complete(duplicate, service)
    assert _form_complete(
        {**duplicate, "values": {**duplicate["values"], "location": "Минск"}},
        service,
    )
    assert _form_complete(unique, service)


def test_tannei_addresses_are_compactly_reported_without_splitting_network(tmp_path):
    repository = StoreRepository(tmp_path / "stores.sqlite3")
    repository.initialize()
    metadata = {
        "id": 1,
        "network_id": 10,
        "network_name": "Coffee",
        "name": "Coffee branch",
        "is_online": False,
        "address": "Минск, ул. Ленина, 1",
    }
    first = repository.import_store(
        metadata,
        [
            {
                "mcc": "5411",
                "payment_date": "2026-08",
                "merchant_type": None,
                "address_extra": None,
            }
        ],
    )
    second = repository.import_store(
        {**metadata, "id": 2, "address": "Брест, ул. Советская, 2"},
        [
            {
                "mcc": "5812",
                "payment_date": "2026-08",
                "merchant_type": None,
                "address_extra": None,
            }
        ],
    )
    assert first.merchant_id == second.merchant_id
    assert (
        repository.brand_location_summary(first.brand_id)
        == "Минск, ул. Ленина, 1 · ещё 1 адрес"
    )
    assert normalize_location(" Минск ул Ленина 1 ") == normalize_location(
        "Минск, ул. Ленина, 1"
    )
