"""Durable initial reminder invitations, explicit consent and delivery races."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from mcc_bot.community import AccessDenied, CommunityService, StaleAction
from mcc_bot.stores import StoreRepository

TODAY = "2026-11-16"
TOMORROW = "2026-11-17"


@pytest.fixture
def service(tmp_path):
    value = CommunityService(
        StoreRepository(tmp_path / "stores.sqlite3"), owner_id=1, allowed_mccs={"5411"}
    )
    value.initialize()
    value.set_role(1, 2, True)
    return value


def pending(service, user_id=100):
    draft = service.begin(
        user_id,
        data={
            "kind": "add_merchant",
            "payload": {"name": f"Synthetic shop {user_id}", "channel": "offline", "mcc": "5411"},
        },
    )
    draft = service.advance(
        user_id, draft.id, draft.version, "preview", draft.data, media=("synthetic-file", "fixture")
    )
    return service.submit(user_id, draft.id, draft.version)


def preference(service, user_id):
    with service.stores.connection() as connection:
        row = connection.execute(
            "SELECT digest,digest_choice_made FROM community_roles WHERE user_id=?", (user_id,)
        ).fetchone()
    return tuple(row) if row else None


def invitation(service, user_id):
    with service.stores.connection() as connection:
        row = connection.execute(
            "SELECT day,role_epoch,state FROM community_reminder_invitations WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return tuple(row) if row else None


def test_migration_preserves_subscribers_and_distinguishes_legacy_zero(tmp_path):
    stores = StoreRepository(tmp_path / "legacy.sqlite3")
    stores.initialize()
    with stores.transaction() as connection:
        connection.execute(
            """CREATE TABLE community_roles (
            user_id INTEGER PRIMARY KEY, active INTEGER NOT NULL DEFAULT 0,
            epoch INTEGER NOT NULL DEFAULT 0, digest INTEGER NOT NULL DEFAULT 0)"""
        )
        connection.executemany(
            "INSERT INTO community_roles VALUES(?,?,?,?)",
            [(2, 1, 7, 1), (3, 1, 8, 0), (4, 0, 9, 1)],
        )
    migrated = CommunityService(stores, owner_id=1, allowed_mccs={"5411"})
    migrated.initialize()
    pending(migrated)

    assert preference(migrated, 2) == (1, 1)
    assert preference(migrated, 3) == (0, 0)
    assert preference(migrated, 4) == (1, 1)
    assert migrated.role_epoch(2) == 7
    assert migrated.digest_candidates() == (2,)
    assert migrated.reminder_invitation_candidates() == (1, 3)
    assert migrated.reserve_reminder_invitation(2, TODAY) is None
    assert migrated.reserve_digest(2, TODAY) == 1
    migrated.set_digest(3, False)
    migrated.initialize()
    assert preference(migrated, 3) == (0, 1)
    assert migrated.reminder_invitation_candidates() == (1,)


def test_invitation_includes_only_current_reviewers_and_owner(service):
    service.set_role(1, 3, True)
    service.set_role(1, 3, True, role="superadmin")
    service.set_role(1, 4, True)
    service.set_role(1, 4, False)
    pending(service)

    assert service.reminder_invitation_candidates() == (1, 2, 3)
    assert service.reserve_reminder_invitation(4, TODAY) is None
    assert service.reserve_reminder_invitation(100, TODAY) is None
    assert service.reserve_reminder_invitation(1, TODAY) == (1, 0)
    assert service.reserve_reminder_invitation(3, TODAY) == (1, service.role_epoch(3))
    assert service.role(4) == "user"


def test_empty_queue_never_consumes_initial_or_daily_slot(service):
    assert service.reminder_invitation_candidates() == ()
    assert service.reserve_reminder_invitation(2, TODAY) is None
    assert invitation(service, 2) is None
    with service.stores.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM community_digests").fetchone()[0] == 0
    pending(service)
    assert service.reserve_reminder_invitation(2, TODAY) == (1, service.role_epoch(2))


def test_fully_claimed_queue_is_not_available_for_invitation(service):
    proposal = pending(service)
    service.claim(2, proposal.id, proposal.version)

    assert service.reminder_invitation_candidates() == ()
    assert service.reserve_reminder_invitation(1, TODAY) is None


@pytest.mark.parametrize("outcome", [None, "sent", "uncertain"])
def test_prompt_intent_is_durable_and_never_retried_after_restart(service, outcome):
    pending(service)
    count, epoch = service.reserve_reminder_invitation(2, TODAY)
    assert (count, epoch) == (1, service.role_epoch(2))
    assert invitation(service, 2) == (TODAY, epoch, "intent")
    assert service.refresh_reminder_invitation(2, TODAY, epoch) == 1
    if outcome is not None:
        service.finish_reminder_invitation(2, TODAY, epoch, outcome)
        assert service.refresh_reminder_invitation(2, TODAY, epoch) is None
    restarted = CommunityService(service.stores, owner_id=1, allowed_mccs={"5411"})
    restarted.initialize()

    assert restarted.reminder_invitation_candidates() == (1,)
    assert restarted.reserve_reminder_invitation(2, TODAY) is None
    assert restarted.reserve_reminder_invitation(2, TOMORROW) is None
    assert not restarted.digest_enabled(2)
    assert preference(restarted, 2) == (0, 0)


def test_explicit_refusal_blocks_initial_invitation_permanently(service):
    pending(service)
    service.set_digest(2, False, expected_epoch=service.role_epoch(2))

    assert preference(service, 2) == (0, 1)
    assert service.reminder_invitation_candidates() == (1,)
    assert service.reserve_reminder_invitation(2, TODAY) is None
    assert service.digest_candidates() == ()
    service.set_role(1, 2, False)
    service.set_role(1, 2, True)
    assert not service.digest_enabled(2)
    assert service.reserve_reminder_invitation(2, TOMORROW) is None


def test_prompt_consumes_daily_slot_even_when_user_immediately_opts_in(service):
    pending(service)
    _, epoch = service.reserve_reminder_invitation(2, TODAY)
    service.finish_reminder_invitation(2, TODAY, epoch, "sent")
    service.set_digest(2, True, expected_epoch=epoch)

    assert service.digest_candidates() == (2,)
    assert service.reserve_digest(2, TODAY) is None
    assert service.refresh_digest_count(2, TODAY) is None
    assert service.reserve_digest(2, TOMORROW) == 1
    assert service.refresh_digest_count(2, TOMORROW) == 1
    with service.stores.connection() as connection:
        assert (
            connection.execute(
                "SELECT state FROM community_digests WHERE day=? AND user_id=2", (TODAY,)
            ).fetchone()[0]
            == "invitation"
        )


@pytest.mark.parametrize("enabled", [False, True])
def test_explicit_choice_after_reservation_prevents_prompt(service, enabled):
    pending(service)
    _, epoch = service.reserve_reminder_invitation(2, TODAY)
    service.set_digest(2, enabled, expected_epoch=epoch)

    assert service.refresh_reminder_invitation(2, TODAY, epoch) is None
    service.finish_reminder_invitation(2, TODAY, epoch, "skipped")
    assert service.reserve_reminder_invitation(2, TOMORROW) is None


def test_queue_claim_race_can_be_skipped_and_reinvited_on_later_day(service):
    proposal = pending(service)
    _, epoch = service.reserve_reminder_invitation(2, TODAY)
    service.claim(2, proposal.id, proposal.version)

    assert service.refresh_reminder_invitation(2, TODAY, epoch) is None
    service.finish_reminder_invitation(2, TODAY, epoch, "skipped")
    pending(service, user_id=101)
    assert service.reserve_reminder_invitation(2, TODAY) is None
    assert service.reserve_reminder_invitation(2, TOMORROW) == (1, epoch)
    assert service.refresh_reminder_invitation(2, TOMORROW, epoch) == 1


def test_refresh_uses_current_available_queue_count(service):
    proposal = pending(service)
    pending(service, user_id=101)
    assert service.reserve_reminder_invitation(2, TODAY)[0] == 2
    service.claim(2, proposal.id, proposal.version)

    assert service.refresh_reminder_invitation(2, TODAY, service.role_epoch(2)) == 1


def test_role_change_invalidates_prompt_buttons_and_old_finish_is_harmless(service):
    pending(service)
    _, old_epoch = service.reserve_reminder_invitation(2, TODAY)
    service.set_role(1, 2, False)
    with pytest.raises(AccessDenied):
        service.set_digest(2, True, expected_epoch=old_epoch)
    service.set_role(1, 2, True)
    new_epoch = service.role_epoch(2)

    assert service.refresh_reminder_invitation(2, TODAY, old_epoch) is None
    with pytest.raises(StaleAction):
        service.set_digest(2, True, expected_epoch=old_epoch)
    assert preference(service, 2) == (0, 0)
    service.finish_reminder_invitation(2, TODAY, old_epoch, "skipped")
    assert service.reserve_reminder_invitation(2, TOMORROW) == (1, new_epoch)
    service.finish_reminder_invitation(2, TODAY, old_epoch, "sent")
    assert invitation(service, 2) == (TOMORROW, new_epoch, "intent")
    assert service.refresh_reminder_invitation(2, TOMORROW, new_epoch) == 1


def test_existing_opt_in_does_not_silently_resume_after_role_regrant(service):
    pending(service)
    service.set_digest(2, True)
    service.set_role(1, 2, False)
    service.set_role(1, 2, True)

    assert preference(service, 2) == (0, 1)
    assert service.digest_candidates() == ()
    assert service.reserve_digest(2, TOMORROW) is None
    assert service.reserve_reminder_invitation(2, TOMORROW) is None


def test_owner_choice_does_not_create_an_active_role(service):
    pending(service)
    service.set_digest(1, False, expected_epoch=0)

    assert preference(service, 1) == (0, 1)
    assert service.role(1) == "owner"
    assert service.reminder_invitation_candidates() == (2,)
    with service.stores.connection() as connection:
        assert (
            connection.execute("SELECT active FROM community_roles WHERE user_id=1").fetchone()[0]
            == 0
        )


def test_concurrent_reservations_produce_one_durable_intent(service):
    pending(service)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda _: service.reserve_reminder_invitation(2, TODAY), range(4))
        )

    assert results.count((1, service.role_epoch(2))) == 1
    assert results.count(None) == 3
    assert invitation(service, 2) == (TODAY, service.role_epoch(2), "intent")


def test_invalid_or_stale_finish_does_not_replace_delivery_outcome(service):
    pending(service)
    _, epoch = service.reserve_reminder_invitation(2, TODAY)
    with pytest.raises(ValueError, match="Invalid reminder invitation outcome"):
        service.finish_reminder_invitation(2, TODAY, epoch, "retry")
    service.finish_reminder_invitation(2, TOMORROW, epoch, "sent")
    service.finish_reminder_invitation(2, TODAY, epoch + 1, "sent")
    assert invitation(service, 2) == (TODAY, epoch, "intent")
    service.finish_reminder_invitation(2, TODAY, epoch, "uncertain")
    service.finish_reminder_invitation(2, TODAY, epoch, "skipped")
    assert invitation(service, 2) == (TODAY, epoch, "uncertain")


def test_helper_applications_are_work_only_for_current_role_managers(service):
    service.set_role(1, 3, True)
    service.set_role(1, 3, True, role="superadmin")
    service.request_role(100, None, None)
    service.request_role(101, None, None)

    assert service.available_helper_request_count(1) == 2
    assert service.available_helper_request_count(3) == 2
    assert service.available_helper_request_count(2) == 0
    assert service.available_helper_request_count(100) == 0
    assert service.reminder_work_counts(1) == {"data": 0, "helpers": 2}
    assert service.reminder_work_counts(2) == {"data": 0, "helpers": 0}
    assert service.reminder_invitation_candidates() == (1, 3)
    assert service.reserve_reminder_invitation(2, TODAY) is None
    assert service.reserve_reminder_invitation(1, TODAY) == (2, service.role_epoch(1))
    service.set_digest(3, True)
    assert service.reserve_digest(3, TODAY) == 2


def test_work_breakdown_combines_available_data_and_unresolved_requests(service):
    service.set_role(1, 3, True)
    service.set_role(1, 3, True, role="superadmin")
    first = pending(service)
    pending(service, user_id=101)
    service.request_role(102, None, None)
    service.request_role(103, None, None)
    service.set_role(1, 103, True)
    service.request_role(104, None, None)
    service.decline_role(1, 104, service.role_epoch(104))
    service.claim(2, first.id, first.version)

    assert service.reminder_work_counts(1) == {"data": 1, "helpers": 1}
    assert service.reminder_work_counts(3) == {"data": 1, "helpers": 1}
    assert service.reminder_work_counts(2) == {"data": 1, "helpers": 0}
    assert service.reminder_work_counts(102) == {"data": 0, "helpers": 0}
    assert service.reserve_reminder_invitation(3, TODAY)[0] == 2
    assert service.reserve_reminder_invitation(2, TODAY)[0] == 1


def test_application_resolution_race_rechecks_both_invitation_and_daily_count(service):
    service.set_role(1, 3, True)
    service.set_role(1, 3, True, role="superadmin")
    service.request_role(100, None, None)
    _, epoch = service.reserve_reminder_invitation(1, TODAY)
    service.set_digest(3, True)
    assert service.reserve_digest(3, TODAY) == 1
    service.set_role(1, 100, True)

    assert service.available_helper_request_count(1) == 0
    assert service.refresh_reminder_invitation(1, TODAY, epoch) is None
    assert service.refresh_digest_count(3, TODAY) is None
    assert service.reminder_invitation_candidates() == ()


def test_superadmin_demotion_removes_application_work_without_resubscribing(service):
    service.set_role(1, 3, True)
    service.set_role(1, 3, True, role="superadmin")
    service.request_role(100, None, None)
    _, epoch = service.reserve_reminder_invitation(3, TODAY)
    service.set_role(1, 3, True, role="admin")

    assert service.available_helper_request_count(3) == 0
    assert service.reminder_work_counts(3) == {"data": 0, "helpers": 0}
    assert service.refresh_reminder_invitation(3, TODAY, epoch) is None
    assert not service.digest_enabled(3)
    with pytest.raises(StaleAction):
        service.set_digest(3, True, expected_epoch=epoch)
