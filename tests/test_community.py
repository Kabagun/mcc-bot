"""State, authorization, transaction, concurrency and retention regressions."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from mcc_bot.community import (
    CLARIFICATION_SECONDS,
    LEASE_SECONDS,
    MEDIA_RETENTION_SECONDS,
    AccessDenied,
    CommunityError,
    CommunityService,
    StaleAction,
)
from mcc_bot.stores import StoreRepository


@pytest.fixture
def community(tmp_path):
    service = CommunityService(StoreRepository(tmp_path / "stores.sqlite3"), owner_id=1)
    service.initialize()
    service.set_role(1, 2, True)
    service.set_role(1, 3, True)
    return service


def make_draft(service, user_id=10, *, kind="add_merchant", payload=None, media=True):
    payload = payload or {"name": "Test Shop", "channel": "offline", "mcc": "5411"}
    draft = service.begin(user_id, stage="evidence", data={"kind": kind, "payload": payload})
    return service.advance(
        user_id,
        draft.id,
        draft.version,
        "preview",
        draft.data,
        media=("secret-file-token", "unique-photo") if media else None,
    )


def make_proposal(service, user_id=10, **kwargs):
    draft = make_draft(service, user_id, **kwargs)
    return service.submit(user_id, draft.id, draft.version)


def tannei_metadata(store_id=1, network_id=10):
    return {
        "id": store_id,
        "network_id": network_id,
        "network_name": "Imported",
        "name": "Imported branch",
        "is_online": False,
        "address": None,
    }


def tannei_observations(mcc="5411"):
    return [
        {
            "mcc": mcc,
            "payment_date": "2026-08",
            "merchant_type": "Shop",
            "address_extra": None,
        }
    ]


def test_owner_is_explicit_and_user_id_authority(community):
    assert community.role(1) == "owner"
    assert community.role(2) == "admin"
    assert community.role(10) == "user"
    no_owner = CommunityService(community.stores)
    assert no_owner.role(1) == "user"
    with pytest.raises(AccessDenied):
        no_owner.set_role(1, 11, True)
    with pytest.raises(AccessDenied):
        community.role(-1)


def test_role_requests_do_not_grant_or_subscribe(community):
    assert community.role_request_status(10) is None
    assert community.helper_count() == 2
    community.request_role(10, "candidate", "Alice", "Smith")
    community.request_role(10, "candidate_new", "Alice", "Updated")
    assert community.role_request_status(10) == "pending"
    assert community.role(10) == "user"
    assert not community.digest_enabled(10)
    candidate = community.role_candidates(1)[0]
    assert candidate["username"] == "candidate_new"
    assert (candidate["first_name"], candidate["last_name"]) == ("Alice", "Updated")
    with pytest.raises(AccessDenied):
        community.set_role(2, 10, True)
    with pytest.raises(AccessDenied):
        community.role_candidates(10)
    with pytest.raises(StaleAction):
        community.set_role(1, 11, True, require_pending=True)
    community.decline_role(1, 10, 0)
    assert community.role_request_status(10) == "declined"
    with pytest.raises(StaleAction):
        community.decline_role(1, 10, 0)
    community.request_role(10, None, "Alice")
    community.set_role(1, 10, True, require_pending=True)
    assert community.is_admin(10)
    assert community.role_request_status(10) == "granted"
    assert community.helper_count() == 3


def test_role_profile_schema_is_additive_and_survives_restart(community):
    with community.stores.transaction() as connection:
        connection.execute("DROP TABLE community_role_profiles")
    reopened = CommunityService(community.stores, owner_id=1)
    reopened.initialize()
    reopened.request_role(12, "helper_12", "Helper", "Twelve")
    restarted = CommunityService(community.stores, owner_id=1)
    restarted.initialize()
    candidate = next(item for item in restarted.role_candidates(1) if item["user_id"] == 12)
    assert candidate["username"] == "helper_12"
    assert candidate["first_name"] == "Helper"


def test_audit_actor_uses_stored_identity_and_stable_id(community):
    community.request_role(10, "helper_name", "Alice", "Smith")
    community.set_role(1, 10, True, require_pending=True)
    actor = community.audit_actor(10, 10)
    assert actor == {
        "user_id": 10,
        "username": "helper_name",
        "first_name": "Alice",
        "last_name": "Smith",
        "automated": False,
    }
    assert community.audit_actor(10, 0)["automated"]
    assert community.audit_actor(10, 999)["user_id"] == 999
    with pytest.raises(AccessDenied):
        community.audit_actor(11, 10)


def test_role_profile_refresh_is_limited_and_tracks_latest_helper_username(community):
    assert not community.refresh_role_profile(20, "ordinary", "Ordinary")
    community.request_role(10, "candidate", "Alice")
    assert community.refresh_role_profile(10, "candidate_new", "Alice", "Updated")
    community.set_role(1, 10, True, require_pending=True)
    assert community.refresh_role_profile(10, "helper_new", "Alice", "Helper")
    assert community.refresh_role_profile(1, "owner_name", "Owner")
    helper = community.audit_actor(1, 10)
    owner = community.audit_actor(1, 1)
    assert (helper["username"], helper["last_name"]) == ("helper_new", "Helper")
    assert owner["username"] == "owner_name"
    with community.stores.connection() as connection:
        assert (
            connection.execute("SELECT 1 FROM community_role_profiles WHERE user_id=20").fetchone()
            is None
        )


def test_role_revoke_invalidates_review_draft_consent_and_regrant(community):
    proposal = make_proposal(community)
    claimed = community.claim(2, proposal.id, proposal.version)
    draft = community.begin(2, privileged=True)
    community.set_digest(2, True)
    community.set_role(1, 2, False)
    assert community.draft(2) is None
    assert not community.digest_enabled(2)
    with pytest.raises(AccessDenied):
        community.review(2, proposal.id, claimed.version, "approved")
    community.set_role(1, 2, True)
    assert not community.digest_enabled(2)
    with pytest.raises(StaleAction):
        community.cancel_draft(2, draft.id, draft.version)
    with pytest.raises(StaleAction):
        community.review(2, proposal.id, claimed.version, "approved")
    with pytest.raises(CommunityError):
        community.set_role(1, 1, False)


def test_draft_restart_and_duplicate_updates(community):
    draft = community.begin(10)
    moved = community.advance(10, draft.id, 1, "choose", {"name": "Shop"}, update_id=100)
    reopened = CommunityService(community.stores, owner_id=1)
    assert reopened.draft(10) == moved
    with pytest.raises(StaleAction):
        reopened.advance(10, moved.id, moved.version, "channel", moved.data, update_id=100)
    assert reopened.draft(10).stage == "choose"
    with pytest.raises(StaleAction):
        community.advance(11, moved.id, moved.version, "preview", {})


def test_screenshot_optional_for_users_and_admins(community):
    draft = make_draft(community, media=False)
    queued = community.submit(10, draft.id, draft.version)
    assert queued.status == "pending"
    draft = make_draft(community, 2, media=False)
    saved = community.submit(2, draft.id, draft.version)
    assert saved.status == "approved"
    assert saved.audit_id is not None
    assert len(community.stores.find_exact("Test Shop", "offline")) == 1


def test_direct_save_repeated_callback_does_not_duplicate_fact(community):
    draft = make_draft(community, 2)
    saved = community.submit(2, draft.id, draft.version)
    with pytest.raises(StaleAction):
        community.submit(2, draft.id, draft.version)
    assert len(community.stores.history()) == 1
    assert community.proposal(2, saved.id).status == "approved"


def test_photo_not_retained_in_draft_proposal_or_audit(community):
    draft = make_draft(community, 2)
    result = community.submit(2, draft.id, draft.version)
    assert community.media_for(2, result.id) == "secret-file-token"
    with community.stores.connection() as conn:
        for table in ("community_drafts", "community_proposals", "store_audit", "store_evidence"):
            assert "secret-file-token" not in str(
                [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            )
    community.expire_media(now=10**12)
    assert community.media_for(2, result.id) is None
    assert community.stores.list_mcc(1)[0].mcc == "5411"
    assert community.stores.history()


def test_screenshot_access_current_role_and_submitter_only(community):
    proposal = make_proposal(community)
    assert community.media_for(10, proposal.id)
    assert community.media_for(2, proposal.id)
    with pytest.raises(AccessDenied):
        community.media_for(11, proposal.id)
    community.set_role(1, 2, False)
    with pytest.raises(AccessDenied):
        community.media_for(2, proposal.id)


def test_reject_requires_reason_and_expiry_is_five_days(community):
    proposal = make_proposal(community)
    claimed = community.claim(2, proposal.id, proposal.version, now=100)
    with pytest.raises(CommunityError):
        community.review(2, proposal.id, claimed.version, "rejected", now=101)
    result = community.review(
        2, proposal.id, claimed.version, "rejected", reason="Unreadable", now=101
    )
    assert result.reason == "Unreadable"
    assert community.expire_media(now=101 + MEDIA_RETENTION_SECONDS - 1) == 0
    assert community.expire_media(now=101 + MEDIA_RETENTION_SECONDS) == 1


def test_claim_lease_renew_and_takeover_boundary(community):
    proposal = make_proposal(community)
    claimed = community.claim(2, proposal.id, proposal.version, now=100)
    with pytest.raises(StaleAction):
        community.claim(3, proposal.id, claimed.version, now=999)
    renewed = community.claim(2, proposal.id, claimed.version, now=200)
    assert renewed.lease_until == 200 + LEASE_SECONDS
    taken = community.claim(3, proposal.id, renewed.version, now=200 + LEASE_SECONDS)
    with pytest.raises(StaleAction):
        community.review(2, proposal.id, renewed.version, "approved", now=1101)
    assert (
        community.review(3, proposal.id, taken.version, "approved", now=1101).status == "approved"
    )


def test_live_claim_is_hidden_from_queue_until_the_fifteen_minute_boundary(community):
    proposal = make_proposal(community)
    assert [item.id for item in community.queue(3, now=100)] == [proposal.id]
    claimed = community.claim(2, proposal.id, proposal.version, now=100)
    assert community.queue(2, now=999) == ()
    assert community.queue(3, now=999) == ()
    assert [item.id for item in community.queue(3, now=1000)] == [proposal.id]
    with pytest.raises(StaleAction, match="другого помощника"):
        community.claim(3, proposal.id, claimed.version, now=999)


def test_one_concurrent_reviewer_wins(community):
    proposal = make_proposal(community)

    def claim(actor):
        try:
            return community.claim(actor, proposal.id, proposal.version)
        except StaleAction:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (2, 3)))
    assert sum(result is not None for result in results) == 1
    winner = next(result for result in results if result)
    result = community.review(winner.reviewer_id, proposal.id, winner.version, "approved")
    assert result.status == "approved"
    with pytest.raises(StaleAction):
        community.review(winner.reviewer_id, proposal.id, winner.version, "approved")
    assert len(community.stores.history()) == 1


def test_publication_and_proposal_roll_back_together(community, monkeypatch):
    proposal = make_proposal(community)
    claimed = community.claim(2, proposal.id, proposal.version)
    original = community.stores.apply_change

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("database failure after audit")

    monkeypatch.setattr(community.stores, "apply_change", crash)
    with pytest.raises(RuntimeError):
        community.review(2, proposal.id, claimed.version, "approved")
    assert community.proposal(2, proposal.id).status == "pending"
    assert not community.stores.find_exact("Test Shop", "offline")
    assert not community.stores.history()
    monkeypatch.setattr(community.stores, "apply_change", original)
    assert community.review(2, proposal.id, claimed.version, "approved").status == "approved"


def test_cancel_own_pending_invalidates_claim(community):
    proposal = make_proposal(community)
    claimed = community.claim(2, proposal.id, proposal.version)
    with pytest.raises(AccessDenied):
        community.cancel(11, proposal.id, claimed.version)
    result = community.cancel(10, proposal.id, claimed.version)
    assert result.status == "cancelled"
    with pytest.raises(StaleAction):
        community.review(2, proposal.id, claimed.version, "approved")


def test_clarification_response_is_versioned_and_reuses_original_evidence(community):
    proposal = make_proposal(community)
    claimed = community.claim(2, proposal.id, proposal.version)
    asked = community.review(2, proposal.id, claimed.version, "clarification", reason="Which shop?")
    assert not community.queue(3)
    draft = community.respond(10, proposal.id, asked.version)
    draft = community.advance(
        10, draft.id, draft.version, "preview", {**draft.data, "comment": "Town centre"}
    )
    result = community.submit(10, draft.id, draft.version)
    assert result.id == proposal.id
    assert result.status == "pending"
    assert result.comment == "Town centre"
    assert community.media_for(10, result.id)
    with pytest.raises(StaleAction):
        community.respond(10, proposal.id, asked.version)


def test_cancelled_clarification_response_returns_the_original_proposal(community):
    proposal = make_proposal(community)
    claimed = community.claim(2, proposal.id, proposal.version, now=100)
    asked = community.review(
        2,
        proposal.id,
        claimed.version,
        "clarification",
        reason="Which shop?",
        now=101,
    )
    response = community.respond(10, proposal.id, asked.version)
    with pytest.raises(StaleAction, match="cancel_response"):
        community.cancel_draft(10, response.id, response.version)
    returned = community.cancel_response(10, response.id, response.version)
    assert returned.status == "pending"
    assert returned.reason == "Which shop?"
    assert returned.reviewer_id is None
    assert returned.lease_until is None
    assert community.draft(10) is None
    assert [item.id for item in community.queue(3, now=102)] == [proposal.id]


def test_unanswered_clarification_is_requeued_after_twenty_four_hours(community):
    proposal = make_proposal(community)
    claimed = community.claim(2, proposal.id, proposal.version, now=100)
    asked = community.review(
        2,
        proposal.id,
        claimed.version,
        "clarification",
        reason="Which shop?",
        now=101,
    )
    community.respond(10, proposal.id, asked.version)
    assert community.queue(3, now=101 + CLARIFICATION_SECONDS - 1) == ()

    queued = community.queue(3, now=101 + CLARIFICATION_SECONDS)

    assert [item.id for item in queued] == [proposal.id]
    restored = community.proposal(3, proposal.id)
    assert restored.status == "pending"
    assert restored.reviewer_id is None
    assert restored.lease_until is None
    assert restored.reason == (
        "Which shop?\n\nПользователь не ответил на уточнение."  # noqa: RUF001
    )
    assert community.draft(10) is None
    assert community.requeue_expired_clarifications(now=101 + CLARIFICATION_SECONDS) == 0


def test_name_report_is_text_only_and_admin_editor_is_not_public(community):
    merchant = community.stores.apply_change(
        "add_merchant", {"name": "Original", "channel": "offline"}, 1
    )
    proposal = make_proposal(
        community,
        kind="rename_merchant",
        payload={"merchant_id": merchant.merchant_id, "name": "Fixed"},
        media=False,
    )
    claimed = community.claim(3, proposal.id, proposal.version)
    community.review(3, proposal.id, claimed.version, "approved")
    assert community.stores.get(merchant.merchant_id).name == "Fixed"
    draft = make_draft(
        community,
        kind="archive_merchant",
        payload={"merchant_id": merchant.merchant_id},
        media=False,
    )
    with pytest.raises(AccessDenied):
        community.submit(10, draft.id, draft.version)


def test_explicit_replace_and_independent_support_survives_undo(community):
    merchant = community.stores.apply_change(
        "add_merchant", {"name": "Shop", "channel": "offline", "mcc": "5411"}, 1
    )
    payload = {"merchant_id": merchant.merchant_id, "mcc": "5812"}
    proposal = make_proposal(community, kind="add_mcc", payload=payload)
    claimed = community.claim(2, proposal.id, proposal.version)
    result = community.review(2, proposal.id, claimed.version, "approved", replace_old="5411")
    assert [fact.mcc for fact in community.stores.list_mcc(merchant.merchant_id)] == ["5812"]
    second = make_proposal(community, 11, kind="add_mcc", payload=payload)
    claimed2 = community.claim(3, second.id, second.version)
    community.review(3, second.id, claimed2.version, "approved")
    community.stores.apply_change("revert", {"audit_id": result.audit_id}, 1)
    assert "5812" in {fact.mcc for fact in community.stores.list_mcc(merchant.merchant_id)}


def test_duplicate_new_merchant_race_is_not_silently_merged(community):
    first = make_proposal(community)
    second = make_proposal(community, 11)
    a = community.claim(2, first.id, first.version)
    b = community.claim(3, second.id, second.version)
    community.review(2, first.id, a.version, "approved")
    with pytest.raises(CommunityError, match="уже есть"):
        community.review(3, second.id, b.version, "approved")
    assert community.proposal(3, second.id).status == "pending"


def test_payload_and_media_metadata_are_bounded(community):
    with pytest.raises(CommunityError):
        community.begin(10, data={"nested": {"file_id": "secret"}})
    draft = community.begin(10)
    with pytest.raises(CommunityError):
        community.advance(10, draft.id, draft.version, "preview", {}, media=("x" * 513, "id"))
    draft = make_draft(community, payload={"name": "Shop", "channel": "offline", "mcc": "not-mcc"})
    with pytest.raises(CommunityError):
        community.submit(10, draft.id, draft.version)


def test_unknown_mcc_is_rejected_for_users_helpers_and_preexisting_proposals(tmp_path):
    stores = StoreRepository(tmp_path / "strict.sqlite3")
    permissive = CommunityService(stores, owner_id=1, allowed_mccs={"5411", "1233"})
    permissive.initialize()
    permissive.set_role(1, 2, True)
    proposal = make_proposal(
        permissive,
        payload={"name": "Old proposal", "channel": "offline", "mcc": "1233"},
    )

    strict = CommunityService(stores, owner_id=1, allowed_mccs={"5411"})
    strict.initialize()
    for user_id in (10, 2):
        draft = make_draft(
            strict,
            user_id,
            payload={"name": f"Unknown {user_id}", "channel": "offline", "mcc": "1233"},
            media=False,
        )
        with pytest.raises(CommunityError, match="MCC 1233 не найден"):
            strict.submit(user_id, draft.id, draft.version)

    claimed = strict.claim(2, proposal.id, proposal.version)
    with pytest.raises(CommunityError, match="MCC 1233 не найден"):
        strict.review(2, proposal.id, claimed.version, "approved")
    assert strict.proposal(2, proposal.id).status == "pending"
    assert strict.stores.find_exact("Old proposal", "offline") == ()


def test_pending_input_quota(community):
    for number in range(20):
        make_proposal(
            community, payload={"name": f"Shop {number}", "channel": "offline", "mcc": "5411"}
        )
    draft = make_draft(community)
    with pytest.raises(CommunityError, match="20"):
        community.submit(10, draft.id, draft.version)


def test_direct_structural_preview_rejects_later_rename(community):
    merchant = community.stores.apply_change(
        "add_merchant", {"name": "Shop", "channel": "offline"}, 1
    )
    draft = make_draft(
        community,
        2,
        kind="rename_merchant",
        media=False,
        payload={"merchant_id": merchant.merchant_id, "name": "First preview"},
    )
    community.stores.apply_change(
        "rename_merchant", {"merchant_id": merchant.merchant_id, "name": "Later edit"}, 3
    )
    with pytest.raises(StaleAction, match="изменились"):
        community.submit(2, draft.id, draft.version)
    assert community.stores.get(merchant.merchant_id).name == "Later edit"
    assert not community.own_proposals(2)


def test_two_structural_reviewers_cannot_silently_overwrite(community):
    merchant = community.stores.apply_change(
        "add_merchant", {"name": "Shop", "channel": "offline"}, 1
    )
    a = make_proposal(
        community,
        kind="rename_merchant",
        media=False,
        payload={"merchant_id": merchant.merchant_id, "name": "First"},
    )
    b = make_proposal(
        community,
        11,
        kind="rename_merchant",
        media=False,
        payload={"merchant_id": merchant.merchant_id, "name": "Second"},
    )
    a = community.claim(2, a.id, a.version)
    b = community.claim(3, b.id, b.version)
    community.review(2, a.id, a.version, "approved")
    with pytest.raises(StaleAction):
        community.review(3, b.id, b.version, "approved")
    b = community.claim(3, b.id, b.version)
    community.review(3, b.id, b.version, "approved")
    assert community.stores.get(merchant.merchant_id).name == "Second"


def test_merge_snapshot_checks_target_and_source(community):
    first = community.stores.apply_change(
        "add_merchant", {"name": "First", "channel": "offline"}, 1
    )
    second = community.stores.apply_change(
        "add_merchant", {"name": "Second", "channel": "offline"}, 1
    )
    draft = make_draft(
        community,
        2,
        kind="merge_merchant",
        media=False,
        payload={"merchant_id": first.merchant_id, "target_id": second.merchant_id},
    )
    community.stores.apply_change(
        "aliases", {"merchant_id": second.merchant_id, "aliases": ["Other"]}, 3
    )
    with pytest.raises(StaleAction):
        community.submit(2, draft.id, draft.version)
    assert community.stores.get(first.merchant_id)


def test_old_mcc_new_support_blocks_stale_replacement_but_not_additive_approval(community):
    merchant = community.stores.apply_change(
        "add_merchant", {"name": "Shop", "channel": "offline", "mcc": "5411"}, 1
    )
    proposed = make_proposal(
        community, kind="add_mcc", payload={"merchant_id": merchant.merchant_id, "mcc": "5812"}
    )
    claimed = community.claim(2, proposed.id, proposed.version)
    community.stores.apply_change(
        "add_mcc", {"merchant_id": merchant.merchant_id, "mcc": "5411"}, 3
    )
    with pytest.raises(StaleAction):
        community.review(2, proposed.id, claimed.version, "approved", replace_old="5411")
    # Adding an independent variant never overwrites the old support.
    result = community.review(2, proposed.id, claimed.version, "approved")
    assert result.status == "approved"
    assert {fact.mcc for fact in community.stores.list_mcc(merchant.merchant_id)} == {
        "5411",
        "5812",
    }


def test_review_touch_keeps_original_snapshot_version_and_rejects_expired_or_foreign(community):
    merchant = community.stores.apply_change(
        "add_merchant", {"name": "Shop", "channel": "offline"}, 1
    )
    proposed = make_proposal(
        community,
        kind="rename_merchant",
        media=False,
        payload={"merchant_id": merchant.merchant_id, "name": "Preview"},
    )
    claimed = community.claim(2, proposed.id, proposed.version, now=100)
    with community.stores.connection() as conn:
        before = dict(conn.execute("SELECT * FROM community_review_snapshots").fetchone())
    community.stores.apply_change(
        "rename_merchant", {"merchant_id": merchant.merchant_id, "name": "Newer edit"}, 3
    )
    touched = community.touch_review(2, proposed.id, claimed.version, now=200)
    assert touched.version == claimed.version
    assert touched.lease_until == 1100
    with community.stores.connection() as conn:
        assert dict(conn.execute("SELECT * FROM community_review_snapshots").fetchone()) == before
    with pytest.raises(StaleAction):
        community.review(2, proposed.id, claimed.version, "approved", now=201)
    with pytest.raises(StaleAction):
        community.touch_review(3, proposed.id, claimed.version, now=201)
    with pytest.raises(StaleAction):
        community.touch_review(2, proposed.id, claimed.version - 1, now=201)
    with pytest.raises(StaleAction):
        community.touch_review(2, proposed.id, claimed.version, now=1100)
    assert community.proposal(2, proposed.id).lease_until == 1100
    community.set_role(1, 2, False)
    with pytest.raises(AccessDenied):
        community.touch_review(2, proposed.id, claimed.version, now=202)


def test_helper_can_correct_and_revert_tannei_backed_public_facts(community):
    imported = community.stores.import_store(tannei_metadata(), tannei_observations())
    raw_snapshot = community.stores.tannei_snapshot(imported.brand_id)
    assert community.can_edit_brand(2, imported.brand_id)
    assert community.can_edit_mcc(2, imported.brand_id, "offline", "5411")
    assert community.can_edit_mcc(2, imported.brand_id, "offline", "5812")
    assert not community.can_edit_brand(10, imported.brand_id)
    assert not community.can_edit_mcc(10, imported.brand_id, "offline", "5411")

    note = make_draft(
        community,
        2,
        kind="edit_mcc_note",
        payload={"merchant_id": imported.merchant_id, "mcc": "5411", "note": "stale source"},
        media=False,
    )
    assert community.submit(2, note.id, note.version).status == "approved"
    assert community.stores.list_mcc(imported.merchant_id)[0].note == "stale source"

    replacement = make_draft(
        community,
        2,
        kind="replace_mcc",
        payload={"merchant_id": imported.merchant_id, "old_mcc": "5411", "mcc": "5812"},
        media=False,
    )
    replaced = community.submit(2, replacement.id, replacement.version)
    assert replaced.status == "approved"
    assert [fact.mcc for fact in community.stores.list_mcc(imported.merchant_id)] == ["5812"]
    assert community.stores.tannei_snapshot(imported.brand_id) == raw_snapshot

    undo_replace = make_draft(
        community,
        2,
        kind="revert",
        payload={"audit_id": replaced.audit_id},
        media=False,
    )
    assert community.submit(2, undo_replace.id, undo_replace.version).status == "approved"
    assert [fact.mcc for fact in community.stores.list_mcc(imported.merchant_id)] == ["5411"]

    archive = make_draft(
        community,
        2,
        kind="archive_mcc",
        payload={"merchant_id": imported.merchant_id, "mcc": "5411"},
        media=False,
    )
    archived = community.submit(2, archive.id, archive.version)
    assert archived.status == "approved"
    assert community.stores.list_mcc(imported.merchant_id) == ()
    assert community.stores.tannei_snapshot(imported.brand_id) == raw_snapshot

    undo_archive = make_draft(
        community,
        2,
        kind="revert",
        payload={"audit_id": archived.audit_id},
        media=False,
    )
    assert community.submit(2, undo_archive.id, undo_archive.version).status == "approved"
    assert [fact.mcc for fact in community.stores.list_mcc(imported.merchant_id)] == ["5411"]


def test_helper_can_add_manual_channel_and_both_mcc_to_tannei_brand(community):
    imported = community.stores.import_store(tannei_metadata(), tannei_observations())
    channel = make_draft(
        community,
        2,
        kind="add_merchant",
        payload={
            "brand_id": imported.brand_id,
            "name": "Imported online",
            "channel": "online",
            "mcc": "5732",
        },
        media=False,
    )
    assert community.submit(2, channel.id, channel.version).status == "approved"

    both = make_draft(
        community,
        2,
        kind="add_mcc_both",
        payload={"brand_id": imported.brand_id, "mcc": "5812", "note": "manual"},
        media=False,
    )
    published = community.submit(2, both.id, both.version)
    assert published.status == "approved"
    facts = community.stores.list_brand_mcc(imported.brand_id)
    assert {(fact.channel, fact.mcc) for fact in facts} >= {
        ("offline", "5812"),
        ("online", "5812"),
    }

    undo = make_draft(
        community,
        2,
        kind="revert",
        payload={"audit_id": published.audit_id},
        media=False,
    )
    assert community.submit(2, undo.id, undo.version).status == "approved"
    assert "5812" not in {fact.mcc for fact in community.stores.list_brand_mcc(imported.brand_id)}


def test_tannei_brand_names_and_merges_use_ordinary_helper_rights(community):
    imported = community.stores.import_store(tannei_metadata(), tannei_observations())
    rename = make_draft(
        community,
        2,
        kind="edit_brand_names",
        payload={"brand_id": imported.brand_id, "name": "Changed", "aliases": ["Alias"]},
        media=False,
    )
    assert community.submit(2, rename.id, rename.version).status == "approved"
    assert community.stores.get_brand(imported.brand_id).name == "Changed"

    human = community.stores.apply_change("add_merchant", {"name": "Human"}, 1)
    merge = make_draft(
        community,
        2,
        kind="merge_brand",
        payload={"brand_id": imported.brand_id, "target_id": human.brand_id},
        media=False,
    )
    assert community.submit(2, merge.id, merge.version).status == "approved"
    assert community.stores.get_brand(imported.brand_id) is None
    assert community.stores.tannei_snapshot(human.brand_id)["source_count"] == 1
