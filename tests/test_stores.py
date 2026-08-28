# Names intentionally exercise Cyrillic transliteration.
# ruff: noqa: RUF001

from __future__ import annotations

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
    assert repository.import_store(source_metadata(), source_evidence()).audit_id == 0
    with repository.connection() as connection:
        assert (
            '"payment_date":"2022-06"'
            in connection.execute("SELECT details_json FROM store_evidence ORDER BY id").fetchone()[
                0
            ]
        )


def test_import_history_explains_visible_changes_without_evidence_metadata(repository):
    imported = repository.import_store(source_metadata(), source_evidence())
    entry = repository.history(imported.merchant_id)[0]
    details = "\n".join(entry.details)
    assert entry.kind == "import"
    assert entry.actor_id == 0
    assert "Добавлен магазин «Евроопт»" in details
    assert "Добавлен MCC: 5411" in details
    assert "Подтверждения tannei.by для MCC 5411: +1" in details
    assert "Добавлены записи источника: 1" in details
    assert "2022-06" not in details
    assert "Groceries" not in details
    assert "address" not in details


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
    assert repository.list_mcc(merchant_id, include_archived=True)[0].evidence_count == 2
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
    assert repository.list_mcc(target.merchant_id, include_archived=True)[0].evidence_count == 2


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
    assert repository.list_mcc(target.merchant_id)[0].evidence_count == 2


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


def test_reimport_after_reverting_added_source_evidence_keeps_it_revoked(repository):
    first = repository.import_store(source_metadata(), source_evidence())
    later = repository.import_store(source_metadata(), source_evidence("5812"))
    repository.apply_change("revert", {"audit_id": later.audit_id}, 1)
    assert repository.import_store(source_metadata(), source_evidence("5812")).audit_id == 0
    assert [fact.mcc for fact in repository.list_mcc(first.merchant_id)] == ["5411"]


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
