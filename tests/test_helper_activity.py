"""Helper-only search totals and private reports from existing moderation records."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from mcc_bot.community import CommunityService
from mcc_bot.helper_activity import HelperActivityRepository
from mcc_bot.stores import StoreRepository

NOW = datetime(2026, 11, 22, tzinfo=UTC).timestamp()
DAY = 86400


@pytest.fixture
def activity(tmp_path):
    stores = StoreRepository(tmp_path / "stores.sqlite3")
    community = CommunityService(stores, owner_id=1, allowed_mccs={"5411"})
    community.initialize()
    repository = HelperActivityRepository(stores)
    with stores.transaction() as conn:
        repository.initialize(conn)
        repository.initialize(conn)
    return community, repository


def submit(community, user_id, name):
    draft = community.begin(
        user_id,
        stage="evidence",
        data={
            "kind": "add_merchant",
            "payload": {"name": name, "channel": "offline", "mcc": "5411"},
        },
    )
    draft = community.advance(
        user_id, draft.id, draft.version, "preview", draft.data, media=("fixture", "unique")
    )
    return community.submit(user_id, draft.id, draft.version)


def test_summary_is_private_and_distinguishes_explicit_choices(activity):
    community, repository = activity
    for user_id in (2, 3, 4, 5):
        community.set_role(1, user_id, True)
    community.set_role(1, 3, True, role="superadmin")
    community.set_role(1, 5, False)
    community.set_digest(2, False)
    community.set_digest(3, True)
    community.refresh_role_profile(2, "fixture_helper", "Test", "Name")

    expected = repository.list_activity(1, owner_id=1, now=NOW)
    assert repository.list_activity(3, owner_id=1, now=NOW) == expected
    assert [(row.user_id, row.role, row.reminder_state) for row in expected] == [
        (3, "superadmin", "enabled"),
        (2, "admin", "disabled"),
        (4, "admin", "unanswered"),
    ]
    assert expected[1].username == "fixture_helper"
    assert expected[1].first_name == "Test"
    assert expected[1].last_name == "Name"
    assert expected[1].role_since is not None
    assert expected[2].username is None

    with community.stores.transaction() as conn:
        conn.execute("UPDATE community_roles SET digest_choice_recorded_at=NULL WHERE user_id=2")
    assert repository.list_activity(1, owner_id=1, now=NOW)[1].reminder_state == "legacy_disabled"

    for unauthorized_id in (0, True, 2, 5, 6, 2**63):
        with pytest.raises(PermissionError):
            repository.list_activity(unauthorized_id, owner_id=1, now=NOW)
    community.set_role(1, 3, True, role="admin")
    assert repository.list_activity(1, owner_id=1, now=NOW)[1].reminder_state == "unanswered"
    with pytest.raises(PermissionError):
        repository.list_activity(3, owner_id=1, now=NOW)


def test_lookup_counters_only_cover_active_helpers_and_expire(activity):
    community, repository = activity
    community.set_role(1, 2, True)
    community.set_role(1, 3, True)
    community.set_role(1, 3, True, role="superadmin")
    assert not repository.record_lookup(1, now=NOW)
    assert not repository.record_lookup(4, now=NOW)
    assert repository.record_lookup(2, now=NOW - 90 * DAY)
    assert repository.record_lookup(2, now=NOW - 89 * DAY)
    assert repository.record_lookup(2, now=NOW - 30 * DAY)
    assert repository.record_lookup(2, now=NOW - 29 * DAY)
    assert repository.record_lookup(2, now=NOW - 1)
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert all(executor.map(lambda _: repository.record_lookup(2, now=NOW), range(8)))
    assert repository.record_lookup(3, now=NOW)
    community.set_role(1, 3, False)
    assert not repository.record_lookup(3, now=NOW)

    (result,) = repository.list_activity(1, owner_id=1, now=NOW)
    assert (result.lookups, result.lookup_days) == (10, 3)
    (result,) = repository.list_activity(1, owner_id=1, days=1, now=NOW)
    assert (result.lookups, result.lookup_days) == (8, 1)
    (result,) = repository.list_activity(1, owner_id=1, days=90, now=NOW)
    assert (result.lookups, result.lookup_days) == (12, 5)
    with community.stores.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM helper_daily_activity").fetchone()[0] == 6

    repository.prune(now=NOW + 90 * DAY)
    with community.stores.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM helper_daily_activity").fetchone()[0] == 0


def test_contributions_and_joint_queue_decisions_are_attributed_separately(activity, monkeypatch):
    community, repository = activity
    earlier = NOW - 30 * DAY
    monkeypatch.setattr("mcc_bot.community.time.time", lambda: earlier)
    community.set_role(1, 2, True)
    old_publication = submit(community, 2, "Expired helper publication")
    assert old_publication.status == "approved"
    old_proposal = submit(community, 10, "Old pending proposal")

    monkeypatch.setattr("mcc_bot.community.time.time", lambda: NOW)
    direct = submit(community, 2, "Direct helper publication")
    assert direct.status == "approved"
    assert direct.reviewer_id == direct.user_id
    pending = submit(community, 3, "Proposal before access")
    community.set_role(1, 3, True)
    community.set_role(1, 4, True)
    approved = community.claim(4, pending.id, pending.version)
    community.review(4, approved.id, approved.version, "approved")
    rejected = community.claim(4, old_proposal.id, old_proposal.version)
    community.review(4, rejected.id, rejected.version, "rejected")

    pending = submit(community, 11, "Claimed proposal")
    community.claim(4, pending.id, pending.version)
    pending = submit(community, 12, "Clarification requested")
    clarification = community.claim(4, pending.id, pending.version)
    community.review(4, clarification.id, clarification.version, "clarification", reason="Verify")
    pending = submit(community, 13, "Cancelled after claim")
    claimed = community.claim(4, pending.id, pending.version)
    community.cancel(13, claimed.id, claimed.version)
    community.set_role(1, 13, True)

    # A real decision on one's own queued proposal still belongs to the queue.
    pending = submit(community, 14, "Self-reviewed proposal")
    community.set_role(1, 14, True)
    claimed = community.claim(14, pending.id, pending.version)
    community.review(14, claimed.id, claimed.version, "approved")

    assert {
        row.user_id: (row.contributions, row.published, row.reviewed, row.clarifications)
        for row in repository.list_activity(1, owner_id=1, now=NOW)
    } == {2: (1, 1, 0, 0), 3: (1, 1, 0, 0), 4: (0, 0, 2, 1), 13: (0, 0, 0, 0), 14: (1, 1, 1, 0)}
