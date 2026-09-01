from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import NetworkError

from mcc_bot.users import UserRegistry, broadcast_message, redact_telegram_ids


def test_user_registry_remembers_unique_chat_ids(tmp_path) -> None:
    path = tmp_path / "users.sqlite3"
    registry = UserRegistry(path)
    registry.initialize()

    registry.remember(20)
    registry.remember(10)
    registry.remember(20)
    registry.remember(-1001)

    assert registry.chat_ids() == (-1001, 10, 20)
    assert registry.private_chat_count() == 2
    path.unlink()
    assert not path.exists()


def test_user_registry_requests_owner_only_permissions(tmp_path) -> None:
    path = tmp_path / "users.sqlite3"
    registry = UserRegistry(path)

    with patch("mcc_bot.users.os.chmod") as chmod:
        registry.initialize()

    chmod.assert_called_once_with(path, 0o600)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("Recipient user ID: 7788990011", "Recipient user ID: [telegram-id]"),
        (
            "Group migrated to supergroup. New chat id: -1009876543210",
            "Group migrated to supergroup. New chat id: [telegram-id]",
        ),
    ],
)
def test_telegram_id_redaction_uses_identity_context(reason, expected) -> None:
    assert redact_telegram_ids(reason) == expected


def test_user_registry_additively_migrates_legacy_schema(tmp_path) -> None:
    path = tmp_path / "users.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE telegram_chats(
               chat_id INTEGER PRIMARY KEY,
               first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
               last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
        )
        connection.execute("INSERT INTO telegram_chats(chat_id) VALUES(10)")

    registry = UserRegistry(path)
    registry.initialize()

    assert registry.chat_ids() == (10,)
    with sqlite3.connect(path) as connection:
        chat_columns = {row[1] for row in connection.execute("PRAGMA table_info(telegram_chats)")}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"username", "first_name", "last_name"} <= chat_columns
    assert {"broadcast_runs", "broadcast_failures"} <= tables


def test_user_registry_refreshes_and_clears_interaction_profile(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()

    registry.remember(10, "old_name", "Old", "Name")
    registry.remember(10, None, "New", None)

    recipient = registry.recipients()[0]
    assert (
        recipient.chat_id,
        recipient.username,
        recipient.first_name,
        recipient.last_name,
    ) == (
        10,
        None,
        "New",
        None,
    )


def test_user_registry_backfills_missing_community_profiles(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10)
    registry.remember(20, "current", None, None)
    stores_path = tmp_path / "stores.sqlite3"
    with sqlite3.connect(stores_path) as connection:
        connection.execute(
            """CREATE TABLE community_role_profiles(
               user_id INTEGER PRIMARY KEY, username TEXT,
               first_name TEXT, last_name TEXT, updated_at REAL NOT NULL)"""
        )
        connection.executemany(
            "INSERT INTO community_role_profiles VALUES(?,?,?,?,0)",
            [(10, "known", "Known", "User"), (20, "stale", "Current", "User")],
        )

    assert registry.backfill_profiles(stores_path) == 2

    recipients = registry.recipients()
    assert (recipients[0].username, recipients[0].first_name) == ("known", "Known")
    assert (recipients[1].username, recipients[1].first_name) == ("current", "Current")


def test_broadcast_message_reports_successes_and_failures(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10)
    registry.remember(20)
    bot = AsyncMock()
    bot.send_message.side_effect = [object(), NetworkError("offline")]

    result = asyncio.run(broadcast_message(bot, registry, "Бот обновлён"))

    assert result.attempted == 2
    assert result.sent == 1
    assert result.failed == 1
    assert [call.kwargs["chat_id"] for call in bot.send_message.await_args_list] == [10, 20]
    assert all(
        call.kwargs["disable_notification"] is True for call in bot.send_message.await_args_list
    )
    run = registry.latest_broadcast_run()
    assert run is not None
    assert (run.status, run.recipient_count, run.attempted, run.sent, run.failed) == (
        "completed",
        2,
        2,
        1,
        1,
    )
    assert run.message_sha256 == hashlib.sha256("Бот обновлён".encode()).hexdigest()
    assert len(run.failures) == 1
    assert (run.failures[0].error_type, run.failures[0].error_text) == (
        "NetworkError",
        "offline",
    )
    with sqlite3.connect(registry.path) as connection:
        stored_values = " ".join(
            str(value)
            for row in connection.execute("SELECT * FROM broadcast_runs")
            for value in row
        )
    assert "Бот обновлён" not in stored_values


def test_broadcast_enriches_legacy_identity_and_snapshots_failure(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10)
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(
        username="known_user",
        first_name="Known",
        last_name="User",
    )
    bot.send_message.side_effect = NetworkError("blocked")

    result = asyncio.run(broadcast_message(bot, registry, "News"))

    assert result.failed == 1
    recipient = registry.recipients()[0]
    assert (recipient.username, recipient.first_name, recipient.last_name) == (
        "known_user",
        "Known",
        "User",
    )
    failure = registry.latest_broadcast_run().failures[0]
    assert (failure.username, failure.first_name, failure.last_name) == (
        "known_user",
        "Known",
        "User",
    )


def test_broadcast_get_chat_failure_does_not_block_delivery(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10)
    bot = AsyncMock()
    bot.get_chat.side_effect = RuntimeError("lookup unavailable")

    result = asyncio.run(broadcast_message(bot, registry, "News"))

    assert (result.sent, result.failed) == (1, 0)
    bot.send_message.assert_awaited_once()
    bot.get_chat.assert_awaited_once_with(10)


def test_unexpected_interruption_leaves_durable_in_progress_run(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "first", "First")
    registry.remember(20, "second", "Second")
    bot = AsyncMock()
    bot.send_message.side_effect = [object(), RuntimeError("process interrupted")]

    with pytest.raises(RuntimeError, match="process interrupted"):
        asyncio.run(broadcast_message(bot, registry, "News"))

    run = registry.latest_broadcast_run()
    assert run is not None
    assert (run.status, run.recipient_count, run.attempted, run.sent, run.failed) == (
        "in_progress",
        2,
        1,
        1,
        0,
    )
    assert run.completed_at is None
