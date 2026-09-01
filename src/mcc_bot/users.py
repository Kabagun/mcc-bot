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
from telegram.error import TelegramError

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

    def recipients(self) -> tuple[ChatRecipient, ...]:
        """Return every remembered recipient and profile in stable order."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT chat_id,username,first_name,last_name
                   FROM telegram_chats ORDER BY chat_id"""
            ).fetchall()
        return tuple(ChatRecipient(**dict(row)) for row in rows)

    def chat_ids(self) -> tuple[int, ...]:
        """Return every remembered chat identifier in stable order."""

        return tuple(recipient.chat_id for recipient in self.recipients())

    def private_chat_count(self) -> int:
        """Return the number of remembered private Telegram chats."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM telegram_chats WHERE chat_id > 0"
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

    def record_broadcast_success(self, run_id: int) -> None:
        """Durably advance one successful delivery for an in-progress run."""

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE broadcast_runs SET attempted_count=attempted_count+1,
                   sent_count=sent_count+1 WHERE id=? AND status='in_progress'""",
                (run_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Broadcast run is missing or already complete")

    def record_broadcast_failure(
        self,
        run_id: int,
        recipient: ChatRecipient,
        error: TelegramError,
        bot: Bot,
    ) -> None:
        """Durably record one failure and its profile snapshot in one transaction."""

        with closing(self._connect()) as connection, connection:
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

    def complete_broadcast_run(self, run_id: int) -> None:
        """Mark a fully iterated broadcast complete without hiding partial failures."""

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """UPDATE broadcast_runs SET status='completed',
                   completed_at=CURRENT_TIMESTAMP WHERE id=? AND status='in_progress'""",
                (run_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Broadcast run is missing or already complete")

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


async def broadcast_message(bot: Bot, registry: UserRegistry, text: str) -> BroadcastResult:
    """Silently broadcast text while durably recording counters and failures."""

    recipients = registry.recipients()
    message_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    run_id = registry.create_broadcast_run(message_sha256, len(recipients))
    sent = 0
    failed = 0
    for remembered_recipient in recipients:
        recipient = await _enrich_recipient(bot, registry, remembered_recipient)
        try:
            await bot.send_message(
                chat_id=recipient.chat_id,
                text=text,
                disable_notification=True,
            )
        except TelegramError as error:
            registry.record_broadcast_failure(run_id, recipient, error, bot)
            failed += 1
        else:
            registry.record_broadcast_success(run_id)
            sent += 1
    registry.complete_broadcast_run(run_id)
    return BroadcastResult(attempted=len(recipients), sent=sent, failed=failed)
