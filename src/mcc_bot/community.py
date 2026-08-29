"""Durable private contributions, role authorization and atomic moderation.

Media references live only in ``community_media``; merchant audit payloads never
contain Telegram file identifiers. All authorization and fact-changing decisions
share the merchant repository's immediate SQLite transaction.
"""

# Russian user-facing copy deliberately contains Cyrillic look-alike letters.
# ruff: noqa: RUF001

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .stores import StoreRepository

LEASE_SECONDS = 15 * 60
MEDIA_RETENTION_SECONDS = 5 * 86400
MAX_COMMENT = 1000
MAX_NAME = 160
MAX_NOTE = 48
MAX_MEDIA_BYTES = 10 * 1024 * 1024
FINAL_STATES = frozenset({"approved", "rejected", "cancelled"})
MCC_KINDS = frozenset({"add_merchant", "add_mcc", "add_mcc_both", "replace_mcc"})
PUBLIC_KINDS = MCC_KINDS | {"rename_merchant", "merge_merchant"}
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

    def __init__(self, stores: StoreRepository, owner_id: int | None = None) -> None:
        self.stores = stores
        if owner_id is not None and (isinstance(owner_id, bool) or owner_id <= 0):
            raise ValueError("Owner must be an explicit positive Telegram user ID")
        self.owner_id = owner_id

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
                """CREATE TABLE IF NOT EXISTS community_proposals (
                    id INTEGER PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    comment TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
                    reviewer_id INTEGER, lease_until REAL, reason TEXT,
                    audit_id INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    finalized_at REAL)""",
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
        """Refresh the last seen identity only for the owner, helpers, or applicants."""

        with self.stores.transaction() as conn:
            role = self._role(conn, user_id)[0]
            request = conn.execute(
                "SELECT status FROM community_role_requests WHERE user_id=?", (user_id,)
            ).fetchone()
            if role not in {"owner", "admin"} and not (request and request["status"] == "pending"):
                return False
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
        conn.execute("DELETE FROM community_drafts WHERE user_id=?", (user_id,))

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
                self._touch_review(
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
            self._draft(conn, user_id, draft_id, version)
            self._discard_draft(conn, user_id)

    def _validate_payload(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        schemas = {
            "add_merchant": ({"name", "channel", "mcc"}, {"brand_id", "note"}),
            "add_mcc": ({"merchant_id", "mcc"}, {"note"}),
            "add_mcc_both": ({"mcc"}, {"brand_id", "name", "note"}),
            "replace_mcc": (
                {"merchant_id", "old_mcc", "mcc"},
                {"note", "merchant_ids"},
            ),
            "rename_merchant": ({"merchant_id", "name"}, set()),
            "merge_merchant": ({"merchant_id", "target_id"}, set()),
            "aliases": ({"merchant_id", "aliases"}, set()),
            "archive_merchant": ({"merchant_id"}, set()),
            "archive_mcc": ({"merchant_id", "mcc"}, {"merchant_ids"}),
            "rename_brand": ({"brand_id", "name"}, set()),
            "brand_aliases": ({"brand_id", "aliases"}, set()),
            "edit_brand_names": ({"brand_id", "name", "aliases"}, set()),
            "set_brand_membership": ({"merchant_id", "brand_id"}, set()),
            "merge_brand": ({"brand_id", "target_id"}, set()),
            "edit_mcc_note": ({"merchant_id", "mcc", "note"}, {"merchant_ids"}),
            "revert": ({"audit_id"}, set()),
        }
        if kind not in schemas:
            raise CommunityError("Неполные данные предложения. Начните заново.")
        required, optional = schemas[kind]
        if not required <= set(payload) or set(payload) - required - optional:
            raise CommunityError("Неполные данные предложения. Начните заново.")
        result = dict(payload)
        if kind == "add_mcc_both" and (("brand_id" in result) == ("name" in result)):
            raise CommunityError("Выберите существующий бренд или задайте название нового.")
        for key, value in result.items():
            if key in {"merchant_id", "brand_id", "target_id", "audit_id"}:
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise CommunityError("Некорректный бренд, магазин или запись истории.")
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
            elif key in {"mcc", "old_mcc"}:
                if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}", value):
                    raise CommunityError("MCC должен состоять из четырёх цифр.")
            elif key == "name":
                result[key] = clean_text(value, maximum=MAX_NAME)
            elif key == "channel" and value not in {"offline", "online"}:
                raise CommunityError("Выберите офлайн или онлайн.")
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
        _safe_json(result)
        return result

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

    def queue(self, user_id: int, *, offset: int = 0) -> tuple[Proposal, ...]:
        """Read pending contributions regardless of digest subscription."""

        with self.stores.connection() as conn:
            self._require_admin(conn, user_id)
            rows = conn.execute(
                "SELECT id FROM community_proposals WHERE status='pending' "
                "ORDER BY id LIMIT 10 OFFSET ?",
                (max(0, offset),),
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
        kind, payload = proposal.kind, dict(proposal.payload)
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
            payload["evidence"] = {"submission_id": proposal.id, "source": "community"}
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
            }
            and brand_id is not None
        ):
            brand_ids = [brand_id]
            if kind == "merge_brand":
                brand_ids.append(payload["target_id"])
            for value in brand_ids:
                brand = self.stores.get_brand(value, connection=conn, include_archived=True)
                if brand is None:
                    raise CommunityError("Бренд больше недоступен. Откройте поиск заново.")
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
        if merchant_id and kind in STRUCTURAL_KINDS | {"add_mcc"}:
            ids = list(payload.get("merchant_ids", [merchant_id]))
            if kind == "merge_merchant":
                ids.append(payload["target_id"])
            for value in ids:
                merchant = self.stores.get(value, connection=conn, include_archived=True)
                if merchant is None:
                    raise CommunityError("Магазин больше недоступен. Откройте поиск заново.")
                result["merchants"][str(value)] = [merchant.revision, merchant.archived]
            if kind in {"replace_mcc", "archive_mcc", "add_mcc", "edit_mcc_note"}:
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
            comment = draft.data.get("comment", "")
            if comment:
                comment = clean_text(comment)
            response_id = draft.data.get("response_id")
            if not response_id and role == "user":
                active_count = conn.execute(
                    "SELECT COUNT(*) FROM community_proposals WHERE user_id=? "
                    "AND status IN ('pending','clarification')",
                    (user_id,),
                ).fetchone()[0]
                if active_count >= 20:
                    raise CommunityError(
                        "У вас уже 20 незавершённых предложений. Дождитесь проверки."
                    )
            original = self._proposal(conn, response_id) if response_id else None
            if original and (
                original.user_id != user_id
                or original.status != "clarification"
                or original.version != draft.data.get("response_version")
            ):
                raise StaleAction("Предложение уже изменилось.")
            media = conn.execute(
                "SELECT 1 FROM community_media WHERE draft_id=?", (draft.id,)
            ).fetchone()
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
                       status,version,created_at,updated_at) VALUES(?,?,?,?,?,'pending',1,?,?)""",
                    (draft.id, user_id, kind, _safe_json(payload), comment, now, now),
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
            conn.execute("DELETE FROM community_drafts WHERE user_id=?", (user_id,))
            return self._proposal(conn, proposal_id)

    def respond(self, user_id: int, proposal_id: int, version: int) -> Draft:
        """Create a private clarification draft tied to the current proposal version."""

        proposal = self.proposal(user_id, proposal_id)
        if (
            proposal.user_id != user_id
            or proposal.status != "clarification"
            or proposal.version != version
        ):
            raise StaleAction("Ответ уже получен или предложение закрыто.")
        current = self.draft(user_id)
        if current is not None:
            if (
                current.data.get("response_id") == proposal_id
                and current.data.get("response_version") == version
            ):
                return current
            raise StaleAction(
                "У вас есть незавершённое действие. Продолжите или отмените его через /start."
            )
        return self.begin(
            user_id,
            stage="response",
            data={
                "kind": proposal.kind,
                "payload": proposal.payload,
                "response_id": proposal.id,
                "response_version": version,
            },
        )

    def claim(
        self, actor_id: int, proposal_id: int, version: int, *, now: float | None = None
    ) -> Proposal:
        """Acquire or renew a 15-minute review lease with atomic role/version checks."""

        now = time.time() if now is None else now
        with self.stores.transaction() as conn:
            self._require_admin(conn, actor_id)
            proposal = self._proposal(conn, proposal_id)
            if proposal.version != version or proposal.status != "pending":
                raise StaleAction("Предложение уже изменилось. Обновите очередь.")
            if proposal.reviewer_id not in {None, actor_id} and (proposal.lease_until or 0) > now:
                raise StaleAction("Предложение уже разбирает другой помощник.")
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

    def _touch_review(
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
        conn.execute(
            "UPDATE community_proposals SET lease_until=? WHERE id=?",
            (now + LEASE_SECONDS, proposal_id),
        )
        return self._proposal(conn, proposal_id)

    def touch_review(
        self, actor_id: int, proposal_id: int, version: int, *, now: float | None = None
    ) -> Proposal:
        """Extend only a live owned lease without refreshing its version or edit snapshot."""

        with self.stores.transaction() as conn:
            return self._touch_review(
                conn, actor_id, proposal_id, version, time.time() if now is None else now
            )

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
        if decision != "approved":
            reason = clean_text(reason)
        with self.stores.transaction() as conn:
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
                self._touch_review(
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
            count = conn.execute(
                "SELECT COUNT(*) FROM community_proposals WHERE status='pending'"
            ).fetchone()[0]
            if not row or not row[0] or not count:
                return None
            cursor = conn.execute(
                "INSERT OR IGNORE INTO community_digests VALUES(?,?,'intent',?)",
                (day, user_id, time.time()),
            )
            return count if cursor.rowcount else None

    def finish_digest(self, user_id: int, day: str, state: str) -> None:
        """Record delivery outcome without scheduling retries for uncertain sends."""

        if state not in {"sent", "uncertain", "skipped"}:
            raise ValueError("Invalid digest outcome")
        with self.stores.transaction() as conn:
            conn.execute(
                "UPDATE community_digests SET state=? WHERE day=? AND user_id=? AND state='intent'",
                (state, day, user_id),
            )
