"""Durable private contributions, role authorization and atomic moderation.

Media references live only in ``community_media``; merchant audit payloads never
contain Telegram file identifiers. All authorization and fact-changing decisions
share the merchant repository's immediate SQLite transaction.
"""

# Russian user-facing copy deliberately contains Cyrillic look-alike letters.
# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlparse

from .descriptions import DescriptionCatalog
from .stores import StoreRepository, normalize_store_name

LEASE_SECONDS = 15 * 60
CLARIFICATION_SECONDS = 24 * 60 * 60
MEDIA_RETENTION_SECONDS = 5 * 86400
MAX_COMMENT = 1000
MAX_NAME = 160
MAX_NOTE = 48
MAX_MEDIA_BYTES = 10 * 1024 * 1024
FINAL_STATES = frozenset({"approved", "rejected", "cancelled"})
MCC_KINDS = frozenset({"add_merchant", "add_mcc", "add_mcc_both", "replace_mcc"})
PUBLIC_KINDS = MCC_KINDS | {"rename_merchant", "merge_merchant", "card_partnership"}
FORM_KINDS = frozenset(
    {"store_metadata", "mcc_save", "mcc_delete", "partner_save", "partner_delete"}
)
PUBLIC_KINDS = PUBLIC_KINDS | FORM_KINDS
EDIT_KINDS = PUBLIC_KINDS | {
    "aliases",
    "archive_merchant",
    "archive_mcc",
    "revert",
    "rename_brand",
    "brand_aliases",
    "edit_brand_names",
    "set_brand_membership",
    "merge_brand",
    "edit_mcc_note",
}
STRUCTURAL_KINDS = frozenset(
    {
        "rename_merchant",
        "aliases",
        "archive_merchant",
        "merge_merchant",
        "add_mcc_both",
        "replace_mcc",
        "archive_mcc",
        "rename_brand",
        "brand_aliases",
        "edit_brand_names",
        "set_brand_membership",
        "merge_brand",
        "edit_mcc_note",
    }
)


class CommunityError(ValueError):
    """A safe, user-facing validation or stale-action error."""


class AccessDenied(CommunityError):
    """The current Telegram user no longer has the required permission."""


class StaleAction(CommunityError):
    """The version or review lease no longer matches the displayed action."""


class PartnerPublisher(Protocol):
    """Atomic adapter required for publishing a reviewed card partnership."""

    def create_offer(
        self,
        payload: Any,
        *,
        actor_id: int,
        connection: sqlite3.Connection | None = None,
    ) -> Any:
        """Create and audit one partner offer in the caller's transaction."""


@dataclass(frozen=True)
class Draft:
    """Versioned conversation state without a Telegram media identifier."""

    id: str
    user_id: int
    version: int
    stage: str
    data: dict[str, Any]


@dataclass(frozen=True)
class Proposal:
    """A contribution visible only to its submitter and current reviewers."""

    id: int
    user_id: int
    kind: str
    payload: dict[str, Any]
    status: str
    version: int
    comment: str
    reason: str | None
    reviewer_id: int | None
    lease_until: float | None
    audit_id: int | None
    fingerprint: str | None = None


def clean_text(value: str, *, maximum: int = MAX_COMMENT) -> str:
    """Validate bounded plain text and remove surrounding whitespace."""

    value = value.strip()
    if not value or len(value) > maximum or any(ord(ch) < 32 and ch != "\n" for ch in value):
        raise CommunityError(f"Введите текст длиной от 1 до {maximum} символов.")
    return value


def _identity_text(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > maximum or any(ord(ch) < 32 for ch in value):
        return None
    return value


def _telegram_username(value: Any) -> str | None:
    value = _identity_text(value, maximum=32)
    if value is None:
        return None
    value = value.removeprefix("@")
    return value if re.fullmatch(r"[A-Za-z0-9_]{1,32}", value) else None


def _safe_json(data: dict[str, Any]) -> str:
    # Whitelisting contribution payloads happens at publication. This guard also
    # prevents accidentally copying a Telegram photo object into a durable draft.
    def inspect(value: Any) -> None:
        if isinstance(value, dict):
            if any("file_id" in str(key) or "file_unique_id" in str(key) for key in value):
                raise CommunityError("Вложение нужно передать отдельно от текста.")
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)

    inspect(data)
    result = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(result) > 12000:
        raise CommunityError("Слишком много данных в предложении.")
    return result


class CommunityService:
    """Manage contributions and roles in the same database as merchant facts."""

    def __init__(
        self,
        stores: StoreRepository,
        owner_id: int | None = None,
        *,
        allowed_mccs: Collection[str] | None = None,
        partners: PartnerPublisher | None = None,
        catalog: Any | None = None,
    ) -> None:
        self.stores = stores
        if owner_id is not None and (isinstance(owner_id, bool) or owner_id <= 0):
            raise ValueError("Owner must be an explicit positive Telegram user ID")
        self.owner_id = owner_id
        self.partners = partners
        self.catalog = catalog
        source = DescriptionCatalog.from_file().labels if allowed_mccs is None else allowed_mccs
        self.allowed_mccs = frozenset(source)

    def is_known_mcc(self, mcc: str) -> bool:
        """Return whether a normalized MCC is allowed in public data."""

        return mcc in self.allowed_mccs

    def initialize(self) -> None:
        """Create additive durable state and clean expired screenshot references."""

        self.stores.initialize()
        with self.stores.transaction() as conn:
            for statement in (
                """CREATE TABLE IF NOT EXISTS community_roles (
                    user_id INTEGER PRIMARY KEY, active INTEGER NOT NULL DEFAULT 0,
                    epoch INTEGER NOT NULL DEFAULT 0, digest INTEGER NOT NULL DEFAULT 0)""",
                """CREATE TABLE IF NOT EXISTS community_role_requests (
                    user_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
                    created_at REAL NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS community_role_profiles (
                    user_id INTEGER PRIMARY KEY, username TEXT,
                    first_name TEXT, last_name TEXT, updated_at REAL NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS community_role_events (
                    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                    actor_id INTEGER NOT NULL, active INTEGER NOT NULL,
                    created_at REAL NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS community_drafts (
                    user_id INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
                    version INTEGER NOT NULL, stage TEXT NOT NULL, data TEXT NOT NULL,
                    privileged INTEGER NOT NULL, role_epoch INTEGER NOT NULL,
                    updated_at REAL NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS community_editor_messages (
                    user_id INTEGER PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS community_proposals (
                    id INTEGER PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    comment TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
                    reviewer_id INTEGER, lease_until REAL, reason TEXT,
                    audit_id INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    finalized_at REAL, fingerprint TEXT)""",
                """CREATE TABLE IF NOT EXISTS community_media (
                    id INTEGER PRIMARY KEY, draft_id TEXT UNIQUE, proposal_id INTEGER UNIQUE,
                    file_id TEXT NOT NULL, unique_id TEXT NOT NULL,
                    created_at REAL NOT NULL, expires_at REAL)""",
                """CREATE TABLE IF NOT EXISTS community_updates (
                    update_id INTEGER PRIMARY KEY, created_at REAL NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS community_digests (
                    day TEXT NOT NULL, user_id INTEGER NOT NULL,
                    state TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(day,user_id))""",
                """CREATE TABLE IF NOT EXISTS community_review_snapshots (
                    proposal_id INTEGER PRIMARY KEY, version INTEGER NOT NULL,
                    state TEXT NOT NULL)""",
                """CREATE INDEX IF NOT EXISTS community_proposals_queue
                    ON community_proposals(status,created_at)""",
            ):
                conn.execute(statement)
            proposal_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(community_proposals)")
            }
            if "fingerprint" not in proposal_columns:
                conn.execute("ALTER TABLE community_proposals ADD COLUMN fingerprint TEXT")
            conn.execute("DROP INDEX IF EXISTS community_active_fingerprint")
            for row in conn.execute(
                "SELECT id,kind,payload FROM community_proposals WHERE fingerprint IS NULL"
            ).fetchall():
                conn.execute(
                    "UPDATE community_proposals SET fingerprint=? WHERE id=?",
                    (self._fingerprint(row["kind"], json.loads(row["payload"])), row["id"]),
                )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS community_active_fingerprint
                ON community_proposals(fingerprint,status)"""
            )
        self.expire_media()

    def _role(self, conn: sqlite3.Connection, user_id: int) -> tuple[str, int]:
        if (
            not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or not 0 < user_id <= 2**63 - 1
        ):
            raise AccessDenied("Используйте личный чат с ботом.")
        row = conn.execute(
            "SELECT active,epoch FROM community_roles WHERE user_id=?", (user_id,)
        ).fetchone()
        if user_id == self.owner_id:
            return "owner", row["epoch"] if row else 0
        return ("admin" if row and row["active"] else "user", row["epoch"] if row else 0)

    def role(self, user_id: int) -> str:
        """Return the role from current authority, never from chat or username."""

        with self.stores.connection() as conn:
            return self._role(conn, user_id)[0]

    def is_admin(self, user_id: int) -> bool:
        """Check whether the user is a currently active reviewer or owner."""

        return self.role(user_id) in {"admin", "owner"}

    def can_edit_brand(self, user_id: int, brand_id: int) -> bool:
        """Return ordinary helper/owner edit access for a brand."""

        del brand_id
        with self.stores.connection() as conn:
            return self._role(conn, user_id)[0] in {"admin", "owner"}

    def can_edit_mcc(self, user_id: int, brand_id: int, channel: str, mcc: str) -> bool:
        """Return ordinary helper/owner edit access for a public MCC fact."""

        del brand_id, channel, mcc
        with self.stores.connection() as conn:
            return self._role(conn, user_id)[0] in {"admin", "owner"}

    def brand_has_confirmed_mcc(self, brand_id: int) -> bool:
        """Return whether a store currently has at least one confirmed active MCC."""

        with self.stores.connection() as conn:
            return bool(self.stores.list_brand_mcc_groups(brand_id, connection=conn))

    def role_epoch(self, user_id: int) -> int:
        """Return the role generation used to reject stale consent and role buttons."""

        with self.stores.connection() as conn:
            return self._role(conn, user_id)[1]

    def role_request_status(self, user_id: int) -> str | None:
        """Return the current helper-application status, when one exists."""

        with self.stores.connection() as conn:
            self._role(conn, user_id)
            row = conn.execute(
                "SELECT status FROM community_role_requests WHERE user_id=?", (user_id,)
            ).fetchone()
        return str(row["status"]) if row else None

    def helper_count(self) -> int:
        """Return the number of active helpers, excluding the configured owner."""

        with self.stores.connection() as conn:
            if self.owner_id is None:
                row = conn.execute("SELECT COUNT(*) FROM community_roles WHERE active=1").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM community_roles WHERE active=1 AND user_id<>?",
                    (self.owner_id,),
                ).fetchone()
        return int(row[0])

    def _require_admin(self, conn: sqlite3.Connection, user_id: int) -> None:
        if self._role(conn, user_id)[0] not in {"admin", "owner"}:
            raise AccessDenied("Это действие доступно только действующим помощникам.")

    def set_role(
        self,
        actor_id: int,
        user_id: int,
        active: bool,
        *,
        expected_epoch: int | None = None,
        require_pending: bool = False,
    ) -> None:
        """Grant/revoke a helper as owner; optionally require a pending application."""

        with self.stores.transaction() as conn:
            if self._role(conn, actor_id)[0] != "owner":
                raise AccessDenied("Только владелец может менять роли.")
            role, epoch = self._role(conn, user_id)
            if role == "owner":
                raise CommunityError("Роль владельца задаётся настройкой бота.")
            if expected_epoch is not None and epoch != expected_epoch:
                raise StaleAction("Роль уже изменилась. Откройте управление заново.")
            if active and require_pending:
                request = conn.execute(
                    "SELECT status FROM community_role_requests WHERE user_id=?", (user_id,)
                ).fetchone()
                if request is None or request["status"] != "pending":
                    raise StaleAction("Заявка уже рассмотрена или не существует.")
            if (role == "admin") == active:
                return
            conn.execute(
                """INSERT INTO community_roles(user_id,active,epoch,digest) VALUES(?,?,?,0)
                   ON CONFLICT(user_id) DO UPDATE SET active=excluded.active,
                   epoch=excluded.epoch,digest=0""",
                (user_id, int(active), epoch + 1),
            )
            conn.execute(
                "INSERT INTO community_role_events(user_id,actor_id,active,created_at) "
                "VALUES(?,?,?,?)",
                (user_id, actor_id, int(active), time.time()),
            )
            self._discard_draft(conn, user_id)
            conn.execute(
                """UPDATE community_proposals SET reviewer_id=NULL,lease_until=NULL,
                   version=version+1 WHERE reviewer_id=? AND status='pending'""",
                (user_id,),
            )
            conn.execute(
                "UPDATE community_role_requests SET status=? WHERE user_id=?",
                ("granted" if active else "revoked", user_id),
            )

    def request_role(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None = None,
    ) -> None:
        """Record an applicant identity and request without granting permissions."""

        with self.stores.transaction() as conn:
            if self._role(conn, user_id)[0] != "user":
                raise CommunityError("У вас уже есть доступ к очереди.")
            now = time.time()
            conn.execute(
                """INSERT INTO community_role_profiles(
                    user_id,username,first_name,last_name,updated_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                    first_name=excluded.first_name,last_name=excluded.last_name,
                    updated_at=excluded.updated_at""",
                (
                    user_id,
                    _telegram_username(username),
                    _identity_text(first_name, maximum=128),
                    _identity_text(last_name, maximum=128),
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO community_role_requests(user_id,status,created_at)
                   VALUES(?,'pending',?) ON CONFLICT(user_id) DO UPDATE SET
                   status='pending',created_at=excluded.created_at""",
                (user_id, now),
            )

    def refresh_role_profile(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None = None,
    ) -> bool:
        """Refresh the identity used only in authorized moderation views."""

        with self.stores.transaction() as conn:
            self._role(conn, user_id)
            conn.execute(
                """INSERT INTO community_role_profiles(
                    user_id,username,first_name,last_name,updated_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                    first_name=excluded.first_name,last_name=excluded.last_name,
                    updated_at=excluded.updated_at""",
                (
                    user_id,
                    _telegram_username(username),
                    _identity_text(first_name, maximum=128),
                    _identity_text(last_name, maximum=128),
                    time.time(),
                ),
            )
            return True

    def proposal_author(self, viewer_id: int, proposal_id: int) -> dict[str, Any]:
        """Return an author's stored identity to a current helper only."""

        with self.stores.connection() as conn:
            self._require_admin(conn, viewer_id)
            proposal = self._proposal(conn, proposal_id)
            profile = conn.execute(
                "SELECT username,first_name,last_name FROM community_role_profiles WHERE user_id=?",
                (proposal.user_id,),
            ).fetchone()
            return {
                "user_id": proposal.user_id,
                "username": profile["username"] if profile else None,
                "first_name": profile["first_name"] if profile else None,
                "last_name": profile["last_name"] if profile else None,
            }

    def role_candidates(self, actor_id: int) -> tuple[dict[str, Any], ...]:
        """Return pending requests and active helper roles to the owner only."""

        with self.stores.connection() as conn:
            if self._role(conn, actor_id)[0] != "owner":
                raise AccessDenied("Только владелец может просматривать роли.")
            return tuple(
                dict(row)
                for row in conn.execute(
                    """SELECT ids.user_id,COALESCE(r.active,0) AS active,
                   COALESCE(r.epoch,0) AS epoch,q.status AS request_status,
                   q.created_at,p.username,p.first_name,p.last_name FROM (
                   SELECT user_id FROM community_role_requests WHERE status='pending'
                   UNION SELECT user_id FROM community_roles WHERE active=1) ids
                   LEFT JOIN community_roles r ON r.user_id=ids.user_id
                   LEFT JOIN community_role_requests q ON q.user_id=ids.user_id
                   LEFT JOIN community_role_profiles p ON p.user_id=ids.user_id
                   ORDER BY COALESCE(r.active,0),COALESCE(q.created_at,0),ids.user_id"""
                )
                if row["user_id"] != self.owner_id
            )

    def audit_actor(self, viewer_id: int, actor_id: int) -> dict[str, Any]:
        """Return a stored audit identity to an authorized helper or owner."""

        with self.stores.connection() as conn:
            self._require_admin(conn, viewer_id)
            if actor_id == 0:
                return {
                    "user_id": 0,
                    "username": None,
                    "first_name": None,
                    "last_name": None,
                    "automated": True,
                }
            profile = conn.execute(
                """SELECT username,first_name,last_name
                   FROM community_role_profiles WHERE user_id=?""",
                (actor_id,),
            ).fetchone()
            return {
                "user_id": actor_id,
                "username": profile["username"] if profile else None,
                "first_name": profile["first_name"] if profile else None,
                "last_name": profile["last_name"] if profile else None,
                "automated": False,
            }

    def decline_role(self, actor_id: int, user_id: int, expected_epoch: int) -> None:
        """Decline a still-pending helper request without changing any active role."""

        with self.stores.transaction() as conn:
            if self._role(conn, actor_id)[0] != "owner":
                raise AccessDenied("Только владелец может менять роли.")
            role, epoch = self._role(conn, user_id)
            if role != "user" or epoch != expected_epoch:
                raise StaleAction("Роль уже изменилась.")
            changed = conn.execute(
                "UPDATE community_role_requests SET status='declined' "
                "WHERE user_id=? AND status='pending'",
                (user_id,),
            ).rowcount
            if not changed:
                raise StaleAction("Заявка уже рассмотрена.")
            conn.execute(
                "INSERT INTO community_roles(user_id,active,epoch,digest) VALUES(?,0,?,0) "
                "ON CONFLICT(user_id) DO UPDATE SET epoch=excluded.epoch",
                (user_id, epoch + 1),
            )

    def digest_enabled(self, user_id: int) -> bool:
        """Return current opt-in; revoked users are never treated as subscribers."""

        with self.stores.connection() as conn:
            if self._role(conn, user_id)[0] == "user":
                return False
            row = conn.execute(
                "SELECT digest FROM community_roles WHERE user_id=?", (user_id,)
            ).fetchone()
            return bool(row and row[0])

    def set_digest(self, user_id: int, enabled: bool, *, expected_epoch: int | None = None) -> None:
        """Set explicit consent for the current reviewer, independently of queue access."""

        with self.stores.transaction() as conn:
            self._require_admin(conn, user_id)
            if expected_epoch is not None and self._role(conn, user_id)[1] != expected_epoch:
                raise StaleAction("Доступ изменился. Откройте управление заново.")
            conn.execute(
                """INSERT INTO community_roles(user_id,active,epoch,digest) VALUES(?,0,0,?)
                   ON CONFLICT(user_id) DO UPDATE SET digest=excluded.digest""",
                (user_id, int(enabled)),
            )

    def _discard_draft(self, conn: sqlite3.Connection, user_id: int) -> None:
        conn.execute(
            """UPDATE community_media SET expires_at=? WHERE draft_id IN
               (SELECT id FROM community_drafts WHERE user_id=?) AND expires_at IS NULL""",
            (time.time() + MEDIA_RETENTION_SECONDS, user_id),
        )
        conn.execute("DELETE FROM community_editor_messages WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM community_drafts WHERE user_id=?", (user_id,))

    def bind_editor_message(
        self, user_id: int, draft_id: str, chat_id: int, message_id: int
    ) -> None:
        """Persist the single Telegram message used by the active form editor."""

        if chat_id <= 0 or message_id <= 0:
            raise CommunityError("Не удалось открыть редактор.")
        with self.stores.transaction() as conn:
            self._draft(conn, user_id, draft_id)
            conn.execute(
                """INSERT INTO community_editor_messages(user_id,draft_id,chat_id,message_id)
                   VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                   draft_id=excluded.draft_id,chat_id=excluded.chat_id,
                   message_id=excluded.message_id""",
                (user_id, draft_id, chat_id, message_id),
            )

    def editor_message(self, user_id: int, draft_id: str) -> tuple[int, int] | None:
        """Return the bound Telegram editor message if it still belongs to this draft."""

        with self.stores.connection() as conn:
            self._draft(conn, user_id, draft_id)
            row = conn.execute(
                """SELECT chat_id,message_id FROM community_editor_messages
                   WHERE user_id=? AND draft_id=?""",
                (user_id, draft_id),
            ).fetchone()
            return (row["chat_id"], row["message_id"]) if row else None

    def begin(
        self,
        user_id: int,
        *,
        stage: str = "name",
        data: dict[str, Any] | None = None,
        privileged: bool = False,
    ) -> Draft:
        """Start a durable draft, expiring any abandoned draft's media separately."""

        with self.stores.transaction() as conn:
            role, epoch = self._role(conn, user_id)
            if privileged and role == "user":
                raise AccessDenied("Нет доступа к редактированию.")
            self._discard_draft(conn, user_id)
            draft_id = uuid.uuid4().hex[:12]
            data = dict(data or {})
            if stage == "preview" and data.get("kind") in EDIT_KINDS:
                data["expected"] = self._snapshot(conn, data["kind"], data["payload"])
            conn.execute(
                """INSERT INTO community_drafts VALUES(?,?,1,?,?,?,?,?)""",
                (
                    user_id,
                    draft_id,
                    stage,
                    _safe_json(data),
                    int(privileged),
                    epoch,
                    time.time(),
                ),
            )
            return self._draft(conn, user_id)

    def _draft(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        draft_id: str | None = None,
        version: int | None = None,
    ) -> Draft:
        row = conn.execute("SELECT * FROM community_drafts WHERE user_id=?", (user_id,)).fetchone()
        if (
            not row
            or (draft_id is not None and row["id"] != draft_id)
            or (version is not None and row["version"] != version)
        ):
            raise StaleAction("Эта кнопка устарела. Откройте текущий шаг или начните заново.")
        role, epoch = self._role(conn, user_id)
        if row["privileged"] and (role == "user" or epoch != row["role_epoch"]):
            raise AccessDenied("Доступ изменился. Начните заново из текущего меню.")
        return Draft(row["id"], user_id, row["version"], row["stage"], json.loads(row["data"]))

    def draft(self, user_id: int) -> Draft | None:
        """Load the active draft after rechecking current role and generation."""

        with self.stores.connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM community_drafts WHERE user_id=?", (user_id,)
            ).fetchone()
            return self._draft(conn, user_id) if exists else None

    def draft_has_media(self, user_id: int, draft_id: str | None = None) -> bool:
        """Report whether the caller's current draft has a live screenshot reference."""

        with self.stores.connection() as conn:
            draft = self._draft(conn, user_id, draft_id)
            return bool(
                conn.execute(
                    "SELECT 1 FROM community_media WHERE draft_id=? AND expires_at IS NULL",
                    (draft.id,),
                ).fetchone()
            )

    def advance(
        self,
        user_id: int,
        draft_id: str,
        version: int,
        stage: str,
        data: dict[str, Any],
        *,
        update_id: int | None = None,
        media: tuple[str, str] | None = None,
    ) -> Draft:
        """Atomically advance one draft version and deduplicate Telegram updates."""

        with self.stores.transaction() as conn:
            draft = self._draft(conn, user_id, draft_id, version)
            if stage in {"reason", "review_preview"} and draft.data.get("proposal_id"):
                self._validate_review(
                    conn,
                    user_id,
                    draft.data["proposal_id"],
                    draft.data["proposal_version"],
                    time.time(),
                )
            if update_id is not None:
                try:
                    conn.execute(
                        "INSERT INTO community_updates VALUES(?,?)", (update_id, time.time())
                    )
                except sqlite3.IntegrityError as exc:
                    raise StaleAction("Сообщение уже обработано.") from exc
            if media:
                if any(
                    not isinstance(value, str) or not value or len(value) > 512 for value in media
                ):
                    raise CommunityError("Не удалось сохранить ссылку на изображение.")
                conn.execute(
                    """INSERT INTO community_media(draft_id,file_id,unique_id,created_at)
                       VALUES(?,?,?,?) ON CONFLICT(draft_id) DO UPDATE SET
                       file_id=excluded.file_id,unique_id=excluded.unique_id,
                       created_at=excluded.created_at,expires_at=NULL""",
                    (draft.id, media[0], media[1], time.time()),
                )
            data = dict(data)
            if (
                stage == "preview"
                and draft.stage != "preview"
                and data.get("kind") in EDIT_KINDS
                and "expected" not in data
            ):
                data["expected"] = self._snapshot(conn, data["kind"], data["payload"])
            conn.execute(
                """UPDATE community_drafts SET stage=?,data=?,version=version+1,updated_at=?
                   WHERE user_id=?""",
                (stage, _safe_json(data), time.time(), user_id),
            )
            return self._draft(conn, user_id)

    def cancel_draft(self, user_id: int, draft_id: str, version: int) -> None:
        """Discard only the active draft shown by a current cancel button."""

        with self.stores.transaction() as conn:
            draft = self._draft(conn, user_id, draft_id, version)
            if draft.data.get("response_id"):
                raise StaleAction("Ответ на уточнение нужно вернуть через cancel_response().")
            self._discard_draft(conn, user_id)

    def cancel_response(self, user_id: int, draft_id: str, version: int) -> Proposal:
        """Discard a clarification response and return its proposal to review atomically."""

        with self.stores.transaction() as conn:
            draft = self._draft(conn, user_id, draft_id, version)
            proposal_id = draft.data.get("response_id")
            proposal_version = draft.data.get("response_version")
            if not isinstance(proposal_id, int) or not isinstance(proposal_version, int):
                raise StaleAction("Ответ на уточнение уже недоступен.")
            proposal = self._proposal(conn, proposal_id)
            if (
                proposal.user_id != user_id
                or proposal.status != "clarification"
                or proposal.version != proposal_version
            ):
                raise StaleAction("Предложение уже вернулось на проверку.")
            now = time.time()
            conn.execute(
                """UPDATE community_proposals SET status='pending',version=version+1,
                   reviewer_id=NULL,lease_until=NULL,updated_at=? WHERE id=?""",
                (now, proposal_id),
            )
            self._discard_draft(conn, user_id)
            return self._proposal(conn, proposal_id)

    def _validate_payload(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        schemas = {
            "add_merchant": (
                {"name", "channel", "mcc"},
                {"brand_id", "note", "aliases", "source_url"},
            ),
            "add_mcc": ({"merchant_id", "mcc"}, {"note", "source_url"}),
            "add_mcc_both": (
                {"mcc"},
                {"brand_id", "name", "note", "aliases", "source_url"},
            ),
            "replace_mcc": (
                {"merchant_id", "old_mcc", "mcc"},
                {"note", "merchant_ids", "channels", "source_url"},
            ),
            "rename_merchant": ({"merchant_id", "name"}, set()),
            "merge_merchant": ({"merchant_id", "target_id"}, set()),
            "aliases": ({"merchant_id", "aliases"}, set()),
            "archive_merchant": ({"merchant_id"}, set()),
            "archive_mcc": ({"merchant_id", "mcc"}, {"merchant_ids", "channels"}),
            "rename_brand": ({"brand_id", "name"}, set()),
            "brand_aliases": ({"brand_id", "aliases"}, set()),
            "edit_brand_names": ({"brand_id", "name", "aliases"}, set()),
            "set_brand_membership": ({"merchant_id", "brand_id"}, set()),
            "merge_brand": ({"brand_id", "target_id"}, set()),
            "edit_mcc_note": (
                {"merchant_id", "mcc", "note"},
                {"merchant_ids", "channels"},
            ),
            "revert": ({"audit_id"}, set()),
            "card_partnership": (
                {
                    "brand_id",
                    "card_id",
                    "channel",
                    "mode",
                    "reward_kind",
                    "starts_on",
                    "ends_on",
                    "conditions",
                    "source_url",
                    "tiers",
                    "exclusions",
                },
                set(),
            ),
            "store_metadata": ({"brand_id", "name", "aliases"}, {"source_url"}),
            "mcc_save": (
                {"mcc", "channel"},
                {
                    "brand_id",
                    "name",
                    "aliases",
                    "merchant_id",
                    "merchant_ids",
                    "channels",
                    "old_mcc",
                    "note",
                    "source_url",
                },
            ),
            "mcc_delete": (
                {"merchant_id", "mcc"},
                {"merchant_ids", "channels"},
            ),
            "partner_save": (
                {"card_id", "channel", "value"},
                {
                    "offer_id",
                    "brand_id",
                    "name",
                    "conditions",
                    "starts_on",
                    "ends_on",
                    "min_purchase",
                    "max_purchase",
                    "per_transaction_cap",
                    "excluded_mccs",
                    "source_url",
                    "mode",
                    "reward_kind",
                },
            ),
            "partner_delete": ({"offer_id"}, set()),
        }
        if kind not in schemas:
            raise CommunityError("Неполные данные предложения. Начните заново.")
        required, optional = schemas[kind]
        if not required <= set(payload) or set(payload) - required - optional:
            raise CommunityError("Неполные данные предложения. Начните заново.")
        result = dict(payload)
        if kind == "add_mcc_both" and (("brand_id" in result) == ("name" in result)):
            raise CommunityError("Выберите существующий магазин или задайте название нового.")
        if kind in {"mcc_save", "partner_save"} and (
            ("brand_id" in result) == ("name" in result)
        ):
            raise CommunityError("Выберите существующий магазин или задайте название нового.")
        for key, value in result.items():
            if key in {"merchant_id", "brand_id", "target_id", "audit_id", "offer_id"}:
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise CommunityError("Некорректный магазин или запись истории.")
            elif key == "merchant_ids":
                if (
                    not isinstance(value, list)
                    or not value
                    or len(value) > 100
                    or any(
                        not isinstance(item, int) or isinstance(item, bool) or item <= 0
                        for item in value
                    )
                    or len(set(value)) != len(value)
                    or value[0] != result.get("merchant_id")
                ):
                    raise CommunityError("Некорректная группа MCC.")
            elif key == "channels":
                if (
                    not isinstance(value, list)
                    or not value
                    or len(value) > 2
                    or any(item not in {"offline", "online"} for item in value)
                    or len(set(value)) != len(value)
                    or value != sorted(value, key=("offline", "online").index)
                ):
                    raise CommunityError("Некорректная группа MCC.")
            elif key in {"mcc", "old_mcc"}:
                if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}", value):
                    raise CommunityError("MCC должен состоять из четырёх цифр.")
            elif key == "name":
                result[key] = clean_text(value, maximum=MAX_NAME)
            elif key == "channel" and value not in (
                {"offline", "online", "any"}
                if kind in {"card_partnership", "partner_save"}
                else {"offline", "online", "both"}
                if kind == "mcc_save"
                else {"offline", "online"}
            ):
                raise CommunityError("Выберите допустимый способ оплаты.")
            elif key == "note":
                if (
                    not isinstance(value, str)
                    or len(value.strip()) > MAX_NOTE
                    or any(ord(ch) < 32 for ch in value)
                ):
                    raise CommunityError(f"Подпись к MCC — не больше {MAX_NOTE} символов.")
                result[key] = value.strip()
            elif key == "aliases":
                if not isinstance(value, list) or len(value) > 20:
                    raise CommunityError("Можно сохранить не больше 20 названий.")
                result[key] = [clean_text(alias, maximum=MAX_NAME) for alias in value]
            elif key == "source_url":
                if not isinstance(value, str) or len(value) > 2048:
                    raise CommunityError("Официальная ссылка слишком длинная.")
                value = value.strip()
                if value:
                    parsed = urlparse(value)
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                        raise CommunityError(
                            "Укажите полную официальную ссылку с http:// или https://."
                        )
                result[key] = value
            elif kind == "card_partnership" and key == "card_id":
                result[key] = clean_text(value, maximum=160)
            elif kind == "card_partnership" and key == "mode":
                if value not in {"additional", "total"}:
                    raise CommunityError("Выберите, складывается ли партнёрская выгода.")
            elif kind == "card_partnership" and key == "reward_kind":
                if value not in {"cash", "points"}:
                    raise CommunityError("Выберите денежный возврат или баллы.")
            elif kind == "card_partnership" and key in {"starts_on", "ends_on"}:
                if value is not None:
                    try:
                        date.fromisoformat(value)
                    except (TypeError, ValueError) as exc:
                        raise CommunityError("Дата партнёрства указана неверно.") from exc
            elif kind == "card_partnership" and key == "conditions":
                result[key] = clean_text(value)
            elif kind == "card_partnership" and key == "tiers":
                if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
                    raise CommunityError("Укажите одну величину партнёрской выгоды.")
                tier = value[0]
                if set(tier) != {
                    "value",
                    "min_purchase",
                    "max_purchase",
                    "per_transaction_cap",
                }:
                    raise CommunityError("Величина партнёрской выгоды указана неверно.")
                try:
                    amount = Decimal(str(tier["value"]))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise CommunityError("Введите процент выгоды числом.") from exc
                if not amount.is_finite() or amount <= 0 or amount > 100:
                    raise CommunityError("Процент выгоды должен быть больше 0 и не больше 100.")
                result[key] = [{**tier, "value": str(amount.normalize())}]
            elif kind == "card_partnership" and key == "exclusions":
                if not isinstance(value, list) or len(value) > 50:
                    raise CommunityError("Слишком много исключений партнёрства.")
                cleaned = []
                for exclusion in value:
                    if not isinstance(exclusion, dict) or set(exclusion) != {
                        "brand_id",
                        "card_id",
                        "reward_kind",
                        "channel",
                        "mcc",
                        "starts_on",
                        "ends_on",
                        "reason",
                        "source_url",
                    }:
                        raise CommunityError("Исключение партнёрства указано неверно.")
                    mcc = exclusion.get("mcc")
                    if mcc is not None and (
                        not isinstance(mcc, str) or not re.fullmatch(r"[0-9]{4}", mcc)
                    ):
                        raise CommunityError("MCC в исключении должен состоять из четырёх цифр.")
                    cleaned.append({**exclusion, "reason": clean_text(exclusion["reason"])})
                result[key] = cleaned
            elif kind == "partner_save" and key == "card_id":
                result[key] = clean_text(value, maximum=160)
            elif kind == "partner_save" and key == "value":
                try:
                    amount = Decimal(str(value).replace(",", "."))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise CommunityError("Введите размер партнёрской выгоды числом.") from exc
                if not amount.is_finite() or amount <= 0 or amount > 100:
                    raise CommunityError("Размер выгоды должен быть больше 0 и не больше 100.")
                result[key] = format(amount.normalize(), "f")
            elif kind == "partner_save" and key in {
                "min_purchase",
                "max_purchase",
                "per_transaction_cap",
            }:
                if value is not None:
                    try:
                        amount = Decimal(str(value).replace(",", "."))
                    except (InvalidOperation, TypeError, ValueError) as exc:
                        raise CommunityError("Сумма в партнёрстве указана неверно.") from exc
                    if not amount.is_finite() or amount < 0:
                        raise CommunityError("Сумма в партнёрстве не может быть отрицательной.")
                    result[key] = format(amount.normalize(), "f")
            elif kind == "partner_save" and key in {"starts_on", "ends_on"}:
                if value:
                    try:
                        date.fromisoformat(value)
                    except (TypeError, ValueError) as exc:
                        raise CommunityError("Дата партнёрства указана неверно.") from exc
                result[key] = value or None
            elif kind == "partner_save" and key == "conditions":
                result[key] = "" if not value else clean_text(value)
            elif kind == "partner_save" and key == "excluded_mccs":
                if not isinstance(value, list) or len(value) > 50 or any(
                    not isinstance(item, str) or not re.fullmatch(r"[0-9]{4}", item)
                    for item in value
                ):
                    raise CommunityError("Исключённые MCC должны состоять из четырёх цифр.")
                result[key] = list(dict.fromkeys(value))
        if kind in MCC_KINDS and result["mcc"] not in self.allowed_mccs:
            raise CommunityError(f"MCC {result['mcc']} не найден в справочнике. Проверьте код.")
        if kind in {"mcc_save", "mcc_delete"} and result["mcc"] not in self.allowed_mccs:
            raise CommunityError(f"MCC {result['mcc']} не найден в справочнике. Проверьте код.")
        if kind == "partner_save":
            if self.catalog is None:
                raise CommunityError("Список карт сейчас недоступен.")
            card = next((item for item in self.catalog.cards if item.id == result["card_id"]), None)
            if card is None:
                raise CommunityError("Карта больше недоступна.")
            policy = card.partner_policy
            if result.get("mode", policy.mode) != policy.mode or result.get(
                "reward_kind", policy.reward_kind
            ) != policy.reward_kind:
                raise CommunityError("Правила партнёрства карты изменились. Откройте форму заново.")
            result["mode"] = policy.mode
            result["reward_kind"] = policy.reward_kind
            if (
                result.get("starts_on")
                and result.get("ends_on")
                and result["starts_on"] > result["ends_on"]
            ):
                raise CommunityError("Дата начала партнёрства позже даты окончания.")
            if (
                result.get("min_purchase") is not None
                and result.get("max_purchase") is not None
                and Decimal(result["min_purchase"]) > Decimal(result["max_purchase"])
            ):
                raise CommunityError("Минимальная сумма больше максимальной.")
        _safe_json(result)
        return result

    @staticmethod
    def _fingerprint(kind: str, payload: dict[str, Any]) -> str:
        """Return a stable exact-effect fingerprint; sources/comments are not identity."""

        values = {key: value for key, value in payload.items() if key != "source_url"}
        if "name" in values:
            values["name"] = normalize_store_name(values["name"])
        if "aliases" in values:
            values["aliases"] = sorted(
                {normalize_store_name(value) for value in values["aliases"]}
            )
        if kind == "mcc_save" and "merchant_id" not in values and "name" in values:
            values = {"name": values["name"]}
        encoded = json.dumps(
            {"kind": kind, "payload": values},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _ensure_not_duplicate(
        self,
        conn: sqlite3.Connection,
        kind: str,
        payload: dict[str, Any],
        fingerprint: str,
        *,
        exclude_proposal_id: int | None = None,
    ) -> None:
        row = conn.execute(
            """SELECT id FROM community_proposals WHERE fingerprint=?
            AND status IN ('pending','clarification') AND (? IS NULL OR id<>?) LIMIT 1""",
            (fingerprint, exclude_proposal_id, exclude_proposal_id),
        ).fetchone()
        if row:
            raise CommunityError("Точное такое предложение уже ожидает проверки.")

        if kind in {"mcc_save", "partner_save"} and "name" in payload:
            needle = normalize_store_name(payload["name"])
            if any(
                needle
                in {
                    normalize_store_name(item.name),
                    *(normalize_store_name(alias) for alias in item.aliases),
                }
                for item in self.stores.list_brands(connection=conn)
            ):
                raise CommunityError("Магазин с таким названием уже существует.")
        if kind == "store_metadata":
            needle = normalize_store_name(payload["name"])
            for brand in self.stores.list_brands(connection=conn):
                normalized_aliases = sorted(
                    {normalize_store_name(value) for value in payload.get("aliases", [])}
                )
                current_aliases = sorted(
                    {normalize_store_name(value) for value in brand.aliases}
                )
                if (
                    brand.id == payload["brand_id"]
                    and needle == normalize_store_name(brand.name)
                    and normalized_aliases == current_aliases
                ):
                    raise CommunityError("Такие данные магазина уже подтверждены.")
                if brand.id != payload["brand_id"] and needle in {
                    normalize_store_name(brand.name),
                    *(normalize_store_name(alias) for alias in brand.aliases),
                }:
                    raise CommunityError("Магазин с таким названием уже существует.")
        if kind == "mcc_save" and "merchant_id" not in payload and "brand_id" in payload:
            wanted = (
                {"offline", "online"}
                if payload["channel"] == "both"
                else {payload["channel"]}
            )
            existing = self.stores.list_brand_mcc_groups(
                payload["brand_id"], connection=conn
            )
            if any(
                item.mcc == payload["mcc"] and wanted <= set(item.channels)
                for item in existing
            ):
                raise CommunityError("Такой MCC уже есть у выбранного магазина.")
        if kind == "mcc_save" and "merchant_id" in payload:
            source_brand = self.stores.brand_for_merchant(
                payload["merchant_id"], connection=conn
            )
            groups = (
                self.stores.list_brand_mcc_groups(source_brand.id, connection=conn)
                if source_brand is not None
                else ()
            )
            current = next(
                (
                    item
                    for item in groups
                    if item.mcc == payload.get("old_mcc", payload["mcc"])
                    and payload["merchant_id"] in item.merchant_ids
                ),
                None,
            )
            target_channels = (
                ("offline", "online")
                if payload["channel"] == "both"
                else (payload["channel"],)
            )
            if (
                current is not None
                and source_brand is not None
                and payload["brand_id"] == source_brand.id
                and tuple(current.channels) == target_channels
                and payload["mcc"] == current.mcc
                and payload.get("note", "") == current.note
            ):
                raise CommunityError("Такой MCC уже подтверждён без изменений.")
        if (
            kind == "partner_save"
            and "offer_id" not in payload
            and "brand_id" in payload
            and self.partners is not None
        ):
            for offer in self.partners.list_offers(payload["brand_id"], connection=conn):
                if self._partner_effect(offer, connection=conn) == self._partner_effect_payload(
                    payload
                ):
                    raise CommunityError("Точное такое партнёрство уже существует.")
        if kind == "partner_save" and "offer_id" in payload and self.partners is not None:
            offer = self.partners.get_offer(
                payload["offer_id"], include_archived=True, connection=conn
            )
            if offer and self._partner_effect(
                offer, connection=conn
            ) == self._partner_effect_payload(payload):
                raise CommunityError("Партнёрство уже подтверждено без изменений.")

    @staticmethod
    def _partner_effect_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "brand_id": payload["brand_id"],
            "card_id": payload["card_id"],
            "channel": payload["channel"],
            "mode": payload["mode"],
            "reward_kind": payload["reward_kind"],
            "value": payload["value"],
            "starts_on": payload.get("starts_on"),
            "ends_on": payload.get("ends_on"),
            "conditions": payload.get("conditions", ""),
            "min_purchase": payload.get("min_purchase"),
            "max_purchase": payload.get("max_purchase"),
            "per_transaction_cap": payload.get("per_transaction_cap"),
            "excluded_mccs": sorted(payload.get("excluded_mccs", [])),
        }

    def _partner_effect(self, offer: Any, *, connection: sqlite3.Connection) -> dict[str, Any]:
        tier = offer.tiers[0]
        return {
            "brand_id": offer.brand_id,
            "card_id": offer.card_id,
            "channel": offer.channel,
            "mode": offer.mode,
            "reward_kind": offer.reward_kind,
            "value": format(tier.value.normalize(), "f"),
            "starts_on": offer.starts_on.isoformat() if offer.starts_on else None,
            "ends_on": offer.ends_on.isoformat() if offer.ends_on else None,
            "conditions": offer.conditions,
            "min_purchase": (
                format(tier.min_purchase.normalize(), "f")
                if tier.min_purchase is not None
                else None
            ),
            "max_purchase": (
                format(tier.max_purchase.normalize(), "f")
                if tier.max_purchase is not None
                else None
            ),
            "per_transaction_cap": (
                format(tier.per_transaction_cap.normalize(), "f")
                if tier.per_transaction_cap is not None
                else None
            ),
            "excluded_mccs": sorted(
                item.mcc
                for item in self.partners.list_offer_exclusions(
                    offer.id, connection=connection
                )
                if item.mcc
            ),
        }

    def pending_effects(self) -> tuple[dict[str, Any], ...]:
        """Return active proposal effects without author identity for public overlays."""

        with self.stores.connection() as conn:
            rows = conn.execute(
                """SELECT id,kind,payload,status FROM community_proposals
                WHERE status IN ('pending','clarification') ORDER BY id"""
            ).fetchall()
            return tuple(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload"]),
                    "status": row["status"],
                }
                for row in rows
            )

    def _proposal(self, conn: sqlite3.Connection, proposal_id: int) -> Proposal:
        row = conn.execute(
            "SELECT * FROM community_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        if not row:
            raise CommunityError("Предложение не найдено.")
        return Proposal(
            row["id"],
            row["user_id"],
            row["kind"],
            json.loads(row["payload"]),
            row["status"],
            row["version"],
            row["comment"],
            row["reason"],
            row["reviewer_id"],
            row["lease_until"],
            row["audit_id"],
            row["fingerprint"],
        )

    def proposal(self, user_id: int, proposal_id: int) -> Proposal:
        """Read a proposal as its author or a currently authorized reviewer."""

        with self.stores.connection() as conn:
            proposal = self._proposal(conn, proposal_id)
            if proposal.user_id != user_id:
                self._require_admin(conn, user_id)
            return proposal

    def own_proposals(self, user_id: int, *, offset: int = 0) -> tuple[Proposal, ...]:
        """Return a bounded page of the user's own contribution status history."""

        with self.stores.connection() as conn:
            self._role(conn, user_id)
            rows = conn.execute(
                "SELECT id FROM community_proposals WHERE user_id=? "
                "ORDER BY id DESC LIMIT 10 OFFSET ?",
                (user_id, max(0, offset)),
            )
            return tuple(self._proposal(conn, row[0]) for row in rows)

    def _requeue_expired_clarifications(self, conn: sqlite3.Connection, now: float) -> int:
        rows = conn.execute(
            "SELECT id,user_id FROM community_proposals "
            "WHERE status='clarification' AND updated_at<=?",
            (now - CLARIFICATION_SECONDS,),
        ).fetchall()
        if not rows:
            return 0
        proposal_ids = {row["id"] for row in rows}
        for row in conn.execute("SELECT user_id,data FROM community_drafts").fetchall():
            try:
                response_id = json.loads(row["data"]).get("response_id")
            except (TypeError, json.JSONDecodeError):
                response_id = None
            if response_id in proposal_ids:
                self._discard_draft(conn, row["user_id"])
        marker = "Пользователь не ответил на уточнение."
        for proposal_id in proposal_ids:
            proposal = self._proposal(conn, proposal_id)
            reason = proposal.reason or ""
            if marker not in reason:
                reason = (reason.rstrip() + "\n\n" + marker).strip()
            conn.execute(
                """UPDATE community_proposals SET status='pending',version=version+1,
                   reason=?,reviewer_id=NULL,lease_until=NULL,updated_at=? WHERE id=?""",
                (reason, now, proposal_id),
            )
        return len(proposal_ids)

    def requeue_expired_clarifications(self, *, now: float | None = None) -> int:
        """Return unanswered clarification requests to review after 24 hours."""

        with self.stores.transaction() as conn:
            return self._requeue_expired_clarifications(conn, time.time() if now is None else now)

    @staticmethod
    def _available_queue_count(conn: sqlite3.Connection, now: float) -> int:
        return int(
            conn.execute(
                """SELECT COUNT(*) FROM community_proposals WHERE status='pending'
                   AND (reviewer_id IS NULL OR lease_until IS NULL OR lease_until<=?)""",
                (now,),
            ).fetchone()[0]
        )

    def queue(
        self, user_id: int, *, offset: int = 0, now: float | None = None
    ) -> tuple[Proposal, ...]:
        """Read only currently available review contributions."""

        current = time.time() if now is None else now
        with self.stores.transaction() as conn:
            self._require_admin(conn, user_id)
            self._requeue_expired_clarifications(conn, current)
            rows = conn.execute(
                """SELECT id FROM community_proposals WHERE status='pending'
                   AND (reviewer_id IS NULL OR lease_until IS NULL OR lease_until<=?)
                   ORDER BY id LIMIT 10 OFFSET ?""",
                (current, max(0, offset)),
            )
            return tuple(self._proposal(conn, row[0]) for row in rows)

    def _publish(
        self,
        conn: sqlite3.Connection,
        proposal: Proposal,
        actor_id: int,
        *,
        replace_old: str | None = None,
        expected: dict[str, Any] | None = None,
    ) -> int:
        kind = proposal.kind
        payload = self._validate_payload(kind, proposal.payload)
        if kind in FORM_KINDS:
            self._require_admin(conn, actor_id)
            if expected is not None and self._snapshot(conn, kind, payload) != expected:
                raise StaleAction(
                    "Данные изменились после просмотра. Откройте редактор предложения заново."
                )
        if kind == "store_metadata":
            return self.stores.apply_change(
                "edit_brand_names",
                {
                    "brand_id": payload["brand_id"],
                    "name": payload["name"],
                    "aliases": payload["aliases"],
                },
                actor_id,
                connection=conn,
            ).audit_id
        if kind == "mcc_delete":
            values = {
                key: payload[key]
                for key in ("merchant_id", "merchant_ids", "channels", "mcc")
                if key in payload
            }
            return self.stores.apply_change(
                "archive_mcc", values, actor_id, connection=conn
            ).audit_id
        if kind == "mcc_save":
            values = dict(payload)
            source_url = values.pop("source_url", "")
            values["evidence"] = {"submission_id": proposal.id, "source": "community"}
            if source_url:
                values["evidence"]["source_url"] = source_url
            return self.stores.save_mcc(values, actor_id=actor_id, connection=conn).audit_id
        if kind == "partner_delete":
            if self.partners is None:
                raise CommunityError("Хранилище партнёрств пока недоступно.")
            if not self.partners.delete_offer_with_exclusions(
                payload["offer_id"], actor_id=actor_id, connection=conn
            ):
                raise StaleAction("Партнёрство уже удалено.")
            return payload["offer_id"]
        if kind == "partner_save":
            if self.partners is None:
                raise CommunityError("Хранилище партнёрств пока недоступно.")
            if "name" in payload:
                created = self.stores.create_partner_store(
                    payload["name"], payload["channel"], actor_id=actor_id, connection=conn
                )
                if created.brand_id is None:
                    raise CommunityError("Не удалось создать магазин для партнёрства.")
                brand_id = created.brand_id
            else:
                brand_id = payload["brand_id"]
            from .partner_rewards import (
                PartnerExclusionInput,
                PartnerOfferInput,
                PartnerTierInput,
            )

            starts_on = (
                date.fromisoformat(payload["starts_on"])
                if payload.get("starts_on")
                else None
            )
            ends_on = date.fromisoformat(payload["ends_on"]) if payload.get("ends_on") else None
            source_url = payload.get("source_url", "")
            offer_input = PartnerOfferInput(
                brand_id=brand_id,
                card_id=payload["card_id"],
                channel=payload["channel"],
                mode=payload["mode"],
                reward_kind=payload["reward_kind"],
                starts_on=starts_on,
                ends_on=ends_on,
                conditions=payload.get("conditions", ""),
                source_url=source_url,
                tiers=(
                    PartnerTierInput(
                        value=Decimal(payload["value"]),
                        min_purchase=(
                            Decimal(payload["min_purchase"])
                            if payload.get("min_purchase") is not None
                            else None
                        ),
                        max_purchase=(
                            Decimal(payload["max_purchase"])
                            if payload.get("max_purchase") is not None
                            else None
                        ),
                        per_transaction_cap=(
                            Decimal(payload["per_transaction_cap"])
                            if payload.get("per_transaction_cap") is not None
                            else None
                        ),
                    ),
                ),
                exclusions=tuple(
                    PartnerExclusionInput(
                        brand_id=brand_id,
                        card_id=payload["card_id"],
                        reward_kind=payload["reward_kind"],
                        channel=payload["channel"],
                        mcc=mcc,
                        suppress_base=False,
                        reason="Партнёрство не действует для MCC",
                        source_url=source_url,
                    )
                    for mcc in payload.get("excluded_mccs", [])
                ),
            )
            offer = self.partners.save_offer(
                payload.get("offer_id"), offer_input, actor_id=actor_id, connection=conn
            )
            return offer.id
        if kind == "card_partnership":
            self._require_admin(conn, actor_id)
            if self.partners is None:
                raise CommunityError("Хранилище партнёрств пока недоступно.")
            try:
                from .partner_rewards import (
                    PartnerExclusionInput,
                    PartnerOfferInput,
                    PartnerTierInput,
                )
            except ImportError as exc:  # pragma: no cover - wiring guard for partial installs
                raise CommunityError("Хранилище партнёрств пока недоступно.") from exc
            offer_input = PartnerOfferInput(
                brand_id=payload["brand_id"],
                card_id=payload["card_id"],
                channel=payload["channel"],
                mode=payload["mode"],
                reward_kind=payload["reward_kind"],
                starts_on=(
                    date.fromisoformat(payload["starts_on"]) if payload["starts_on"] else None
                ),
                ends_on=(date.fromisoformat(payload["ends_on"]) if payload["ends_on"] else None),
                conditions=payload["conditions"],
                source_url=payload["source_url"],
                tiers=tuple(
                    PartnerTierInput(
                        value=Decimal(tier["value"]),
                        min_purchase=(
                            Decimal(tier["min_purchase"])
                            if tier["min_purchase"] is not None
                            else None
                        ),
                        max_purchase=(
                            Decimal(tier["max_purchase"])
                            if tier["max_purchase"] is not None
                            else None
                        ),
                        per_transaction_cap=(
                            Decimal(tier["per_transaction_cap"])
                            if tier["per_transaction_cap"] is not None
                            else None
                        ),
                        starts_on=None,
                        ends_on=None,
                    )
                    for tier in payload["tiers"]
                ),
                exclusions=tuple(
                    PartnerExclusionInput(
                        brand_id=exclusion["brand_id"],
                        card_id=exclusion["card_id"],
                        reward_kind=exclusion["reward_kind"],
                        channel=exclusion["channel"],
                        mcc=exclusion["mcc"],
                        starts_on=(
                            date.fromisoformat(exclusion["starts_on"])
                            if exclusion["starts_on"]
                            else None
                        ),
                        ends_on=(
                            date.fromisoformat(exclusion["ends_on"])
                            if exclusion["ends_on"]
                            else None
                        ),
                        reason=exclusion["reason"],
                        source_url=exclusion["source_url"],
                    )
                    for exclusion in payload["exclusions"]
                ),
            )
            offer = self.partners.create_offer(offer_input, actor_id=actor_id, connection=conn)
            identifier = getattr(offer, "id", getattr(offer, "offer_id", None))
            if not isinstance(identifier, int):
                raise CommunityError("Партнёрство сохранено без корректного идентификатора.")
            return identifier
        if (
            kind == "add_merchant"
            and "brand_id" not in payload
            and self.stores.find_exact(payload["name"], payload["channel"], connection=conn)
        ):
            raise CommunityError(
                "Такой магазин уже есть. Отмените предложение и выберите его из поиска."
            )
        if replace_old is not None:
            if kind != "add_mcc" or not re.fullmatch(r"[0-9]{4}", replace_old):
                raise CommunityError("Замена доступна только для MCC существующего магазина.")
            if replace_old == payload["mcc"]:
                raise CommunityError("Старый и новый MCC совпадают.")
            kind, payload = "replace_mcc", {**payload, "old_mcc": replace_old}
        self._authorize_store_change(conn, actor_id, kind, payload)
        if kind in STRUCTURAL_KINDS:
            current = self._snapshot(conn, kind, payload)
            if expected is None or current != expected:
                raise StaleAction(
                    "Данные магазина изменились после просмотра. "
                    "Откройте редактирование или возьмите предложение в разбор заново."
                )
        if kind in MCC_KINDS:
            source_url = payload.pop("source_url", "")
            payload["evidence"] = {"submission_id": proposal.id, "source": "community"}
            if source_url:
                payload["evidence"]["source_url"] = source_url
        return self.stores.apply_change(kind, payload, actor_id, connection=conn).audit_id

    def _authorize_store_change(
        self, conn: sqlite3.Connection, actor_id: int, kind: str, payload: dict[str, Any]
    ) -> None:
        """Recheck ordinary helper/owner authority inside the publication transaction."""

        del kind, payload
        self._require_admin(conn, actor_id)

    def _snapshot(
        self, conn: sqlite3.Connection, kind: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Capture just the entities a structural preview can overwrite."""

        result: dict[str, Any] = {
            "brands": {},
            "merchants": {},
            "facts": {},
            "tannei": {},
            "offers": {},
        }
        brand_id = payload.get("brand_id")
        if (
            kind
            in {
                "rename_brand",
                "brand_aliases",
                "edit_brand_names",
                "merge_brand",
                "set_brand_membership",
                "add_mcc_both",
                "store_metadata",
                "mcc_save",
                "partner_save",
            }
            and brand_id is not None
        ):
            brand_ids = [brand_id]
            if kind == "merge_brand":
                brand_ids.append(payload["target_id"])
            for value in brand_ids:
                brand = self.stores.get_brand(value, connection=conn, include_archived=True)
                if brand is None:
                    raise CommunityError("Магазин больше недоступен. Откройте поиск заново.")
                result["brands"][str(value)] = [brand.revision, brand.archived]
        if kind == "add_mcc_both" and brand_id is not None:
            for merchant in self.stores.list_brand_members(
                brand_id, connection=conn, include_archived=True
            ):
                result["merchants"][str(merchant.id)] = [
                    merchant.revision,
                    merchant.archived,
                    merchant.channel,
                ]
                for fact in self.stores.list_mcc(
                    merchant.id, connection=conn, include_archived=True
                ):
                    result["facts"][f"{merchant.id}:{fact.mcc}"] = [
                        fact.revision,
                        fact.archived,
                        fact.evidence_count,
                        fact.note,
                    ]
            snapshot = self.stores.tannei_snapshot(brand_id, connection=conn)
            result["tannei"][str(brand_id)] = (
                [snapshot["revision"], snapshot["source_count"], snapshot["channels"]]
                if snapshot
                else None
            )
        merchant_id = payload.get("merchant_id")
        if merchant_id and kind in STRUCTURAL_KINDS | {"add_mcc", "mcc_save", "mcc_delete"}:
            ids = list(payload.get("merchant_ids", [merchant_id]))
            if kind == "merge_merchant":
                ids.append(payload["target_id"])
            for value in ids:
                merchant = self.stores.get(value, connection=conn, include_archived=True)
                if merchant is None:
                    raise CommunityError("Магазин больше недоступен. Откройте поиск заново.")
                result["merchants"][str(value)] = [merchant.revision, merchant.archived]
            if kind in {
                "replace_mcc",
                "archive_mcc",
                "add_mcc",
                "edit_mcc_note",
                "mcc_save",
                "mcc_delete",
            }:
                old_mcc = payload.get("old_mcc", payload.get("mcc"))
                for value in ids:
                    facts = {
                        fact.mcc: fact
                        for fact in self.stores.list_mcc(
                            value, connection=conn, include_archived=True
                        )
                    }
                    codes = facts if kind == "add_mcc" else [old_mcc]
                    for code in codes:
                        fact = facts.get(code)
                        result["facts"][f"{value}:{code}"] = (
                            [
                                fact.revision,
                                fact.archived,
                                fact.evidence_count,
                                getattr(fact, "note", ""),
                            ]
                            if fact
                            else None
                        )
        offer_id = payload.get("offer_id")
        if offer_id is not None and kind in {"partner_save", "partner_delete"}:
            if self.partners is None:
                raise CommunityError("Хранилище партнёрств пока недоступно.")
            offer = self.partners.get_offer(offer_id, include_archived=True, connection=conn)
            if offer is None:
                raise CommunityError("Партнёрство больше недоступно.")
            result["offers"][str(offer_id)] = {
                "brand_id": offer.brand_id,
                "card_id": offer.card_id,
                "channel": offer.channel,
                "mode": offer.mode,
                "reward_kind": offer.reward_kind,
                "starts_on": offer.starts_on.isoformat() if offer.starts_on else None,
                "ends_on": offer.ends_on.isoformat() if offer.ends_on else None,
                "conditions": offer.conditions,
                "source_url": offer.source_url,
                "archived": offer.archived,
                "tiers": [
                    [
                        str(tier.value),
                        str(tier.min_purchase) if tier.min_purchase is not None else None,
                        str(tier.max_purchase) if tier.max_purchase is not None else None,
                        str(tier.per_transaction_cap)
                        if tier.per_transaction_cap is not None
                        else None,
                    ]
                    for tier in offer.tiers
                ],
                "exclusions": [
                    item.mcc
                    for item in self.partners.list_offer_exclusions(
                        offer_id, connection=conn
                    )
                ],
            }
        return result

    def submit(self, user_id: int, draft_id: str, version: int) -> Proposal:
        """Submit user evidence or atomically publish a trusted preview once."""

        with self.stores.transaction() as conn:
            draft = self._draft(conn, user_id, draft_id, version)
            if draft.stage != "preview":
                raise StaleAction("Сначала проверьте предложение.")
            role = self._role(conn, user_id)[0]
            kind = draft.data.get("kind", "")
            if kind not in (EDIT_KINDS if role != "user" else PUBLIC_KINDS):
                raise AccessDenied("Это изменение доступно только помощникам.")
            payload = self._validate_payload(kind, draft.data.get("payload", {}))
            if kind == "partner_save" and "name" in payload and role == "user":
                raise AccessDenied("Новый магазин для партнёрства может создать только помощник.")
            if (
                role == "user"
                and kind in {"card_partnership", "partner_save"}
                and "brand_id" in payload
                and not self.stores.list_brand_mcc_groups(
                    payload["brand_id"], connection=conn
                )
            ):
                raise AccessDenied(
                    "Партнёрство можно предложить только для магазина с подтверждённым MCC."
                )
            response_id = draft.data.get("response_id")
            fingerprint = self._fingerprint(kind, payload)
            self._ensure_not_duplicate(
                conn, kind, payload, fingerprint, exclude_proposal_id=response_id
            )
            comment = draft.data.get("comment", "")
            if comment:
                comment = clean_text(comment)
            media = conn.execute(
                "SELECT 1 FROM community_media WHERE draft_id=?", (draft.id,)
            ).fetchone()
            original = self._proposal(conn, response_id) if response_id else None
            if original and (
                original.user_id != user_id
                or original.status != "clarification"
                or original.version != draft.data.get("response_version")
            ):
                raise StaleAction("Предложение уже изменилось.")
            now = time.time()
            if original:
                proposal_id = original.id
                conn.execute(
                    """UPDATE community_proposals SET status='pending',version=version+1,
                       comment=?,updated_at=?,reviewer_id=NULL,lease_until=NULL WHERE id=?""",
                    (comment, now, proposal_id),
                )
                if media:
                    conn.execute("DELETE FROM community_media WHERE proposal_id=?", (proposal_id,))
            else:
                cursor = conn.execute(
                    """INSERT INTO community_proposals(draft_id,user_id,kind,payload,comment,
                       status,version,created_at,updated_at,fingerprint)
                       VALUES(?,?,?,?,?,'pending',1,?,?,?)""",
                    (
                        draft.id,
                        user_id,
                        kind,
                        _safe_json(payload),
                        comment,
                        now,
                        now,
                        fingerprint,
                    ),
                )
                proposal_id = cursor.lastrowid
            conn.execute(
                "UPDATE community_media SET proposal_id=?,draft_id=NULL WHERE draft_id=?",
                (proposal_id, draft.id),
            )
            proposal = self._proposal(conn, proposal_id)
            # A response to clarification always returns to review, even if its
            # author received a role while their original proposal was pending.
            if role != "user" and not original:
                audit_id = self._publish(
                    conn, proposal, user_id, expected=draft.data.get("expected")
                )
                conn.execute(
                    """UPDATE community_proposals SET status='approved',version=version+1,
                       reviewer_id=?,audit_id=?,finalized_at=? WHERE id=?""",
                    (user_id, audit_id, now, proposal_id),
                )
                self._expire_proposal_media(conn, proposal_id, now)
            conn.execute("DELETE FROM community_editor_messages WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM community_drafts WHERE user_id=?", (user_id,))
            return self._proposal(conn, proposal_id)

    def respond(self, user_id: int, proposal_id: int, version: int) -> Draft:
        """Create a private clarification draft tied to the current proposal version."""

        with self.stores.transaction() as conn:
            proposal = self._proposal(conn, proposal_id)
            if (
                proposal.user_id != user_id
                or proposal.status != "clarification"
                or proposal.version != version
            ):
                if proposal.user_id == user_id and proposal.status == "pending":
                    raise StaleAction("Заявка уже вернулась на проверку.")
                raise StaleAction("Ответ уже получен или предложение закрыто.")
            row = conn.execute(
                "SELECT 1 FROM community_drafts WHERE user_id=?", (user_id,)
            ).fetchone()
            if row:
                current = self._draft(conn, user_id)
                if (
                    current.data.get("response_id") == proposal_id
                    and current.data.get("response_version") == version
                ):
                    return current
                raise StaleAction(
                    "У вас есть незавершённое действие. Продолжите или отмените его через /start."
                )
            _, epoch = self._role(conn, user_id)
            draft_id = uuid.uuid4().hex[:12]
            data = {
                "kind": proposal.kind,
                "payload": proposal.payload,
                "response_id": proposal.id,
                "response_version": version,
                "draft_mode": True,
            }
            conn.execute(
                """INSERT INTO community_drafts VALUES(?,?,1,'response',?,?,?,?)""",
                (user_id, draft_id, _safe_json(data), 0, epoch, time.time()),
            )
            return self._draft(conn, user_id)

    def claim(
        self, actor_id: int, proposal_id: int, version: int, *, now: float | None = None
    ) -> Proposal:
        """Acquire one fixed 15-minute review lease with atomic role/version checks."""

        now = time.time() if now is None else now
        with self.stores.transaction() as conn:
            self._require_admin(conn, actor_id)
            proposal = self._proposal(conn, proposal_id)
            if proposal.version != version or proposal.status != "pending":
                raise StaleAction("Предложение уже изменилось. Обновите очередь.")
            if proposal.reviewer_id is not None and (proposal.lease_until or 0) > now:
                if proposal.reviewer_id != actor_id:
                    raise StaleAction("Заявка уже в работе у другого помощника.")
                raise StaleAction("Заявка уже зарезервирована вами. Продолжите текущий разбор.")
            conn.execute(
                """UPDATE community_proposals SET reviewer_id=?,lease_until=?,version=version+1
                   WHERE id=?""",
                (actor_id, now + LEASE_SECONDS, proposal_id),
            )
            conn.execute(
                "INSERT INTO community_review_snapshots VALUES(?,?,?) "
                "ON CONFLICT(proposal_id) DO UPDATE "
                "SET version=excluded.version,state=excluded.state",
                (
                    proposal_id,
                    version + 1,
                    _safe_json(self._snapshot(conn, proposal.kind, proposal.payload)),
                ),
            )
            return self._proposal(conn, proposal_id)

    def _validate_review(
        self, conn: sqlite3.Connection, actor_id: int, proposal_id: int, version: int, now: float
    ) -> Proposal:
        self._require_admin(conn, actor_id)
        proposal = self._proposal(conn, proposal_id)
        if (
            proposal.status != "pending"
            or proposal.version != version
            or proposal.reviewer_id != actor_id
            or (proposal.lease_until or 0) <= now
        ):
            raise StaleAction(
                "Срок разбора истёк или предложение уже изменилось. Откройте очередь."
            )
        return proposal

    def validate_review(
        self, actor_id: int, proposal_id: int, version: int, *, now: float | None = None
    ) -> Proposal:
        """Validate a live owned review lease without changing its deadline or version."""

        with self.stores.connection() as conn:
            return self._validate_review(
                conn, actor_id, proposal_id, version, time.time() if now is None else now
            )

    def _release_review(
        self,
        conn: sqlite3.Connection,
        actor_id: int,
        proposal_id: int,
        version: int,
        now: float,
    ) -> Proposal | None:
        self._require_admin(conn, actor_id)
        changed = conn.execute(
            """UPDATE community_proposals SET reviewer_id=NULL,lease_until=NULL,
               version=version+1,updated_at=?
               WHERE id=? AND status='pending' AND version=? AND reviewer_id=?""",
            (now, proposal_id, version, actor_id),
        ).rowcount
        return self._proposal(conn, proposal_id) if changed else None

    def release_review(
        self, actor_id: int, proposal_id: int, version: int, *, now: float | None = None
    ) -> Proposal:
        """Release one's unchanged pending claim, including after its lease expires."""

        with self.stores.transaction() as conn:
            proposal = self._release_review(
                conn, actor_id, proposal_id, version, time.time() if now is None else now
            )
            if proposal is None:
                raise StaleAction("Разбор уже изменился. Откройте очередь.")
            return proposal

    def cancel_review_draft(
        self, actor_id: int, draft_id: str, draft_version: int
    ) -> Proposal | None:
        """Discard a moderator draft and release only its unchanged pending claim."""

        with self.stores.transaction() as conn:
            draft = self._draft(conn, actor_id, draft_id, draft_version)
            proposal_id = draft.data.get("proposal_id")
            proposal_version = draft.data.get("proposal_version")
            if not isinstance(proposal_id, int) or not isinstance(proposal_version, int):
                raise StaleAction("Разбор уже недоступен.")
            proposal = self._release_review(
                conn, actor_id, proposal_id, proposal_version, time.time()
            )
            self._discard_draft(conn, actor_id)
            return proposal

    def review(
        self,
        actor_id: int,
        proposal_id: int,
        version: int,
        decision: str,
        *,
        reason: str = "",
        replace_old: str | None = None,
        now: float | None = None,
    ) -> Proposal:
        """Commit one decision and its merchant audit together under a live lease."""

        now = time.time() if now is None else now
        if decision not in {"approved", "rejected", "clarification"}:
            raise CommunityError("Неизвестное решение.")
        if decision == "clarification":
            reason = clean_text(reason)
        elif decision == "rejected":
            reason = clean_text(reason) if reason else ""
        with self.stores.transaction() as conn:
            proposal = self._validate_review(conn, actor_id, proposal_id, version, now)
            snapshot = conn.execute(
                "SELECT state FROM community_review_snapshots WHERE proposal_id=? AND version=?",
                (proposal_id, version),
            ).fetchone()
            audit_id = (
                self._publish(
                    conn,
                    proposal,
                    actor_id,
                    replace_old=replace_old,
                    expected=json.loads(snapshot[0]) if snapshot else None,
                )
                if decision == "approved"
                else None
            )
            conn.execute(
                """UPDATE community_proposals SET status=?,version=version+1,reason=?,audit_id=?,
                   lease_until=NULL,updated_at=?,finalized_at=? WHERE id=?""",
                (
                    decision,
                    reason or None,
                    audit_id,
                    now,
                    now if decision in FINAL_STATES else None,
                    proposal_id,
                ),
            )
            if decision in FINAL_STATES:
                self._expire_proposal_media(conn, proposal_id, now)
            return self._proposal(conn, proposal_id)

    def edit_review_payload(
        self,
        actor_id: int,
        proposal_id: int,
        version: int,
        payload: dict[str, Any],
        *,
        now: float | None = None,
    ) -> Proposal:
        """Replace a claimed proposal payload without extending its fixed lease."""

        current = time.time() if now is None else now
        with self.stores.transaction() as conn:
            proposal = self._validate_review(conn, actor_id, proposal_id, version, current)
            validated = self._validate_payload(proposal.kind, payload)
            fingerprint = self._fingerprint(proposal.kind, validated)
            self._ensure_not_duplicate(
                conn,
                proposal.kind,
                validated,
                fingerprint,
                exclude_proposal_id=proposal_id,
            )
            next_version = version + 1
            conn.execute(
                """UPDATE community_proposals SET payload=?,fingerprint=?,version=?,updated_at=?
                WHERE id=?""",
                (_safe_json(validated), fingerprint, next_version, current, proposal_id),
            )
            conn.execute(
                """INSERT INTO community_review_snapshots(proposal_id,version,state)
                VALUES(?,?,?) ON CONFLICT(proposal_id) DO UPDATE SET
                version=excluded.version,state=excluded.state""",
                (
                    proposal_id,
                    next_version,
                    _safe_json(self._snapshot(conn, proposal.kind, validated)),
                ),
            )
            return self._proposal(conn, proposal_id)

    def cancel(self, user_id: int, proposal_id: int, version: int) -> Proposal:
        """Cancel one's own unfinalized proposal, invalidating an active reviewer lease."""

        with self.stores.transaction() as conn:
            proposal = self._proposal(conn, proposal_id)
            if proposal.user_id != user_id:
                raise AccessDenied("Можно отменить только своё предложение.")
            if proposal.status in FINAL_STATES or proposal.version != version:
                raise StaleAction("Предложение уже изменилось.")
            now = time.time()
            conn.execute(
                """UPDATE community_proposals SET status='cancelled',version=version+1,
                   lease_until=NULL,finalized_at=?,updated_at=? WHERE id=?""",
                (now, now, proposal_id),
            )
            self._expire_proposal_media(conn, proposal_id, now)
            return self._proposal(conn, proposal_id)

    def _expire_proposal_media(
        self, conn: sqlite3.Connection, proposal_id: int, now: float
    ) -> None:
        conn.execute(
            "UPDATE community_media SET expires_at=? WHERE proposal_id=?",
            (now + MEDIA_RETENTION_SECONDS, proposal_id),
        )

    def media_for(
        self,
        actor_id: int,
        proposal_id: int,
        *,
        review_version: int | None = None,
        now: float | None = None,
    ) -> str | None:
        """Read a nonexpired screenshot reference only as its author or current reviewer."""

        self.expire_media()
        with self.stores.transaction() as conn:
            proposal = self._proposal(conn, proposal_id)
            if proposal.user_id != actor_id:
                self._require_admin(conn, actor_id)
            if review_version is not None:
                self._validate_review(
                    conn,
                    actor_id,
                    proposal_id,
                    review_version,
                    time.time() if now is None else now,
                )
            row = conn.execute(
                "SELECT file_id FROM community_media WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            return row[0] if row else None

    def proposal_has_media(self, actor_id: int, proposal_id: int) -> bool:
        """Report screenshot presence after applying proposal visibility authorization."""

        self.expire_media()
        with self.stores.connection() as conn:
            proposal = self._proposal(conn, proposal_id)
            if proposal.user_id != actor_id:
                self._require_admin(conn, actor_id)
            return bool(
                conn.execute(
                    "SELECT 1 FROM community_media WHERE proposal_id=? AND expires_at IS NULL",
                    (proposal_id,),
                ).fetchone()
            )

    def expire_media(self, *, now: float | None = None) -> int:
        """Delete expired references while preserving proposals, facts and audit history."""

        now = time.time() if now is None else now
        with self.stores.transaction() as conn:
            count = conn.execute("DELETE FROM community_media WHERE expires_at<=?", (now,)).rowcount
            conn.execute("DELETE FROM community_updates WHERE created_at<?", (now - 30 * 86400,))
            return count

    def digest_candidates(self) -> tuple[int, ...]:
        """Return explicitly subscribed current reviewers, with no subscriber inference."""

        with self.stores.connection() as conn:
            return tuple(
                row[0]
                for row in conn.execute(
                    "SELECT user_id FROM community_roles WHERE digest=1 ORDER BY user_id"
                )
                if self._role(conn, row[0])[0] != "user"
            )

    def reserve_digest(self, user_id: int, day: str) -> int | None:
        """Persist intent before sending; never retry an existing day/recipient entry."""

        with self.stores.transaction() as conn:
            if self._role(conn, user_id)[0] == "user":
                return None
            row = conn.execute(
                "SELECT digest FROM community_roles WHERE user_id=?", (user_id,)
            ).fetchone()
            now = time.time()
            self._requeue_expired_clarifications(conn, now)
            count = self._available_queue_count(conn, now)
            if not row or not row[0] or not count:
                return None
            cursor = conn.execute(
                "INSERT OR IGNORE INTO community_digests VALUES(?,?,'intent',?)",
                (day, user_id, time.time()),
            )
            return count if cursor.rowcount else None

    def refresh_digest_count(self, user_id: int, day: str) -> int | None:
        """Recheck consent and the currently available queue immediately before delivery."""

        with self.stores.transaction() as conn:
            state = conn.execute(
                "SELECT state FROM community_digests WHERE day=? AND user_id=?",
                (day, user_id),
            ).fetchone()
            role = self._role(conn, user_id)[0]
            consent = conn.execute(
                "SELECT digest FROM community_roles WHERE user_id=?", (user_id,)
            ).fetchone()
            if not state or state[0] != "intent" or role == "user" or not consent or not consent[0]:
                return None
            now = time.time()
            self._requeue_expired_clarifications(conn, now)
            return self._available_queue_count(conn, now) or None

    def finish_digest(self, user_id: int, day: str, state: str) -> None:
        """Record delivery outcome without scheduling retries for uncertain sends."""

        if state not in {"sent", "uncertain", "skipped"}:
            raise ValueError("Invalid digest outcome")
        with self.stores.transaction() as conn:
            conn.execute(
                "UPDATE community_digests SET state=? WHERE day=? AND user_id=? AND state='intent'",
                (state, day, user_id),
            )
