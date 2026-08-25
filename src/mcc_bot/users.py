"""Persistent Telegram chat registry used for operational broadcasts."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError


@dataclass(frozen=True, slots=True)
class BroadcastResult:
    """Delivery counters for one broadcast attempt."""

    attempted: int
    sent: int
    failed: int


class UserRegistry:
    """Store Telegram chat identifiers without retaining message contents."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        """Create the registry database and schema when they do not exist."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_chats (
                    chat_id INTEGER PRIMARY KEY,
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def remember(self, chat_id: int) -> None:
        """Insert a chat or refresh its last-seen timestamp."""

        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """
                INSERT INTO telegram_chats (chat_id)
                VALUES (?)
                ON CONFLICT(chat_id) DO UPDATE SET last_seen_at = CURRENT_TIMESTAMP
                """,
                (chat_id,),
            )

    def chat_ids(self) -> tuple[int, ...]:
        """Return every remembered chat identifier in stable order."""

        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                "SELECT chat_id FROM telegram_chats ORDER BY chat_id"
            ).fetchall()
        return tuple(row[0] for row in rows)


async def broadcast_message(bot: Bot, registry: UserRegistry, text: str) -> BroadcastResult:
    """Send one message to every remembered chat and return delivery counters."""

    chat_ids = registry.chat_ids()
    sent = 0
    failed = 0
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except TelegramError:
            failed += 1
        else:
            sent += 1
    return BroadcastResult(attempted=len(chat_ids), sent=sent, failed=failed)
