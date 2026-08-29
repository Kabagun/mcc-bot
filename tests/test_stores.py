# Names intentionally exercise Cyrillic transliteration.
# ruff: noqa: RUF001

from __future__ import annotations

import json
import sqlite3

import pytest

from mcc_bot.stores import StaleChangeError, StoreError, StoreRepository, normalize_store_name


@pytest.fixture
def repository(tmp_path):
    repository = StoreRepository(tmp_path / "stores.sqlite3")
    repository.initialize()
    return repository


def add(repository, name="Евроопт", channel="offline", mcc="5411"):
    payload = {"name": name, "channel": channel}
    if mcc:
        payload["mcc"] = mcc
    return repository.apply_change("add_merchant", payload, 123)


def source_metadata(store_id=1, network_id=10, online=False):
    return {
        "id": store_id,
        "network_id": network_id,
        "network_name": "Евроопт",
        "name": "Е-доставка" if online else "Евроопт",
        "is_online": online,
        "address": None,
    }


def source_evidence(mcc="5411"):
    return [
        {"mcc": mcc, "payment_date": "2022-06", "merchant_type": "Groceries", "address_extra": None}
    ]


@pytest.mark.parametrize("name", ["Евроопт", " EURO OPT ", "Evro-opt", "евро.опт", "EUROOPT"])
def test_transliterated_aliases_search_without_merging(repository, name):
    first = add(repository)
    assert normalize_store_name(name) == normalize_store_name("Евроопт")
    assert repository.search(name).matches[0].id == first.merchant_id
    second = add(repository, name="Euroopt")
    assert len(repository.search(name).matches) == 2
    assert first.merchant_id != second.merchant_id


def test_cross_script_brand_pronunciation_is_search_only_and_ranked(repository):
    green = add(repository, name="Green", mcc=None)
    longer = add(repository, name="Green Market", mcc=None)

    result = repository.search("грин")
    assert [merchant.id for merchant in result.matches] == [green.merchant_id]
    assert not result.suggestions
    assert repository.find_exact("грин", "offline") == ()
    assert longer.merchant_id not in {merchant.id for merchant in result.matches}

    cyrillic = add(repository, name="Грин", mcc=None)
    result = repository.search("грин")
    assert [brand.id for brand in result.matches] == [cyrillic.brand_id]
    assert [brand.id for brand in repository.search("Green").matches] == [green.brand_id]


def test_relaxed_transliteration_does_not_cross_same_script_or_short_names(repository):
    green = add(repository, name="Green", mcc=None)
    grin = add(repository, name="Grin", mcc=None)
    bee = add(repository, name="Bee", mcc=None)

    result = repository.search("Grin")
    assert [merchant.id for merchant in result.matches] == [grin.merchant_id]
    assert green.merchant_id not in {merchant.id for merchant in result.matches}
    assert bee.merchant_id not in {merchant.id for merchant in repository.search("би").matches}


@pytest.mark.parametrize("query", ["Добрыя леки", "добрыя лекi", "DOBRYYA LEKI"])
def test_belarusian_i_and_common_keyboard_spellings_share_one_key(repository, query):
    merchant = add(repository, name="Добрыя лекі", mcc=None)
    assert [item.id for item in repository.search(query).matches] == [merchant.merchant_id]


def test_cross_script_pronunciation_applies_to_explicit_aliases(repository):
    merchant = repository.apply_change(
        "add_merchant",
        {"name": "Other", "channel": "offline", "aliases": ["Green"]},
        123,
    )
    assert [item.id for item in repository.search("грин").matches] == [merchant.merchant_id]
    assert repository.find_exact("грин", "offline") == ()


def test_fuzzy_suggestions_and_channels_are_distinct(repository):
    offline = add(repository)
    online = add(repository, channel="online")
    result = repository.search("evroppt")
    assert not result.matches
    assert {merchant.id for merchant in result.suggestions} == {
        offline.merchant_id,
        online.merchant_id,
    }
    with repository.transaction() as connection:
        assert [
            item.id for item in repository.find_exact("Euroopt", "online", connection=connection)
        ] == [online.merchant_id]


def test_add_duplicate_confirmation_and_revert_keep_other_evidence(repository):
    merchant = add(repository, mcc=None)
    first = repository.apply_change(
        "add_mcc",
        {"merchant_id": merchant.merchant_id, "mcc": "5411", "evidence": {"submission_id": 1}},
        10,
    )
    second = repository.apply_change(
        "add_mcc",
        {"merchant_id": merchant.merchant_id, "mcc": "5411", "evidence": {"submission_id": 2}},
        20,
    )
    assert len(repository.list_mcc(merchant.merchant_id)) == 1
    assert repository.list_mcc(merchant.merchant_id)[0].evidence_count == 2
    repository.apply_change("revert", {"audit_id": first.audit_id}, 10)
    assert repository.list_mcc(merchant.merchant_id)[0].evidence_count == 1
    repository.apply_change("revert", {"audit_id": second.audit_id}, 20)
    assert not repository.list_mcc(merchant.merchant_id)


def test_revert_never_overwrites_later_structural_edit(repository):
    merchant = add(repository)
    change = repository.apply_change(
        "rename_merchant", {"merchant_id": merchant.merchant_id, "name": "A"}, 1
    )
    repository.apply_change(
        "rename_merchant", {"merchant_id": merchant.merchant_id, "name": "B"}, 2
    )
    with pytest.raises(StaleChangeError):
        repository.apply_change("revert", {"audit_id": change.audit_id}, 1)
    assert repository.get(merchant.merchant_id).name == "B"


def test_revert_create_rejects_new_independent_evidence_atomically(repository):
    first = add(repository)
    repository.apply_change("add_mcc", {"merchant_id": first.merchant_id, "mcc": "5411"}, 99)
    with repository.transaction() as connection:  # noqa: SIM117 - catch inside transaction
        with pytest.raises(StaleChangeError):
            repository.apply_change(
                "revert", {"audit_id": first.audit_id}, 1, connection=connection
            )
    assert repository.get(first.merchant_id)
    assert repository.list_mcc(first.merchant_id)[0].evidence_count == 2


def test_shared_transaction_rolls_back_fact_and_audit(repository):
    merchant = add(repository, mcc=None)
    before = repository.history()
    with pytest.raises(RuntimeError), repository.transaction() as connection:
        repository.apply_change(
            "add_mcc",
            {"merchant_id": merchant.merchant_id, "mcc": "5411"},
            1,
            connection=connection,
        )
        raise RuntimeError("moderation completion failed")
    assert not repository.list_mcc(merchant.merchant_id)
    assert repository.history() == before


def test_reject_media_in_durable_evidence(repository):
    merchant = add(repository, mcc=None)
    with pytest.raises(StoreError):
        repository.apply_change(
            "add_mcc",
            {"merchant_id": merchant.merchant_id, "mcc": "5411", "evidence": {"file_id": "secret"}},
            1,
        )
    assert not repository.list_mcc(merchant.merchant_id)


def test_import_chain_identity_channel_idempotence_and_precision(repository):
    first = repository.import_store(source_metadata(), source_evidence())
    repository.import_store(source_metadata(2), source_evidence("5812"))
    online = repository.import_store(source_metadata(3, online=True), source_evidence())
    assert first.merchant_id != online.merchant_id
    assert [fact.mcc for fact in repository.list_mcc(first.merchant_id)] == ["5411", "5812"]
    assert not repository.import_store(source_metadata(), source_evidence()).changed
    snapshot = repository.tannei_snapshot(first.brand_id)
    assert snapshot["channels"]["offline"]["5411"]["first_seen"] == "2022-06"


def test_import_snapshot_is_visible_without_automatic_audit_or_evidence_metadata(repository):
    imported = repository.import_store(source_metadata(), source_evidence())
    assert imported.changed
    assert imported.audit_id == 0
    assert repository.history(imported.merchant_id) == ()
    snapshot = repository.tannei_snapshot(imported.brand_id)
    assert snapshot["channels"]["offline"]["5411"]["support_count"] == 1
    assert "Groceries" not in str(snapshot)
    assert "address" not in str(snapshot)


def test_import_preserves_manual_name_alias_archive_and_duplicate_observations(repository):
    imported = repository.import_store(source_metadata(), source_evidence() * 2)
    merchant_id = imported.merchant_id
    repository.apply_change("rename_merchant", {"merchant_id": merchant_id, "name": "New"}, 1)
    repository.apply_change("aliases", {"merchant_id": merchant_id, "aliases": ["Alias"]}, 1)
    repository.apply_change("archive_mcc", {"merchant_id": merchant_id, "mcc": "5411"}, 1)
    repository.import_store(source_metadata(), source_evidence() * 2)
    assert not repository.list_mcc(merchant_id)
    assert repository.get(merchant_id).name == "New"
    assert repository.get(merchant_id).aliases == ("Alias",)
    assert repository.list_mcc(merchant_id, include_archived=True)[0].evidence_count == 1
    repository.apply_change("archive_merchant", {"merchant_id": merchant_id}, 1)
    repository.import_store(source_metadata(4), source_evidence("5812"))
    assert repository.get(merchant_id) is None


def test_merge_archived_source_preserves_tombstone_for_reimport_and_new_branch(repository):
    source = repository.import_store(source_metadata(), source_evidence())
    target = add(repository, name="Target", mcc=None)
    repository.apply_change("archive_mcc", {"merchant_id": source.merchant_id, "mcc": "5411"}, 1)
    repository.apply_change(
        "merge_merchant", {"merchant_id": source.merchant_id, "target_id": target.merchant_id}, 1
    )
    repository.import_store(source_metadata(), source_evidence())
    repository.import_store(source_metadata(2), source_evidence())
    assert not repository.list_mcc(target.merchant_id)
    assert repository.list_mcc(target.merchant_id, include_archived=True)[0].evidence_count == 1


def test_merge_undo_preserves_new_independent_target_evidence(repository):
    source = add(repository)
    target = add(repository, "Target", mcc=None)
    merge = repository.apply_change(
        "merge_merchant", {"merchant_id": source.merchant_id, "target_id": target.merchant_id}, 1
    )
    repository.apply_change("add_mcc", {"merchant_id": target.merchant_id, "mcc": "5411"}, 2)
    repository.apply_change("revert", {"audit_id": merge.audit_id}, 1)
    assert repository.list_mcc(source.merchant_id)[0].evidence_count == 1
    assert repository.list_mcc(target.merchant_id)[0].evidence_count == 1


def test_replace_and_undo_restore_evidence_and_reject_double_undo(repository):
    merchant = add(repository)
    edit = repository.apply_change(
        "replace_mcc", {"merchant_id": merchant.merchant_id, "old_mcc": "5411", "mcc": "5812"}, 1
    )
    assert [fact.mcc for fact in repository.list_mcc(merchant.merchant_id)] == ["5812"]
    repository.apply_change("revert", {"audit_id": edit.audit_id}, 1)
    assert [fact.mcc for fact in repository.list_mcc(merchant.merchant_id)] == ["5411"]
    with pytest.raises(StaleChangeError):
        repository.apply_change("revert", {"audit_id": edit.audit_id}, 1)


def test_merge_undo_after_new_import_is_stale_and_does_not_split_network(repository):
    source = repository.import_store(source_metadata(), source_evidence())
    target = add(repository, "Target", mcc=None)
    merge = repository.apply_change(
        "merge_merchant", {"merchant_id": source.merchant_id, "target_id": target.merchant_id}, 1
    )
    repository.import_store(source_metadata(2), source_evidence())
    with pytest.raises(StaleChangeError):
        repository.apply_change("revert", {"audit_id": merge.audit_id}, 1)
    assert repository.get(source.merchant_id) is None
    assert repository.list_mcc(target.merchant_id)[0].evidence_count == 1


@pytest.mark.parametrize("metadata", [source_metadata(network_id=20), source_metadata(online=True)])
def test_import_changed_source_identity_is_not_silently_reclassified(repository, metadata):
    imported = repository.import_store(source_metadata(), source_evidence())
    with pytest.raises(StoreError, match="network/channel"):
        repository.import_store(metadata, source_evidence())
    assert repository.get(imported.merchant_id).channel == "offline"
    assert repository.list_mcc(imported.merchant_id)[0].evidence_count == 1


def test_reused_submission_cannot_support_a_different_fact(repository):
    merchant = add(repository, mcc=None)
    payload = {"merchant_id": merchant.merchant_id, "mcc": "5411", "evidence": {"submission_id": 7}}
    repository.apply_change("add_mcc", payload, 1)
    with pytest.raises(StoreError):
        repository.apply_change("add_mcc", {**payload, "mcc": "5812"}, 1)
    assert [fact.mcc for fact in repository.list_mcc(merchant.merchant_id)] == ["5411"]


@pytest.mark.parametrize("undo", [False, True])
def test_replayed_old_submission_cannot_reactivate_archived_or_reverted_fact(repository, undo):
    merchant = add(repository, mcc=None)
    payload = {"merchant_id": merchant.merchant_id, "mcc": "5411", "evidence": {"submission_id": 7}}
    addition = repository.apply_change("add_mcc", payload, 1)
    if undo:
        repository.apply_change("revert", {"audit_id": addition.audit_id}, 1)
    else:
        repository.apply_change(
            "archive_mcc", {"merchant_id": merchant.merchant_id, "mcc": "5411"}, 1
        )
    with pytest.raises(StoreError):
        repository.apply_change("add_mcc", payload, 1)
    assert not repository.list_mcc(merchant.merchant_id)


def test_changed_import_updates_snapshot_without_creating_revertible_audit(repository):
    first = repository.import_store(source_metadata(), source_evidence())
    assert repository.import_store(source_metadata(), source_evidence("5812")).changed
    assert repository.history(first.merchant_id) == ()
    assert [fact.mcc for fact in repository.list_mcc(first.merchant_id)] == ["5411", "5812"]
    assert not repository.import_store(source_metadata(), source_evidence("5812")).changed


def test_history_pagination_reaches_older_archived_merchants(repository):
    old = add(repository, "Old merchant", mcc=None)
    archive = repository.apply_change("archive_merchant", {"merchant_id": old.merchant_id}, 1)
    for index in range(12):
        add(repository, f"New merchant {index}", mcc=None)

    first = repository.history(limit=10)
    second = repository.history(limit=10, offset=10)
    assert len(first) == 10
    assert len(second) == 4
    assert all(entry.merchant_id != old.merchant_id for entry in first)
    assert archive.audit_id in {entry.id for entry in second}
    assert first + second == repository.history()
    assert not {entry.id for entry in first} & {entry.id for entry in second}
    assert repository.get(old.merchant_id) is None
    assert repository.get(old.merchant_id, include_archived=True).archived


def test_history_filtered_pagination_shares_transaction_and_keeps_limit_clamp(repository):
    merchant = add(repository, mcc=None)
    with repository.transaction() as connection:
        for index in range(105):
            repository.apply_change(
                "rename_merchant",
                {"merchant_id": merchant.merchant_id, "name": f"Name {index}"},
                1,
                connection=connection,
            )
        all_entries = repository.history(merchant.merchant_id, limit=999, connection=connection)
        page = repository.history(merchant.merchant_id, limit=10, offset=10, connection=connection)
        assert len(all_entries) == 100
        assert page == all_entries[10:20]
        assert (
            repository.history(merchant.merchant_id, limit=0, connection=connection)
            == all_entries[:1]
        )
    assert repository.history(merchant.merchant_id, offset=-10) == repository.history(
        merchant.merchant_id
    )
    assert repository.history(offset=10**100) == ()


def test_audit_entry_returns_one_built_entry_or_none(repository):
    created = add(repository, name="Point lookup", mcc="5411")
    expected = next(entry for entry in repository.history() if entry.id == created.audit_id)

    assert repository.audit_entry(created.audit_id) == expected
    assert repository.audit_entry(created.audit_id + 10_000) is None
    with repository.transaction() as connection:
        assert repository.audit_entry(created.audit_id, connection=connection) == expected


@pytest.mark.parametrize("audit_id", [True, False, 0, -1, 1.0, "1", None])
def test_audit_entry_rejects_nonpositive_or_noninteger_id(repository, audit_id):
    with pytest.raises(StoreError, match="положительным целым"):
        repository.audit_entry(audit_id)


@pytest.mark.parametrize("offset", [True, False, 1.5, "10", None])
def test_history_rejects_noninteger_offsets(repository, offset):
    with pytest.raises(StoreError):
        repository.history(offset=offset)


def test_additive_brand_migration_is_idempotent_and_preserves_every_legacy_id(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
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
        INSERT INTO store_merchants(id,name,channel,aliases_json,revision)
          VALUES(17,'Legacy','offline','["Old"]',4);
        INSERT INTO store_facts(id,merchant_id,mcc,revision) VALUES(41,17,'5411',3);
        INSERT INTO store_evidence(id,fact_id,source,source_key,details_json)
          VALUES(53,41,'legacy','key','{}');
        INSERT INTO store_sources(id,source,store_id,merchant_id,metadata_json)
          VALUES(61,'legacy','store',17,'{}');
        INSERT INTO store_audit(id,kind,merchant_id,actor_id,changes_json)
          VALUES(71,'legacy',17,987,'[]');
        """
    )
    connection.close()

    repository = StoreRepository(path)
    repository.initialize()
    repository.initialize()

    with repository.connection() as connection:
        assert connection.execute("SELECT id FROM store_merchants").fetchall()[0][0] == 17
        assert tuple(connection.execute("SELECT id,note FROM store_facts").fetchone()) == (41, "")
        assert connection.execute("SELECT id FROM store_evidence").fetchall()[0][0] == 53
        assert connection.execute("SELECT id FROM store_sources").fetchall()[0][0] == 61
        assert tuple(connection.execute("SELECT id,actor_id FROM store_audit").fetchone()) == (
            71,
            987,
        )
        assert tuple(connection.execute("SELECT id,name FROM store_brands").fetchone()) == (
            17,
            "Legacy",
        )
        assert tuple(
            connection.execute("SELECT brand_id,merchant_id FROM store_brand_members").fetchone()
        ) == (17, 17)
        assert (
            connection.execute("SELECT brand_id FROM store_audit WHERE id=71").fetchone()[0] == 17
        )


def test_brand_search_uses_first_nonempty_tier_and_exact_alias(repository):
    mila = add(repository, name="Мила", mcc=None)
    milavitsa = add(repository, name="Милавица", mcc=None)
    repository.apply_change(
        "brand_aliases", {"brand_id": mila.brand_id, "aliases": ["Mila Shop"]}, 5
    )

    assert [brand.id for brand in repository.search("мила").matches] == [mila.brand_id]
    assert milavitsa.brand_id not in {brand.id for brand in repository.search("мила").matches}
    assert [brand.id for brand in repository.search("Mila Shop").matches] == [mila.brand_id]


def test_brand_groups_members_and_keeps_same_mcc_separate_between_channels(repository):
    offline = add(repository, name="Brand", channel="offline", mcc=None)
    online = repository.apply_change(
        "add_merchant",
        {"name": "Brand app", "channel": "online", "brand_id": offline.brand_id},
        2,
    )
    repository.apply_change(
        "add_mcc",
        {"merchant_id": offline.merchant_id, "mcc": "5411", "note": "касса"},
        3,
    )
    repository.apply_change(
        "add_mcc",
        {"merchant_id": online.merchant_id, "mcc": "5411", "note": "приложение"},
        4,
    )

    assert repository.list_brand_channels(offline.brand_id) == {
        "offline": (repository.get(offline.merchant_id),),
        "online": (repository.get(online.merchant_id),),
    }
    grouped = repository.list_brand_mcc(offline.brand_id)
    assert [(item.channel, item.mcc, item.note) for item in grouped] == [
        ("offline", "5411", "касса"),
        ("online", "5411", "приложение"),
    ]


def test_add_member_harmonizes_note_after_inserting_its_fact(repository):
    target = add(repository, name="Brand", mcc=None)
    repository.apply_change(
        "add_mcc",
        {"merchant_id": target.merchant_id, "mcc": "5411", "note": "первая"},
        1,
    )

    with pytest.raises(StoreError, match="выберите одно вручную"):
        repository.apply_change(
            "add_merchant",
            {
                "name": "Conflicting branch",
                "channel": "offline",
                "brand_id": target.brand_id,
                "mcc": "5411",
                "note": "другая",
            },
            2,
        )
    assert repository.find_exact("Conflicting branch", "offline") == ()

    adopted = repository.apply_change(
        "add_merchant",
        {
            "name": "Matching branch",
            "channel": "offline",
            "brand_id": target.brand_id,
            "mcc": "5411",
        },
        3,
    )
    assert repository.list_mcc(adopted.merchant_id)[0].note == "первая"


def test_brand_search_ignores_names_of_archived_members(repository):
    active = add(repository, name="Canonical", mcc=None)
    obsolete = repository.apply_change(
        "add_merchant",
        {"name": "Obsolete branch", "channel": "online", "brand_id": active.brand_id},
        1,
    )
    repository.apply_change("archive_merchant", {"merchant_id": obsolete.merchant_id}, actor_id=2)

    assert repository.search("Canonical").matches[0].id == active.brand_id
    assert repository.search("Obsolete branch").matches == ()


def test_brand_merge_adopts_note_and_conflicting_notes_require_manual_edit(repository):
    target = add(repository, name="Target", mcc=None)
    source = add(repository, name="Source", mcc=None)
    repository.apply_change("add_mcc", {"merchant_id": target.merchant_id, "mcc": "5411"}, 1)
    repository.apply_change(
        "add_mcc",
        {"merchant_id": source.merchant_id, "mcc": "5411", "note": "продукты"},
        2,
    )
    repository.apply_change(
        "merge_brand", {"brand_id": source.brand_id, "target_id": target.brand_id}, 3
    )
    assert repository.list_mcc(target.merchant_id)[0].note == "продукты"
    assert repository.list_brand_mcc(target.brand_id)[0].merchant_ids == (
        target.merchant_id,
        source.merchant_id,
    )

    conflict = add(repository, name="Conflict", mcc=None)
    repository.apply_change(
        "add_mcc",
        {"merchant_id": conflict.merchant_id, "mcc": "5411", "note": "супермаркет"},
        4,
    )
    with pytest.raises(StoreError, match="выберите одно вручную"):
        repository.apply_change(
            "merge_brand", {"brand_id": conflict.brand_id, "target_id": target.brand_id}, 5
        )
    assert repository.brand_for_merchant(conflict.merchant_id).id == conflict.brand_id

    repository.apply_change(
        "edit_mcc_note",
        {"merchant_id": conflict.merchant_id, "mcc": "5411", "note": "продукты"},
        6,
    )
    repository.apply_change(
        "merge_brand", {"brand_id": conflict.brand_id, "target_id": target.brand_id}, 7
    )
    assert repository.list_brand_mcc(target.brand_id)[0].note == "продукты"


def test_note_validation_replace_and_audited_group_edit(repository):
    merchant = add(repository, mcc=None)
    with pytest.raises(StoreError, match="48"):
        repository.apply_change(
            "add_mcc",
            {"merchant_id": merchant.merchant_id, "mcc": "5411", "note": "x" * 49},
            1,
        )
    repository.apply_change(
        "add_mcc",
        {"merchant_id": merchant.merchant_id, "mcc": "5411", "note": "old"},
        2,
    )
    edit = repository.apply_change(
        "edit_mcc_note",
        {"merchant_id": merchant.merchant_id, "mcc": "5411", "note": "new"},
        77,
    )
    assert repository.list_mcc(merchant.merchant_id)[0].note == "new"
    entry = repository.history(merchant.merchant_id)[0]
    assert entry.id == edit.audit_id
    assert entry.actor_id == 77
    assert "Примечание MCC 5411" in "\n".join(entry.details)
    repository.apply_change(
        "replace_mcc",
        {
            "merchant_id": merchant.merchant_id,
            "old_mcc": "5411",
            "mcc": "5812",
            "note": "restaurant",
        },
        3,
    )
    assert repository.list_mcc(merchant.merchant_id)[0].note == "restaurant"


def test_audit_summary_covers_mcc_actions_and_channels(repository):
    merchant = add(repository, name="Online", channel="online", mcc=None)
    added = repository.apply_change(
        "add_mcc", {"merchant_id": merchant.merchant_id, "mcc": "5411"}, 1
    )
    assert repository.history()[0].id == added.audit_id
    assert repository.history()[0].summary == "добавлен MCC 5411 · онлайн"

    confirmed = repository.apply_change(
        "add_mcc", {"merchant_id": merchant.merchant_id, "mcc": "5411"}, 2
    )
    assert repository.history()[0].id == confirmed.audit_id
    assert repository.history()[0].summary == "подтверждён MCC 5411 · онлайн"

    archived = repository.apply_change(
        "archive_mcc", {"merchant_id": merchant.merchant_id, "mcc": "5411"}, 3
    )
    assert repository.history()[0].id == archived.audit_id
    assert repository.history()[0].summary == "удалён MCC 5411 · онлайн"

    offline = add(repository, name="Offline", mcc="5411")
    replaced = repository.apply_change(
        "replace_mcc",
        {"merchant_id": offline.merchant_id, "old_mcc": "5411", "mcc": "5812"},
        4,
    )
    assert repository.history()[0].id == replaced.audit_id
    assert repository.history()[0].summary == "MCC 5411 → 5812 · офлайн"


def test_audit_summary_covers_names_aliases_merge_and_note(repository):
    source = add(repository, name="Source", mcc="5411")
    renamed = repository.apply_change(
        "rename_brand", {"brand_id": source.brand_id, "name": "Renamed"}, 1
    )
    assert repository.history()[0].id == renamed.audit_id
    assert repository.history()[0].summary == "название: «Source» → «Renamed»"

    alias_added = repository.apply_change(
        "brand_aliases", {"brand_id": source.brand_id, "aliases": ["Alias"]}, 2
    )
    assert repository.history()[0].id == alias_added.audit_id
    assert repository.history()[0].summary == "добавлено название «Alias»"
    alias_removed = repository.apply_change(
        "brand_aliases", {"brand_id": source.brand_id, "aliases": []}, 3
    )
    assert repository.history()[0].id == alias_removed.audit_id
    assert repository.history()[0].summary == "удалено название «Alias»"

    note = repository.apply_change(
        "edit_mcc_note",
        {"merchant_id": source.merchant_id, "mcc": "5411", "note": "касса"},
        4,
    )
    assert repository.history()[0].id == note.audit_id
    assert repository.history()[0].summary == "примечание MCC 5411: «нет» → «касса»"

    target = add(repository, name="Target", mcc=None)
    merged = repository.apply_change(
        "merge_brand", {"brand_id": source.brand_id, "target_id": target.brand_id}, 5
    )
    assert repository.history()[0].id == merged.audit_id
    assert repository.history()[0].summary == "объединён с «Target»"


def test_public_fact_replace_and_archive_cover_every_internal_source_row(repository):
    first = add(repository, name="Grouped", mcc=None)
    second = repository.apply_change(
        "add_merchant",
        {
            "brand_id": first.brand_id,
            "name": "Grouped source",
            "channel": "offline",
            "mcc": "5411",
            "note": "касса",
        },
        1,
    )
    repository.apply_change(
        "add_mcc",
        {"merchant_id": first.merchant_id, "mcc": "5411", "note": "касса"},
        1,
    )
    merchant_ids = [first.merchant_id, second.merchant_id]
    assert repository.list_brand_mcc(first.brand_id)[0].merchant_ids == tuple(merchant_ids)

    replaced = repository.apply_change(
        "replace_mcc",
        {
            "merchant_id": first.merchant_id,
            "merchant_ids": merchant_ids,
            "old_mcc": "5411",
            "mcc": "5812",
            "note": "касса",
        },
        2,
    )
    for merchant_id in merchant_ids:
        assert [(fact.mcc, fact.note) for fact in repository.list_mcc(merchant_id)] == [
            ("5812", "касса")
        ]
    assert repository.list_brand_mcc(first.brand_id)[0].merchant_ids == tuple(merchant_ids)

    archived = repository.apply_change(
        "archive_mcc",
        {
            "merchant_id": first.merchant_id,
            "merchant_ids": merchant_ids,
            "mcc": "5812",
        },
        3,
    )
    assert repository.list_brand_mcc(first.brand_id) == ()
    assert all(not repository.list_mcc(merchant_id) for merchant_id in merchant_ids)

    repository.apply_change("revert", {"audit_id": archived.audit_id}, 4)
    assert repository.list_brand_mcc(first.brand_id)[0].merchant_ids == tuple(merchant_ids)
    assert repository.history(first.merchant_id)[1].id == archived.audit_id
    assert replaced.audit_id > 0

    edited = repository.apply_change(
        "edit_mcc_note",
        {
            "merchant_id": first.merchant_id,
            "merchant_ids": merchant_ids,
            "mcc": "5812",
            "note": "новая подпись",
        },
        5,
    )
    assert all(
        repository.list_mcc(merchant_id)[0].note == "новая подпись" for merchant_id in merchant_ids
    )
    repository.apply_change("revert", {"audit_id": edited.audit_id}, 6)
    assert all(repository.list_mcc(merchant_id)[0].note == "касса" for merchant_id in merchant_ids)


def test_public_fact_group_rejects_partial_or_changed_membership(repository):
    first = add(repository, name="Grouped", mcc="5411")
    second = repository.apply_change(
        "add_merchant",
        {
            "brand_id": first.brand_id,
            "name": "Grouped source",
            "channel": "offline",
            "mcc": "5411",
        },
        1,
    )
    with pytest.raises(StoreError, match="Группа MCC уже изменилась"):
        repository.apply_change(
            "archive_mcc",
            {
                "merchant_id": first.merchant_id,
                "merchant_ids": [first.merchant_id],
                "mcc": "5411",
            },
            2,
        )
    assert repository.list_brand_mcc(first.brand_id)[0].merchant_ids == (
        first.merchant_id,
        second.merchant_id,
    )


def test_merchant_merge_reconciles_brand_and_brand_history_preserves_actors(repository):
    source = repository.apply_change("add_merchant", {"name": "Duplicate"}, 11)
    target = repository.apply_change("add_merchant", {"name": "Canonical"}, 22)
    repository.apply_change(
        "rename_merchant", {"merchant_id": source.merchant_id, "name": "Duplicate old"}, 33
    )
    repository.apply_change(
        "merge_merchant",
        {"merchant_id": source.merchant_id, "target_id": target.merchant_id},
        44,
    )

    assert repository.get(source.merchant_id) is None
    assert repository.brand_for_merchant(source.merchant_id).id == target.brand_id
    assert repository.get_brand(source.brand_id) is None
    assert repository.get_brand(source.brand_id, include_archived=True).archived
    assert {entry.actor_id for entry in repository.brand_history(target.brand_id)} >= {
        11,
        22,
        33,
        44,
    }


def test_curated_confirmation_is_transactional_deterministic_and_idempotent(repository):
    first = add(repository, name="First", mcc=None)
    second = add(repository, name="Second", mcc=None)
    key = "a" * 64 + ":7"
    evidence = {"image_sha256": "a" * 64, "row_number": 7}

    added = repository.confirm_mcc(
        first.merchant_id,
        "5411",
        actor_id=500,
        source="curated-image",
        source_key=key,
        evidence=evidence,
        note="grocery",
    )
    assert added.audit_id
    assert (
        repository.confirm_mcc(
            first.merchant_id,
            "5411",
            actor_id=500,
            source="curated-image",
            source_key=key,
            evidence=evidence,
            note="grocery",
        ).audit_id
        == 0
    )
    before = repository.history()
    with pytest.raises(StoreError), repository.transaction() as connection:
        repository.confirm_mcc(
            second.merchant_id,
            "5812",
            actor_id=500,
            source="curated-image",
            source_key="b" * 64 + ":1",
            connection=connection,
        )
        repository.confirm_mcc(
            second.merchant_id,
            "5999",
            actor_id=500,
            source="curated-image",
            source_key=key,
            connection=connection,
        )
    assert not repository.list_mcc(second.merchant_id)
    assert repository.history() == before


def test_import_keeps_strict_source_brands_despite_similar_names(repository):
    first = repository.import_store(source_metadata(store_id=1, network_id=10), source_evidence())
    second_metadata = source_metadata(store_id=2, network_id=20)
    second_metadata["network_name"] = "EURO OPT"
    second = repository.import_store(second_metadata, source_evidence())

    assert first.merchant_id != second.merchant_id
    assert first.brand_id != second.brand_id
    assert len(repository.search("Euroopt").matches) == 2
    with repository.connection() as connection:
        links = connection.execute(
            "SELECT store_id,merchant_id,network_id FROM store_sources ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in links] == [
            ("1", first.merchant_id, "10"),
            ("2", second.merchant_id, "20"),
        ]


def test_add_mcc_both_uses_two_real_channels_and_edit_names_is_one_reversible_audit(repository):
    created = repository.apply_change(
        "add_mcc_both",
        {"name": "Two channel", "mcc": "5411", "note": "new"},
        7,
    )
    channels = repository.list_brand_channels(created.brand_id)
    assert set(channels) == {"offline", "online"}
    assert {member.channel for members in channels.values() for member in members} == {
        "offline",
        "online",
    }
    assert all(
        repository.list_mcc(member.id)[0].note == "new"
        for members in channels.values()
        for member in members
    )
    edited = repository.apply_change(
        "edit_brand_names",
        {"brand_id": created.brand_id, "name": "Canonical", "aliases": ["Alias"]},
        8,
    )
    assert repository.get_brand(created.brand_id).name == "Canonical"
    assert repository.get_brand(created.brand_id).aliases == ("Alias",)
    assert repository.history()[0].id == edited.audit_id
    repository.apply_change("revert", {"audit_id": edited.audit_id}, 8)
    assert repository.get_brand(created.brand_id).name == "Two channel"
    assert repository.get_brand(created.brand_id).aliases == ()


def test_add_mcc_both_new_brand_keeps_aliases_on_brand_and_channel_members(repository):
    created = repository.apply_change(
        "add_mcc_both",
        {"name": "Two channel", "aliases": ["Alias"], "mcc": "5411"},
        7,
    )

    assert repository.get_brand(created.brand_id).aliases == ("Alias",)
    members = [
        member
        for channel_members in repository.list_brand_channels(created.brand_id).values()
        for member in channel_members
    ]
    assert {member.channel for member in members} == {"offline", "online"}
    assert all(member.aliases == ("Alias",) for member in members)
    assert repository.history()[0].summary == "добавлен MCC 5411 · офлайн и онлайн"


def test_add_mcc_both_preserves_existing_fact_note_and_rolls_back_both_on_error(repository):
    existing = add(repository, name="Existing", mcc=None)
    repository.apply_change(
        "add_mcc",
        {"merchant_id": existing.merchant_id, "mcc": "5411", "note": "old"},
        1,
    )
    result = repository.apply_change(
        "add_mcc_both",
        {"brand_id": existing.brand_id, "mcc": "5411", "note": "new"},
        2,
    )
    grouped = repository.list_brand_mcc(existing.brand_id)
    assert [(fact.channel, fact.note) for fact in grouped] == [
        ("offline", "old"),
        ("online", "new"),
    ]
    assert repository.history()[0].id == result.audit_id

    before = repository.list_brand_channels(existing.brand_id)
    with pytest.raises(StoreError), repository.transaction() as connection:
        repository.apply_change(
            "add_mcc_both",
            {
                "brand_id": existing.brand_id,
                "mcc": "5812",
                "evidence": {"file_id": "not durable"},
            },
            2,
            connection=connection,
        )
    assert repository.list_brand_channels(existing.brand_id) == before
    assert "5812" not in {fact.mcc for fact in repository.list_brand_mcc(existing.brand_id)}


def test_tannei_snapshot_counts_one_logical_support_and_keeps_source_provenance(repository):
    first = repository.import_store(
        source_metadata(store_id=1),
        source_evidence("5411") + source_evidence("5411"),
    )
    repository.import_store(source_metadata(store_id=2), source_evidence("5411"))
    snapshot = repository.tannei_snapshot(first.brand_id)
    support = snapshot["channels"]["offline"]["5411"]
    assert snapshot["source_count"] == 2
    assert support == {
        "support_count": 1,
        "first_seen": "2022-06",
        "last_seen": "2022-06",
        "source_store_ids": ["1", "2"],
    }
    assert repository.brand_has_tannei(first.brand_id)
    assert repository.brand_mcc_has_tannei(first.brand_id, "offline", "5411")
    assert not repository.brand_mcc_has_tannei(first.brand_id, "online", "5411")
    assert repository.list_brand_mcc(first.brand_id)[0].evidence_count == 1
    with repository.connection() as connection:
        private = json.loads(
            connection.execute(
                "SELECT snapshot_json FROM store_tannei_snapshots WHERE brand_id=?",
                (first.brand_id,),
            ).fetchone()[0]
        )
    observation_keys = [
        key for source in private["stores"].values() for key in source["observations"]
    ]
    assert len(observation_keys) == 3
    assert all(
        len(key.split(":")) == 3 and len(key.split(":")[1]) == 64 for key in observation_keys
    )
    revision = snapshot["revision"]
    assert not repository.import_store(
        source_metadata(store_id=1),
        source_evidence("5411") + source_evidence("5411"),
    ).changed
    assert repository.tannei_snapshot(first.brand_id)["revision"] == revision


def test_snapshot_merge_has_one_active_public_owner_and_revert_restores_both(repository):
    source = repository.import_store(
        source_metadata(store_id=1, network_id=10), source_evidence("5411")
    )
    target = repository.import_store(
        source_metadata(store_id=2, network_id=20), source_evidence("5812")
    )
    merged = repository.apply_change(
        "merge_brand", {"brand_id": source.brand_id, "target_id": target.brand_id}, 1
    )
    assert repository.tannei_snapshot(source.brand_id) is None
    assert repository.tannei_snapshot(target.brand_id)["source_count"] == 2
    repository.apply_change("revert", {"audit_id": merged.audit_id}, 1)
    assert repository.tannei_snapshot(source.brand_id)["source_count"] == 1
    assert repository.tannei_snapshot(target.brand_id)["source_count"] == 1
    assert repository.brand_mcc_has_tannei(source.brand_id, "offline", "5411")
    assert repository.brand_mcc_has_tannei(target.brand_id, "offline", "5812")


def test_import_batch_rolls_back_every_source_when_later_identity_conflicts(repository):
    changed = source_metadata(store_id=1, network_id=20)
    with pytest.raises(StoreError, match="network/channel"):
        repository.import_stores(
            [
                (source_metadata(store_id=1, network_id=10), source_evidence()),
                (changed, source_evidence("5812")),
            ]
        )
    assert repository.counts()["source_stores"] == 0
    assert repository.search("Евроопт").matches == ()


def test_compaction_is_idempotent_preserves_human_audits_and_legacy_merge_guard(repository):
    imported = repository.import_store(source_metadata(store_id=1), source_evidence())
    target = add(repository, name="Target", mcc=None)
    merge = repository.apply_change(
        "merge_merchant",
        {"merchant_id": imported.merchant_id, "target_id": target.merchant_id},
        91,
    )
    repository.import_store(source_metadata(store_id=2), source_evidence("5812"))
    with repository.transaction() as connection:
        # Emulate a pre-snapshot merge/import history while retaining human IDs.
        merge_row = connection.execute(
            "SELECT changes_json FROM store_audit WHERE id=?", (merge.audit_id,)
        ).fetchone()
        merge_edits = [
            edit for edit in json.loads(merge_row[0]) if edit["table"] != "store_tannei_snapshots"
        ]
        connection.execute(
            "UPDATE store_audit SET changes_json=? WHERE id=?",
            (json.dumps(merge_edits, ensure_ascii=False, separators=(",", ":")), merge.audit_id),
        )
        connection.execute("DELETE FROM store_audit WHERE kind='import' AND actor_id=0")
        connection.execute(
            "DELETE FROM store_tannei_merge_guards WHERE audit_id=?", (merge.audit_id,)
        )
    before_human = [(entry.id, entry.reverted_by) for entry in repository.history()]
    repository.initialize()
    first_snapshot = repository.tannei_snapshot(target.brand_id)
    tables = (
        "store_tannei_snapshots",
        "store_tannei_import_guards",
        "store_tannei_merge_guards",
        "store_sources",
        "store_evidence",
        "store_audit",
    )
    with repository.connection() as connection:
        compacted_rows = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in tables
        }
        assert connection.execute(
            "SELECT 1 FROM store_tannei_merge_guards WHERE audit_id=?", (merge.audit_id,)
        ).fetchone()
    repository.initialize()
    assert repository.tannei_snapshot(target.brand_id) == first_snapshot
    with repository.connection() as connection:
        assert {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in tables
        } == compacted_rows
    assert [(entry.id, entry.reverted_by) for entry in repository.history()] == before_human
    with pytest.raises(StaleChangeError, match="импортированные данные"):
        repository.apply_change("revert", {"audit_id": merge.audit_id}, 91)


def test_legacy_tannei_rows_compact_without_renumbering_human_history(repository):
    human = add(repository, name="Legacy source", mcc="5411")
    with repository.transaction() as connection:
        fact_id = connection.execute(
            "SELECT id FROM store_facts WHERE merchant_id=? AND mcc='5411'",
            (human.merchant_id,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO store_sources
            (id,source,store_id,merchant_id,network_id,metadata_json)
            VALUES(800,'tannei','77',?,'10',?)""",
            (human.merchant_id, json.dumps(source_metadata(store_id=77))),
        )
        details = {
            "payment_date": "2021-01",
            "merchant_type": "Legacy",
            "address_extra": None,
            "source_store_id": "77",
        }
        connection.execute(
            """INSERT INTO store_evidence
            (id,fact_id,source,source_key,details_json)
            VALUES(900,?,'tannei','77:legacy:0',?)""",
            (fact_id, json.dumps(details)),
        )
        connection.execute(
            """INSERT INTO store_audit
            (id,kind,merchant_id,brand_id,actor_id,changes_json)
            VALUES(1000,'import',?,?,0,'[]')""",
            (human.merchant_id, human.brand_id),
        )
        connection.execute("UPDATE store_audit SET reverted_by=12345 WHERE id=?", (human.audit_id,))
    repository.initialize()
    repository.initialize()
    with repository.connection() as connection:
        assert connection.execute("SELECT 1 FROM store_evidence WHERE id=900").fetchone() is None
        assert connection.execute("SELECT 1 FROM store_audit WHERE id=1000").fetchone() is None
        assert tuple(
            connection.execute(
                "SELECT id,reverted_by FROM store_audit WHERE id=?", (human.audit_id,)
            ).fetchone()
        ) == (human.audit_id, 12345)
        assert tuple(
            connection.execute(
                "SELECT id,store_id,network_id FROM store_sources WHERE id=800"
            ).fetchone()
        ) == (800, "77", "10")
        assert (
            connection.execute(
                "SELECT revision FROM store_tannei_import_guards WHERE store_source_id=800"
            ).fetchone()[0]
            == 1
        )
    snapshot = repository.tannei_snapshot(human.brand_id)
    assert snapshot["channels"]["offline"]["5411"]["support_count"] == 1
