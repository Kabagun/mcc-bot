"""Private aggregate activity for the current helper roster."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from .stores import StoreRepository

LOOKUP_RETENTION_DAYS = 90


@dataclass(frozen=True)
class HelperActivity:
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    role: str
    reminder_state: str
    lookups: int
    lookup_days: int
    contributions: int
    published: int
    reviewed: int
    clarifications: int
    role_since: float | None


def _utc_day(now: float | None) -> date:
    return (datetime.now(UTC) if now is None else datetime.fromtimestamp(now, UTC)).date()


def _valid_user_id(user_id: int) -> bool:
    return isinstance(user_id, int) and not isinstance(user_id, bool) and 0 < user_id < 2**63


class HelperActivityRepository:
    """Read helper work and keep aggregate counts per helper and UTC day."""

    def __init__(self, stores: StoreRepository) -> None:
        self.stores = stores

    def initialize(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS helper_daily_activity (
                day TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                lookups INTEGER NOT NULL DEFAULT 0 CHECK(lookups >= 0),
                clarifications INTEGER NOT NULL DEFAULT 0 CHECK(clarifications >= 0),
                PRIMARY KEY(day,user_id)) WITHOUT ROWID"""
        )

    @staticmethod
    def _prune(conn: sqlite3.Connection, day: date) -> None:
        oldest = day - timedelta(days=LOOKUP_RETENTION_DAYS - 1)
        conn.execute("DELETE FROM helper_daily_activity WHERE day<?", (oldest.isoformat(),))

    def prune(self, *, now: float | None = None) -> None:
        """Remove lookup counters outside the rolling UTC retention window."""

        with self.stores.transaction() as conn:
            self._prune(conn, _utc_day(now))

    def record_lookup(self, user_id: int, *, now: float | None = None) -> bool:
        """Increment a lookup count only while the user has an active helper role."""

        if not _valid_user_id(user_id):
            return False
        with self.stores.connection() as conn:
            # Keep ordinary traffic off the SQLite writer. The INSERT rechecks
            # eligibility atomically if a role is revoked after this read.
            active_role = conn.execute(
                "SELECT 1 FROM community_roles WHERE user_id=? AND active=1", (user_id,)
            ).fetchone()
            if active_role is None:
                return False
        day = _utc_day(now)
        with self.stores.transaction() as conn:
            added = conn.execute(
                """INSERT INTO helper_daily_activity(day,user_id,lookups)
                   SELECT ?,user_id,1 FROM community_roles WHERE user_id=? AND active=1
                   ON CONFLICT(day,user_id) DO UPDATE SET lookups=lookups+1""",
                (day.isoformat(), user_id),
            ).rowcount
            self._prune(conn, day)
            return bool(added)

    def record_clarification(self, conn: sqlite3.Connection, user_id: int, *, now: float) -> None:
        """Count a clarification in the same transaction as the queue action."""

        conn.execute(
            """INSERT INTO helper_daily_activity(day,user_id,clarifications) VALUES(?,?,1)
               ON CONFLICT(day,user_id) DO UPDATE SET clarifications=clarifications+1""",
            (_utc_day(now).isoformat(), user_id),
        )

    def list_activity(
        self,
        viewer_id: int,
        *,
        owner_id: int | None,
        days: int = 30,
        now: float | None = None,
    ) -> tuple[HelperActivity, ...]:
        """Summarize current helpers over the given number of UTC calendar days."""

        if not _valid_user_id(viewer_id):
            raise PermissionError("Helper activity requires an owner or active superadmin")
        if not 1 <= days <= LOOKUP_RETENTION_DAYS:
            raise ValueError(f"days must be between 1 and {LOOKUP_RETENTION_DAYS}")
        today = _utc_day(now)
        start = today - timedelta(days=days - 1)
        end = today + timedelta(days=1)
        with self.stores.connection() as conn:
            # Read authorization and the roster from the same snapshot.
            conn.execute("BEGIN")
            if viewer_id != owner_id:
                superadmin = conn.execute(
                    "SELECT 1 FROM community_roles "
                    "WHERE user_id=? AND active=1 AND role='superadmin'",
                    (viewer_id,),
                ).fetchone()
                if superadmin is None:
                    raise PermissionError("Helper activity requires an owner or active superadmin")
            # Auto-published proposals share their creation and finalization
            # timestamps and go directly from version 1 to 2. Queue decisions
            # are made after a claim, which already advances the version.
            rows = conn.execute(
                """WITH activity AS (
                       SELECT user_id,SUM(lookups) AS lookups,SUM(lookups>0) AS lookup_days,
                              SUM(clarifications) AS clarifications
                       FROM helper_daily_activity WHERE day>=:start_day AND day<:end_day
                       GROUP BY user_id
                   ), contributions AS (
                       SELECT user_id,COUNT(*) AS total,SUM(status='approved') AS published
                       FROM community_proposals
                       WHERE created_at>=:start_ts AND created_at<:end_ts AND status<>'cancelled'
                       GROUP BY user_id
                   ), reviews AS (
                       SELECT reviewer_id,COUNT(*) AS total FROM community_proposals
                       WHERE finalized_at>=:start_ts AND finalized_at<:end_ts
                         AND status IN ('approved','rejected') AND reviewer_id IS NOT NULL
                         AND NOT (status='approved' AND reviewer_id=user_id
                                  AND finalized_at=created_at AND version=2)
                       GROUP BY reviewer_id
                   )
                   SELECT r.user_id,p.username,p.first_name,p.last_name,r.role,
                          CASE WHEN r.digest=1 THEN 'enabled'
                               WHEN r.digest_choice_made=1 AND r.digest_choice_recorded_at IS NULL
                                 THEN 'legacy_disabled'
                               WHEN r.digest_choice_made=1 THEN 'disabled'
                               ELSE 'unanswered' END AS reminder_state,
                          COALESCE(a.lookups,0) AS lookups,COALESCE(a.lookup_days,0) AS lookup_days,
                          COALESCE(a.clarifications,0) AS clarifications,
                          COALESCE(c.total,0) AS contributions,COALESCE(v.total,0) AS reviewed,
                          COALESCE(c.published,0) AS published,
                          (SELECT created_at FROM community_role_events e
                           WHERE e.user_id=r.user_id AND e.active=1 AND e.role=r.role
                           ORDER BY e.id DESC LIMIT 1) AS role_since
                   FROM community_roles r
                   LEFT JOIN community_role_profiles p ON p.user_id=r.user_id
                   LEFT JOIN activity a ON a.user_id=r.user_id
                   LEFT JOIN contributions c ON c.user_id=r.user_id
                   LEFT JOIN reviews v ON v.reviewer_id=r.user_id
                   WHERE r.active=1 AND r.user_id IS NOT :owner_id
                   ORDER BY r.role DESC,r.user_id""",
                {
                    "start_day": start.isoformat(),
                    "end_day": end.isoformat(),
                    "start_ts": datetime.combine(start, time.min, UTC).timestamp(),
                    "end_ts": datetime.combine(end, time.min, UTC).timestamp(),
                    "owner_id": owner_id,
                },
            )
            return tuple(HelperActivity(**row) for row in rows)
