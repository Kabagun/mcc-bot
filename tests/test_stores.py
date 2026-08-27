# Names intentionally exercise Cyrillic transliteration.
# ruff: noqa: RUF001

from __future__ import annotations

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
    assert [merchant.id for merchant in result.matches] == [
        cyrillic.merchant_id,
        green.merchant_id,
    ]
    assert [merchant.id for merchant in repository.search("Green").matches] == [
        green.merchant_id,
        cyrillic.merchant_id,
        longer.merchant_id,
    ]


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
