"""Persistent Telegram chat registry and durable broadcast audit trail."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import Bot
from telegram.error import BadRequest, ChatMigrated, Forbidden, RetryAfter, TelegramError

LOGGER = logging.getLogger(__name__)
_MISSING = object()
_TOKEN_PATTERN = re.compile(r"bot\d+:[A-Za-z0-9_-]+", re.IGNORECASE)
_TELEGRAM_ID_CONTEXT_PATTERN = re.compile(
    r"(?P<label>\b(?:(?:new|old|source|target)[\s_-]+)?"
    r"(?:chat|user)[\s_-]+(?:id|identifier)\s*[:=]?\s*)-?\d+",
    re.IGNORECASE,
)
_SUPERGROUP_ID_PATTERN = re.compile(r"(?<!\d)-100\d{6,}(?!\d)")
_PROFILE_LOOKUP_DELAY_SECONDS = 0.05
_USER_BLOCK_ERROR_PATTERN = re.compile(
    r"(?:forbidden\s*:\s*)?bot\s+was\s+blocked\s+by\s+(?:the\s+)?user[.!]?",
    re.IGNORECASE,
)


class BroadcastSafetyError(RuntimeError):
    """A release cannot safely proceed without operator reconciliation."""


class StaleAccessState(ValueError):
    """A moderation confirmation no longer matches the current access revision."""


@dataclass(frozen=True, slots=True)
class BroadcastResult:
    """Delivery counters for one broadcast attempt."""

    attempted: int
    sent: int
    failed: int


@dataclass(frozen=True, slots=True)
class ChatRecipient:
    """A remembered chat and the latest available operator-facing identity."""

    chat_id: int
    username: str | None
    first_name: str | None
    last_name: str | None

    @property
    def has_identity(self) -> bool:
        """Return whether the recipient has a username or display name."""

        return bool(self.username or self.first_name or self.last_name)


@dataclass(frozen=True, slots=True)
class BroadcastFailure:
    """One failed delivery with its identity snapshot and exact safe reason."""

    chat_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    error_type: str
    error_text: str
    failed_at: str


@dataclass(frozen=True, slots=True)
class BroadcastRun:
    """Durable counters and failures for one broadcast run."""

    run_id: int
    started_at: str
    completed_at: str | None
    recipient_count: int
    attempted: int
    sent: int
    failed: int
    status: str
    message_sha256: str
    failures: tuple[BroadcastFailure, ...]


@dataclass(frozen=True, slots=True)
class BlockedChat:
    """A private chat blocked after Telegram rejected delivery."""

    chat_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    appeal_state: str
    appeal_text: str | None
    permanent: bool


@dataclass(frozen=True, slots=True)
class ChatAccessState:
    """Current access state for one registered private chat, including its generation."""

    chat_id: int
    active: bool
    permanent: bool
    appeal_state: str
    revision: int


def _identity_text(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > maximum or any(ord(character) < 32 for character in value):
        return None
    return value


def _username(value: Any) -> str | None:
    value = _identity_text(value, maximum=32)
    if value is None:
        return None
    value = value.removeprefix("@")
    return value if re.fullmatch(r"[A-Za-z0-9_]{1,32}", value) else None


def redact_telegram_ids(value: str, *known_ids: int) -> str:
    """Redact known or context-labelled Telegram IDs from operator-facing text."""

    for telegram_id in known_ids:
        if isinstance(telegram_id, int) and not isinstance(telegram_id, bool):
            value = re.sub(
                rf"(?<!\d){re.escape(str(telegram_id))}(?!\d)",
                "[telegram-id]",
                value,
            )
    value = _TELEGRAM_ID_CONTEXT_PATTERN.sub(
        lambda match: f"{match.group('label')}[telegram-id]",
        value,
    )
    return _SUPERGROUP_ID_PATTERN.sub("[telegram-id]", value)


def _safe_error_text(error: TelegramError, bot: Bot, chat_id: int) -> str:
    """Preserve Telegram's reason while redacting tokens and Telegram IDs."""

    value = str(error)
    token = getattr(bot, "token", None)
    if isinstance(token, str) and token:
        value = value.replace(token, "[REDACTED]")
    value = _TOKEN_PATTERN.sub("bot[REDACTED]", value)
    related_ids = [chat_id]
    for attribute in ("new_chat_id", "chat_id", "user_id"):
        candidate = getattr(error, attribute, None)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            related_ids.append(candidate)
    return redact_telegram_ids(value, *related_ids)


def is_user_block_error(error_or_text: object) -> bool:
    """Recognize confirmed private user-block rejections, not other delivery failures.

    Text is accepted for persisted Forbidden audit records. Exception objects must
    actually be Forbidden: a network failure containing similar text is not proof.
    """

    if not isinstance(error_or_text, (Forbidden, str)):
        return False
    value = str(error_or_text).strip()
    return _USER_BLOCK_ERROR_PATTERN.fullmatch(value) is not None


def _is_private_chat_id(chat_id: object) -> bool:
    return type(chat_id) is int and 0 < chat_id < 2**63


def _ban_chat(connection: sqlite3.Connection, chat_id: int) -> None:
    """Permanently block access while preserving the existing block and appeal audit."""

    connection.execute("INSERT OR IGNORE INTO telegram_chats(chat_id) VALUES(?)", (chat_id,))
    connection.execute(
        """INSERT INTO blocked_chats(chat_id,active,blocked_at,permanent,appeal_state,revision)
           VALUES(?,1,CURRENT_TIMESTAMP,1,'permanent',1) ON CONFLICT(chat_id) DO UPDATE SET
           active=1,permanent=1,appeal_state='permanent',revision=blocked_chats.revision+1""",
        (chat_id,),
    )


def _block_chat(
    connection: sqlite3.Connection, chat_id: int, error: Forbidden | str | None = None
) -> None:
    """Ban confirmed user blocks, preserving appealable handling for generic failures."""

    connection.execute("INSERT OR IGNORE INTO telegram_chats(chat_id) VALUES(?)", (chat_id,))
    previous = connection.execute(
        "SELECT active,appeal_state,permanent FROM blocked_chats WHERE chat_id=?", (chat_id,)
    ).fetchone()
    permanent = bool(is_user_block_error(error) or (previous and previous["permanent"]))
    if permanent:
        _ban_chat(connection, chat_id)
        return
    # A fresh generic failure must not borrow second-resolution broadcast proof
    # from an earlier block, even when both incidents occur in the same second.
    connection.execute(
        """INSERT INTO blocked_chats(chat_id,active,blocked_at,permanent,appeal_state,revision)
           VALUES(?,1,strftime('%Y-%m-%d %H:%M:%f','now'),?,?,1)
           ON CONFLICT(chat_id) DO UPDATE SET
           active=1,permanent=excluded.permanent,appeal_state=excluded.appeal_state,
           revision=blocked_chats.revision+1,
           blocked_at=strftime('%Y-%m-%d %H:%M:%f','now'),
           appeal_text=CASE WHEN excluded.permanent=1 THEN blocked_chats.appeal_text ELSE NULL END,
           appeal_at=CASE WHEN excluded.permanent=1 THEN blocked_chats.appeal_at ELSE NULL END,
           resolved_at=CASE WHEN excluded.permanent=1 THEN blocked_chats.resolved_at ELSE NULL END,
           resolved_by=CASE WHEN excluded.permanent=1
                            THEN blocked_chats.resolved_by ELSE NULL END""",
        (chat_id, int(permanent), "permanent" if permanent else "none"),
    )


class UserRegistry:
    """Store Telegram recipients, profile snapshots and broadcast outcomes."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        """Create or additively migrate the private registry schema."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_chats (
                    chat_id INTEGER PRIMARY KEY,
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            chat_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(telegram_chats)")
            }
            for column in ("username", "first_name", "last_name"):
                if column not in chat_columns:
                    connection.execute(f"ALTER TABLE telegram_chats ADD COLUMN {column} TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS broadcast_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    recipient_count INTEGER NOT NULL,
                    attempted_count INTEGER NOT NULL DEFAULT 0,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    message_sha256 TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS broadcast_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL REFERENCES broadcast_runs(id),
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    error_type TEXT NOT NULL,
                    error_text TEXT NOT NULL,
                    failed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(run_id, chat_id)
                )
                """
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS broadcast_failures_run
                   ON broadcast_failures(run_id, id)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS broadcast_releases (
                   release_id TEXT PRIMARY KEY,
                   message_sha256 TEXT NOT NULL,
                   active_run_id INTEGER REFERENCES broadcast_runs(id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS broadcast_release_deliveries (
                   release_id TEXT NOT NULL REFERENCES broadcast_releases(release_id),
                   chat_id INTEGER NOT NULL,
                   run_id INTEGER NOT NULL REFERENCES broadcast_runs(id),
                   status TEXT NOT NULL CHECK(status IN ('sending','sent','failed','uncertain')),
                   updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   PRIMARY KEY(release_id,chat_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS blocked_chats (
                   chat_id INTEGER PRIMARY KEY REFERENCES telegram_chats(chat_id),
                   active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                   blocked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   appeal_state TEXT NOT NULL DEFAULT 'none',
                   appeal_text TEXT,
                   appeal_at TEXT,
                   resolved_at TEXT,
                   resolved_by INTEGER,
                   permanent INTEGER NOT NULL DEFAULT 0 CHECK(permanent IN (0, 1))
                )"""
            )
            blocked_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(blocked_chats)")
            }
            if "permanent" not in blocked_columns:
                connection.execute(
                    """ALTER TABLE blocked_chats ADD COLUMN permanent INTEGER NOT NULL
                       DEFAULT 0 CHECK(permanent IN (0, 1))"""
                )
            if "revision" not in blocked_columns:
                connection.execute(
                    """ALTER TABLE blocked_chats ADD COLUMN revision INTEGER NOT NULL
                       DEFAULT 0 CHECK(revision>=0)"""
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS manual_access_actions (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   actor_id INTEGER NOT NULL,
                   target_chat_id INTEGER NOT NULL REFERENCES telegram_chats(chat_id),
                   action TEXT NOT NULL CHECK(action IN ('ban','unban')),
                   created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now')),
                   revision INTEGER NOT NULL,
                   UNIQUE(target_chat_id,revision))"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO blocked_chats(
                   chat_id,active,blocked_at,permanent,appeal_state,revision)
                   SELECT DISTINCT f.chat_id,1,CURRENT_TIMESTAMP,0,'none',1
                   FROM broadcast_failures f
                   JOIN broadcast_runs r ON r.id=f.run_id
                   WHERE r.status='completed' AND r.id=(
                       SELECT MAX(id) FROM broadcast_runs WHERE status='completed'
                   ) AND f.error_type='Forbidden'
                   AND f.chat_id > 0"""
            )
        os.chmod(self.path, 0o600)

    def remember(
        self,
        chat_id: int,
        username: str | object | None = _MISSING,
        first_name: str | object | None = _MISSING,
        last_name: str | object | None = _MISSING,
    ) -> None:
        """Insert a chat, refresh its last-seen time and optionally replace its profile."""

        profile_supplied = any(value is not _MISSING for value in (username, first_name, last_name))
        with closing(self._connect()) as connection, connection:
            if not profile_supplied:
                connection.execute(
                    """
                    INSERT INTO telegram_chats (chat_id)
                    VALUES (?)
                    ON CONFLICT(chat_id) DO UPDATE SET last_seen_at = CURRENT_TIMESTAMP
                    """,
                    (chat_id,),
                )
                return
            connection.execute(
                """
                INSERT INTO telegram_chats (chat_id, username, first_name, last_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    last_seen_at = CURRENT_TIMESTAMP,
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name
                """,
                (
                    chat_id,
                    _username(None if username is _MISSING else username),
                    _identity_text(
                        None if first_name is _MISSING else first_name,
                        maximum=128,
                    ),
                    _identity_text(
                        None if last_name is _MISSING else last_name,
                        maximum=128,
                    ),
                ),
            )

    def update_profile(
        self,
        chat_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> ChatRecipient:
        """Persist a best-effort Telegram lookup without changing interaction timestamps."""

        recipient = ChatRecipient(
            chat_id=chat_id,
            username=_username(username),
            first_name=_identity_text(first_name, maximum=128),
            last_name=_identity_text(last_name, maximum=128),
        )
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """UPDATE telegram_chats SET username=?,first_name=?,last_name=?
                   WHERE chat_id=?""",
                (
                    recipient.username,
                    recipient.first_name,
                    recipient.last_name,
                    recipient.chat_id,
                ),
            )
        return recipient

    def backfill_profiles(self, stores_path: Path | str) -> int:
        """Fill missing registry identities from known community profiles."""

        source_path = Path(stores_path)
        if not source_path.is_file():
            return 0
        source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source:
            if (
                source.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='community_role_profiles'"
                ).fetchone()
                is None
            ):
                return 0
            profiles = source.execute(
                """SELECT user_id,username,first_name,last_name
                   FROM community_role_profiles"""
            ).fetchall()
        changed = 0
        with closing(self._connect()) as connection, connection:
            for user_id, username, first_name, last_name in profiles:
                clean_username = _username(username)
                clean_first_name = _identity_text(first_name, maximum=128)
                clean_last_name = _identity_text(last_name, maximum=128)
                cursor = connection.execute(
                    """UPDATE telegram_chats SET
                       username=COALESCE(username,?),
                       first_name=COALESCE(first_name,?),
                       last_name=COALESCE(last_name,?)
                       WHERE chat_id=? AND (
                           (username IS NULL AND ? IS NOT NULL) OR
                           (first_name IS NULL AND ? IS NOT NULL) OR
                           (last_name IS NULL AND ? IS NOT NULL)
                       )""",
                    (
                        clean_username,
                        clean_first_name,
                        clean_last_name,
                        user_id,
                        clean_username,
                        clean_first_name,
                        clean_last_name,
                    ),
                )
                changed += cursor.rowcount
        return changed

    def private_recipient(self, chat_id: int) -> ChatRecipient | None:
        """Look up a known private contact even when excluded from broadcasts."""
        if not _is_private_chat_id(chat_id):
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT chat_id,username,first_name,last_name FROM telegram_chats WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
        return ChatRecipient(**dict(row)) if row is not None else None

    def recipients(self) -> tuple[ChatRecipient, ...]:
        """Return eligible recipients and profiles in stable order."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT c.chat_id,c.username,c.first_name,c.last_name
                   FROM telegram_chats c LEFT JOIN blocked_chats b ON b.chat_id=c.chat_id
                   WHERE COALESCE(b.active,0)=0 ORDER BY c.chat_id"""
            ).fetchall()
        return tuple(ChatRecipient(**dict(row)) for row in rows)

    def chat_ids(self) -> tuple[int, ...]:
        """Return every remembered chat identifier in stable order."""

        return tuple(recipient.chat_id for recipient in self.recipients())

    def private_chat_count(self) -> int:
        """Return the number of eligible private Telegram chats."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT COUNT(*) FROM telegram_chats c
                   LEFT JOIN blocked_chats b ON b.chat_id=c.chat_id
                   WHERE c.chat_id > 0 AND COALESCE(b.active,0)=0"""
            ).fetchone()
        return int(row[0])

    def create_broadcast_run(self, message_sha256: str, recipient_count: int) -> int:
        """Create an in-progress run before any Telegram delivery begins."""

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """INSERT INTO broadcast_runs(recipient_count,message_sha256)
                   VALUES(?,?)""",
                (recipient_count, message_sha256),
            )
            return int(cursor.lastrowid)

    def prepare_release_broadcast(
        self, release_id: str, message_sha256: str
    ) -> tuple[int, tuple[ChatRecipient, ...]]:
        """Atomically reserve a release and select eligible deliveries not yet successful.

        An active run survives process death deliberately: Telegram may have accepted its
        last message before the local acknowledgement was persisted. Neither a stale run
        nor an uncertain transport outcome may be retried automatically.
        """

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", release_id):
            raise BroadcastSafetyError(
                "Release ID must contain 1-128 letters, digits, dots, underscores or -"
            )
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO broadcast_releases(release_id,message_sha256)
                   VALUES(?,?)""",
                (release_id, message_sha256),
            )
            release = connection.execute(
                "SELECT message_sha256,active_run_id FROM broadcast_releases WHERE release_id=?",
                (release_id,),
            ).fetchone()
            if release["message_sha256"] != message_sha256:
                raise BroadcastSafetyError("Release message differs from its recorded text")
            if release["active_run_id"] is not None:
                raise BroadcastSafetyError(
                    "Release is active or interrupted; reconcile its delivery state before retrying"
                )
            uncertain = connection.execute(
                """SELECT 1 FROM broadcast_release_deliveries
                   WHERE release_id=? AND status IN ('sending','uncertain') LIMIT 1""",
                (release_id,),
            ).fetchone()
            if uncertain is not None:
                raise BroadcastSafetyError(
                    "Release has uncertain deliveries; reconcile them before retrying"
                )
            rows = connection.execute(
                """SELECT c.chat_id,c.username,c.first_name,c.last_name
                   FROM telegram_chats c LEFT JOIN blocked_chats b ON b.chat_id=c.chat_id
                   LEFT JOIN broadcast_release_deliveries d
                       ON d.chat_id=c.chat_id AND d.release_id=?
                   WHERE COALESCE(b.active,0)=0 AND COALESCE(d.status,'failed')!='sent'
                   ORDER BY c.chat_id""",
                (release_id,),
            ).fetchall()
            cursor = connection.execute(
                """INSERT INTO broadcast_runs(recipient_count,message_sha256) VALUES(?,?)""",
                (len(rows), message_sha256),
            )
            run_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE broadcast_releases SET active_run_id=? WHERE release_id=?",
                (run_id, release_id),
            )
        return run_id, tuple(ChatRecipient(**dict(row)) for row in rows)

    def begin_release_delivery(self, release_id: str, run_id: int, chat_id: int) -> None:
        """Persist intent before contacting Telegram; never overwrite a successful send."""

        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            release = connection.execute(
                "SELECT 1 FROM broadcast_releases WHERE release_id=? AND active_run_id=?",
                (release_id, run_id),
            ).fetchone()
            if release is None:
                raise BroadcastSafetyError("Release run does not own the delivery reservation")
            cursor = connection.execute(
                """INSERT INTO broadcast_release_deliveries(release_id,chat_id,run_id,status)
                   VALUES(?,?,?,'sending') ON CONFLICT(release_id,chat_id) DO UPDATE SET
                   run_id=excluded.run_id,status='sending',updated_at=CURRENT_TIMESTAMP
                   WHERE broadcast_release_deliveries.status='failed'""",
                (release_id, chat_id, run_id),
            )
            if cursor.rowcount != 1:
                raise BroadcastSafetyError("Release delivery is already sent or uncertain")

    @staticmethod
    def _finish_release_delivery(
        connection: sqlite3.Connection, release_id: str, run_id: int, chat_id: int, status: str
    ) -> None:
        cursor = connection.execute(
            """UPDATE broadcast_release_deliveries SET status=?,updated_at=CURRENT_TIMESTAMP
               WHERE release_id=? AND chat_id=? AND run_id=? AND status='sending'""",
            (status, release_id, chat_id, run_id),
        )
        if cursor.rowcount != 1:
            raise BroadcastSafetyError(
                "Release delivery reservation is missing or already resolved"
            )

    def record_broadcast_success(
        self, run_id: int, *, release_id: str | None = None, chat_id: int | None = None
    ) -> None:
        """Durably advance one successful delivery for an in-progress run."""

        with closing(self._connect()) as connection, connection:
            if release_id is not None:
                self._finish_release_delivery(connection, release_id, run_id, chat_id, "sent")
            cursor = connection.execute(
                """UPDATE broadcast_runs SET attempted_count=attempted_count+1,
                   sent_count=sent_count+1 WHERE id=? AND status='in_progress'""",
                (run_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Broadcast run is missing or already complete")

    def ban(self, chat_id: int) -> None:
        """Permanently ban one private account without changing its audit metadata."""

        if not _is_private_chat_id(chat_id):
            return
        with closing(self._connect()) as connection, connection:
            _ban_chat(connection, chat_id)

    def find_private_chats_by_username(self, username: str) -> tuple[ChatRecipient, ...]:
        """Return all exact case-insensitive matches, including blocked accounts.

        Remembered usernames can be outdated or reassigned. Multiple registered
        matches are deliberately returned rather than guessing a single target.
        """

        value = username.strip().removeprefix("@") if isinstance(username, str) else ""
        if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", value):
            raise ValueError("A valid exact Telegram username is required")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT chat_id,username,first_name,last_name FROM telegram_chats
                   WHERE chat_id>0 AND username=? COLLATE NOCASE ORDER BY chat_id""",
                (value,),
            ).fetchall()
        return tuple(ChatRecipient(**dict(row)) for row in rows)

    @staticmethod
    def _access_state(connection: sqlite3.Connection, chat_id: int) -> ChatAccessState | None:
        row = connection.execute(
            """SELECT c.chat_id,COALESCE(b.active,0) AS active,
               COALESCE(b.permanent,0) AS permanent,COALESCE(b.appeal_state,'none') AS appeal_state,
               COALESCE(b.revision,0) AS revision FROM telegram_chats c
               LEFT JOIN blocked_chats b ON b.chat_id=c.chat_id WHERE c.chat_id=?""",
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        return ChatAccessState(
            row["chat_id"],
            bool(row["active"]),
            bool(row["permanent"]),
            row["appeal_state"],
            row["revision"],
        )

    def access_state(self, chat_id: int) -> ChatAccessState | None:
        """Read a registered private account's access generation without changing it."""

        if not _is_private_chat_id(chat_id):
            raise ValueError("A registered private chat is required")
        with closing(self._connect()) as connection:
            return self._access_state(connection, chat_id)

    def set_manual_access(
        self, chat_id: int, *, banned: bool, actor_id: int, expected_revision: int
    ) -> ChatAccessState:
        """Apply an authorized caller's confirmed choice and append its audit atomically.

        The repository does not determine moderator roles or authority. The caller
        must authorize actor and target before supplying the captured revision.
        """

        if not _is_private_chat_id(chat_id) or not _is_private_chat_id(actor_id):
            raise ValueError("Valid private actor and target accounts are required")
        if type(banned) is not bool or type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("A boolean access choice and valid revision are required")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._access_state(connection, chat_id)
            if current is None:
                raise ValueError("The target private chat is not registered")
            if current.revision != expected_revision:
                raise StaleAccessState("Access changed; review the target again before confirming")
            if banned:
                _ban_chat(connection, chat_id)
            else:
                connection.execute(
                    """INSERT INTO blocked_chats(
                       chat_id,active,blocked_at,permanent,appeal_state,resolved_at,resolved_by,revision)
                       VALUES(?,0,CURRENT_TIMESTAMP,0,'approved',CURRENT_TIMESTAMP,?,1)
                       ON CONFLICT(chat_id) DO UPDATE SET active=0,permanent=0,
                       appeal_state='approved',resolved_at=CURRENT_TIMESTAMP,
                       resolved_by=excluded.resolved_by,revision=blocked_chats.revision+1""",
                    (chat_id, actor_id),
                )
            result = self._access_state(connection, chat_id)
            connection.execute(
                """INSERT INTO manual_access_actions(actor_id,target_chat_id,action,revision)
                   VALUES(?,?,?,?)""",
                (actor_id, chat_id, "ban" if banned else "unban", result.revision),
            )
            return result

    def mark_forbidden(self, chat_id: int, error: Forbidden | str | None = None) -> None:
        """Block a private chat; confirmed user blocks are permanent immediately."""

        if not _is_private_chat_id(chat_id):
            return
        if error is not None and not isinstance(error, (Forbidden, str)):
            return
        with closing(self._connect()) as connection, connection:
            _block_chat(connection, chat_id, error)

    def ban_known_blockers(self) -> int:
        """Promote current active blocks backed by a confirmed Forbidden audit record.

        The latest failure must confirm a user block and belong to the current
        block period. Older or ambiguous evidence is deliberately insufficient.
        Inactive forgiven records and existing permanent audit remain unchanged.
        """

        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT b.chat_id,f.error_text FROM blocked_chats b
                   JOIN broadcast_failures f ON f.chat_id=b.chat_id
                   WHERE b.active=1 AND b.permanent=0 AND b.chat_id>0
                   AND f.error_type='Forbidden' AND f.failed_at>=b.blocked_at
                   AND f.id=(SELECT latest.id FROM broadcast_failures latest
                             WHERE latest.chat_id=b.chat_id
                             ORDER BY latest.failed_at DESC,latest.id DESC LIMIT 1)"""
            ).fetchall()
            chat_ids = {row["chat_id"] for row in rows if is_user_block_error(row["error_text"])}
            for chat_id in chat_ids:
                _ban_chat(connection, chat_id)
            return len(chat_ids)

    def blocked_chat(self, chat_id: int) -> BlockedChat | None:
        """Return an active blocked chat, if one exists."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT b.chat_id,c.username,c.first_name,c.last_name,
                   b.appeal_state,b.appeal_text,b.permanent FROM blocked_chats b
                   JOIN telegram_chats c ON c.chat_id=b.chat_id
                   WHERE b.chat_id=? AND b.active=1""",
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values["permanent"] = bool(values["permanent"])
        return BlockedChat(**values)

    def pending_appeals(self) -> tuple[BlockedChat, ...]:
        """Return pending unblock requests for authorized reviewers."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT b.chat_id,c.username,c.first_name,c.last_name,
                   b.appeal_state,b.appeal_text,b.permanent FROM blocked_chats b
                   JOIN telegram_chats c ON c.chat_id=b.chat_id
                   WHERE b.active=1 AND b.permanent=0 AND b.appeal_state='pending'
                   ORDER BY b.appeal_at,b.chat_id"""
            ).fetchall()
        return tuple(
            BlockedChat(**{**dict(row), "permanent": bool(row["permanent"])}) for row in rows
        )

    def begin_appeal(self, chat_id: int) -> bool:
        """Start or restart an appeal for an active blocked private chat."""

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE blocked_chats SET appeal_state='awaiting_reason',revision=revision+1,
                   appeal_text=NULL, appeal_at=NULL
                   WHERE chat_id=? AND active=1 AND permanent=0
                   AND appeal_state IN ('none','rejected','awaiting_reason')""",
                (chat_id,),
            )
            return cursor.rowcount == 1

    def submit_appeal(self, chat_id: int, reason: str) -> bool:
        """Persist a short explanation from a blocked user."""

        reason = reason.strip()
        if not 5 <= len(reason) <= 500 or any(ord(char) < 32 and char != "\n" for char in reason):
            raise ValueError("Расскажите причину в 5-500 символах.")
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE blocked_chats SET appeal_state='pending',revision=revision+1,
                   appeal_text=?, appeal_at=CURRENT_TIMESTAMP
                   WHERE chat_id=? AND active=1 AND permanent=0
                   AND appeal_state='awaiting_reason'""",
                (reason, chat_id),
            )
            return cursor.rowcount == 1

    def resolve_appeal(self, chat_id: int, *, approve: bool, reviewer_id: int) -> bool:
        """Approve or decline one still-pending unblock request."""

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE blocked_chats SET active=?, appeal_state=?,revision=revision+1,
                   resolved_at=CURRENT_TIMESTAMP,resolved_by=?
                   WHERE chat_id=? AND active=1 AND permanent=0 AND appeal_state='pending'""",
                (0 if approve else 1, "approved" if approve else "rejected", reviewer_id, chat_id),
            )
            return cursor.rowcount == 1

    def record_broadcast_failure(
        self,
        run_id: int,
        recipient: ChatRecipient,
        error: TelegramError,
        bot: Bot,
        *,
        release_id: str | None = None,
    ) -> None:
        """Durably record one failure and its profile snapshot in one transaction."""

        with closing(self._connect()) as connection, connection:
            if release_id is not None:
                # A timeout or transport failure can happen after Telegram accepted the
                # message. Only definite API rejections are safe to retry automatically.
                status = (
                    "failed"
                    if isinstance(error, (BadRequest, ChatMigrated, Forbidden, RetryAfter))
                    else "uncertain"
                )
                self._finish_release_delivery(
                    connection, release_id, run_id, recipient.chat_id, status
                )
            connection.execute(
                """INSERT INTO broadcast_failures(
                   run_id,chat_id,username,first_name,last_name,error_type,error_text
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    run_id,
                    recipient.chat_id,
                    recipient.username,
                    recipient.first_name,
                    recipient.last_name,
                    type(error).__name__,
                    _safe_error_text(error, bot, recipient.chat_id),
                ),
            )
            cursor = connection.execute(
                """UPDATE broadcast_runs SET attempted_count=attempted_count+1,
                   failed_count=failed_count+1 WHERE id=? AND status='in_progress'""",
                (run_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Broadcast run is missing or already complete")
            if isinstance(error, Forbidden) and _is_private_chat_id(recipient.chat_id):
                _block_chat(connection, recipient.chat_id, error)

    def complete_broadcast_run(self, run_id: int, *, release_id: str | None = None) -> None:
        """Mark a fully iterated broadcast complete without hiding partial failures."""

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE broadcast_runs SET status='completed',
                   completed_at=CURRENT_TIMESTAMP WHERE id=? AND status='in_progress'""",
                (run_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Broadcast run is missing or already complete")
            if release_id is not None:
                cursor = connection.execute(
                    """UPDATE broadcast_releases SET active_run_id=NULL
                       WHERE release_id=? AND active_run_id=?""",
                    (release_id, run_id),
                )
                if cursor.rowcount != 1:
                    raise BroadcastSafetyError("Release run does not own the delivery reservation")

    def latest_broadcast_run(self) -> BroadcastRun | None:
        """Return the newest run and its recorded failures, including incomplete runs."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT id,started_at,completed_at,recipient_count,attempted_count,
                   sent_count,failed_count,status,message_sha256
                   FROM broadcast_runs ORDER BY id DESC LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            failure_rows = connection.execute(
                """SELECT chat_id,username,first_name,last_name,error_type,error_text,failed_at
                   FROM broadcast_failures WHERE run_id=? ORDER BY id""",
                (row["id"],),
            ).fetchall()
        failures = tuple(BroadcastFailure(**dict(failure)) for failure in failure_rows)
        return BroadcastRun(
            run_id=row["id"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            recipient_count=row["recipient_count"],
            attempted=row["attempted_count"],
            sent=row["sent_count"],
            failed=row["failed_count"],
            status=row["status"],
            message_sha256=row["message_sha256"],
            failures=failures,
        )


async def _enrich_recipient(
    bot: Bot,
    registry: UserRegistry,
    recipient: ChatRecipient,
) -> ChatRecipient:
    if recipient.has_identity:
        return recipient
    try:
        chat = await bot.get_chat(recipient.chat_id)
    except Exception:  # Telegram/client lookup failures must not block the actual delivery.
        LOGGER.warning("Could not enrich a broadcast recipient profile")
        return recipient
    finally:
        await asyncio.sleep(_PROFILE_LOOKUP_DELAY_SECONDS)
    enriched = ChatRecipient(
        chat_id=recipient.chat_id,
        username=_username(getattr(chat, "username", None)),
        first_name=_identity_text(getattr(chat, "first_name", None), maximum=128),
        last_name=_identity_text(getattr(chat, "last_name", None), maximum=128),
    )
    if not enriched.has_identity:
        return recipient
    try:
        return registry.update_profile(
            recipient.chat_id,
            enriched.username,
            enriched.first_name,
            enriched.last_name,
        )
    except sqlite3.Error:
        LOGGER.warning("Could not persist an enriched broadcast recipient profile")
        return enriched


async def broadcast_message(
    bot: Bot, registry: UserRegistry, text: str, *, release_id: str | None = None
) -> BroadcastResult:
    """Silently broadcast text, optionally deduplicating an explicitly named release.

    Counters describe this attempt, including retries. Release IDs must be reused with
    exactly the same text. Interrupted or uncertain releases require reconciliation;
    ordinary broadcasts retain their existing independent-run behavior.
    """

    message_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if release_id is None:
        recipients = registry.recipients()
        run_id = registry.create_broadcast_run(message_sha256, len(recipients))
    else:
        run_id, recipients = registry.prepare_release_broadcast(release_id, message_sha256)
    sent = 0
    failed = 0
    for remembered_recipient in recipients:
        recipient = await _enrich_recipient(bot, registry, remembered_recipient)
        if release_id is not None:
            registry.begin_release_delivery(release_id, run_id, recipient.chat_id)
        try:
            await bot.send_message(
                chat_id=recipient.chat_id,
                text=text,
                disable_notification=True,
            )
        except TelegramError as error:
            registry.record_broadcast_failure(run_id, recipient, error, bot, release_id=release_id)
            failed += 1
        else:
            registry.record_broadcast_success(
                run_id, release_id=release_id, chat_id=recipient.chat_id
            )
            sent += 1
    registry.complete_broadcast_run(run_id, release_id=release_id)
    return BroadcastResult(attempted=len(recipients), sent=sent, failed=failed)
