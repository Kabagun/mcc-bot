from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from mcc_bot.users import (
    UNBAN_CHECKOUT_RESERVATION_SECONDS,
    UNBAN_CURRENCY,
    UNBAN_INVOICE_PREFIX,
    UNBAN_PRICE_XTR,
    BroadcastSafetyError,
    StaleAccessState,
    UserRegistry,
    broadcast_message,
    is_user_block_error,
    redact_telegram_ids,
)


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
        connection.execute(
            """CREATE TABLE blocked_chats(
               chat_id INTEGER PRIMARY KEY REFERENCES telegram_chats(chat_id),
               active INTEGER NOT NULL, blocked_at TEXT NOT NULL,
               appeal_state TEXT NOT NULL, appeal_text TEXT, appeal_at TEXT,
               resolved_at TEXT, resolved_by INTEGER)"""
        )
        connection.execute(
            """INSERT INTO blocked_chats(chat_id,active,blocked_at,appeal_state)
               VALUES(10,0,'2026-09-25','approved')"""
        )

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
    assert {
        "broadcast_runs",
        "broadcast_failures",
        "broadcast_releases",
        "broadcast_release_deliveries",
        "manual_access_actions",
    } <= tables
    assert registry.blocked_chat(10) is None
    assert registry.access_state(10).revision == 0
    assert registry.access_state(10).appeal_state == "approved"
    registry.mark_forbidden(10)
    assert not registry.blocked_chat(10).permanent
    registry.mark_forbidden(10, Forbidden("bot was blocked by the user"))
    assert registry.blocked_chat(10).permanent


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


def test_forbidden_blocks_private_recipient_and_appeal_can_restore_access(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "blocked", "Blocked")
    registry.remember(20)
    bot = AsyncMock()
    bot.send_message.side_effect = [Forbidden("user is deactivated"), object()]

    result = asyncio.run(broadcast_message(bot, registry, "News"))

    assert (result.sent, result.failed) == (1, 1)
    assert registry.chat_ids() == (20,)
    assert registry.private_chat_count() == 1
    assert registry.blocked_chat(10).appeal_state == "none"
    registry.remember(10, "back", "Back")
    assert registry.chat_ids() == (20,)
    assert registry.begin_appeal(10)
    with pytest.raises(ValueError, match="5-500"):
        registry.submit_appeal(10, "no")
    assert registry.submit_appeal(10, "Я хочу снова пользоваться ботом")
    assert registry.pending_appeals()[0].appeal_text == "Я хочу снова пользоваться ботом"
    assert not registry.begin_appeal(10)
    assert registry.pending_appeals()[0].appeal_state == "pending"
    assert registry.resolve_appeal(10, approve=True, reviewer_id=1)
    assert registry.blocked_chat(10) is None
    assert registry.chat_ids() == (10, 20)
    registry.initialize()
    assert registry.chat_ids() == (10, 20)
    registry.mark_forbidden(10, Forbidden("bot was blocked by the user"))
    assert registry.blocked_chat(10).permanent
    assert registry.blocked_chat(10).appeal_state == "permanent"
    assert not registry.begin_appeal(10)
    assert not registry.resolve_appeal(10, approve=True, reviewer_id=1)
    assert registry.chat_ids() == (20,)
    registry.initialize()
    assert registry.blocked_chat(10).permanent


def test_migration_blocks_only_latest_completed_forbidden_failures(tmp_path) -> None:
    path = tmp_path / "users.sqlite3"
    registry = UserRegistry(path)
    registry.initialize()
    registry.remember(10)
    registry.remember(20)
    registry.remember(30)
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """INSERT INTO broadcast_runs(id,recipient_count,status,message_sha256)
               VALUES(?,3,'completed','hash')""",
            [(1,), (2,)],
        )
        connection.executemany(
            """INSERT INTO broadcast_failures(run_id,chat_id,error_type,error_text)
               VALUES(?,?,?,?)""",
            [
                (1, 10, "Forbidden", "blocked"),
                (2, 20, "Forbidden", "blocked"),
                (2, 30, "NetworkError", "offline"),
            ],
        )
    registry.initialize()
    assert registry.chat_ids() == (10, 30)
    assert registry.blocked_chat(20) is not None
    registry.begin_appeal(20)
    registry.submit_appeal(20, "Снова хочу пользоваться")
    assert registry.resolve_appeal(20, approve=True, reviewer_id=1)
    registry.initialize()
    assert registry.chat_ids() == (10, 20, 30)


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


def test_release_retry_skips_successes_and_retries_only_eligible_failures(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    for chat_id in (10, 20, 30):
        registry.remember(chat_id, "synthetic")
    bot = AsyncMock()
    bot.send_message.side_effect = [object(), RetryAfter(1), Forbidden("blocked")]

    first = asyncio.run(broadcast_message(bot, registry, "News", release_id="test-release"))

    assert (first.attempted, first.sent, first.failed) == (3, 1, 2)
    restarted_registry = UserRegistry(registry.path)
    restarted_registry.initialize()
    restarted_registry.remember(40, "synthetic")
    retry_bot = AsyncMock()
    retry = asyncio.run(
        broadcast_message(retry_bot, restarted_registry, "News", release_id="test-release")
    )

    assert (retry.attempted, retry.sent, retry.failed) == (2, 2, 0)
    assert [call.kwargs["chat_id"] for call in retry_bot.send_message.await_args_list] == [20, 40]
    assert all(
        call.kwargs["disable_notification"] is True
        for call in retry_bot.send_message.await_args_list
    )
    retry_bot.reset_mock()
    final = asyncio.run(
        broadcast_message(retry_bot, restarted_registry, "News", release_id="test-release")
    )
    assert (final.attempted, final.sent, final.failed) == (0, 0, 0)
    retry_bot.send_message.assert_not_awaited()
    with sqlite3.connect(registry.path) as connection:
        statuses = connection.execute(
            "SELECT chat_id,status FROM broadcast_release_deliveries ORDER BY chat_id"
        ).fetchall()
    assert statuses == [(10, "sent"), (20, "sent"), (30, "failed"), (40, "sent")]


def test_legacy_broadcasts_and_distinct_releases_remain_independent(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "synthetic")
    bot = AsyncMock()

    for release_id in (None, None, "first", "second"):
        result = asyncio.run(broadcast_message(bot, registry, "News", release_id=release_id))
        assert (result.attempted, result.sent, result.failed) == (1, 1, 0)

    assert bot.send_message.await_count == 4


def test_release_rejects_changed_message_without_sending_or_exposing_it(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "synthetic")
    bot = AsyncMock()
    asyncio.run(broadcast_message(bot, registry, "Approved\n", release_id="first"))
    bot.reset_mock()

    with pytest.raises(BroadcastSafetyError, match="differs") as error:
        asyncio.run(broadcast_message(bot, registry, "Private changed text", release_id="first"))

    assert "Private changed text" not in str(error.value)
    bot.send_message.assert_not_awaited()
    with sqlite3.connect(registry.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM broadcast_runs").fetchone()[0] == 1


@pytest.mark.parametrize("release_id", ["", "has spaces", "x" * 129, "../private"])
def test_release_rejects_invalid_identifier_before_sending(tmp_path, release_id) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    bot = AsyncMock()

    with pytest.raises(BroadcastSafetyError, match="Release ID must"):
        asyncio.run(broadcast_message(bot, registry, "News", release_id=release_id))

    bot.send_message.assert_not_awaited()
    assert registry.latest_broadcast_run() is None


def test_release_rejects_concurrent_run(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "synthetic")

    async def run_concurrently() -> None:
        sending = asyncio.Event()
        finish = asyncio.Event()
        first_bot = AsyncMock()
        second_bot = AsyncMock()

        async def send_message(**_kwargs):
            sending.set()
            await finish.wait()

        first_bot.send_message.side_effect = send_message
        first_task = asyncio.create_task(
            broadcast_message(first_bot, registry, "News", release_id="first")
        )
        await sending.wait()
        try:
            with pytest.raises(BroadcastSafetyError, match="active or interrupted"):
                await broadcast_message(second_bot, registry, "News", release_id="first")
            second_bot.send_message.assert_not_awaited()
        finally:
            finish.set()
            result = await first_task
        assert result.sent == 1

    asyncio.run(run_concurrently())


def test_release_interruption_keeps_durable_reservation_and_blocks_blind_retry(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "synthetic")
    registry.remember(20, "synthetic")
    bot = AsyncMock()
    bot.send_message.side_effect = [object(), RuntimeError("process interrupted")]

    with pytest.raises(RuntimeError, match="process interrupted"):
        asyncio.run(broadcast_message(bot, registry, "News", release_id="first"))

    restarted = UserRegistry(registry.path)
    restarted.initialize()
    retry_bot = AsyncMock()
    with pytest.raises(BroadcastSafetyError, match="active or interrupted"):
        asyncio.run(broadcast_message(retry_bot, restarted, "News", release_id="first"))
    retry_bot.send_message.assert_not_awaited()
    run = restarted.latest_broadcast_run()
    assert (run.status, run.sent, run.failed) == ("in_progress", 1, 0)
    with sqlite3.connect(registry.path) as connection:
        assert connection.execute(
            "SELECT chat_id,status FROM broadcast_release_deliveries ORDER BY chat_id"
        ).fetchall() == [(10, "sent"), (20, "sending")]


def test_release_cannot_retry_after_acceptance_without_persisted_success(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "synthetic")
    bot = AsyncMock()

    with (
        patch.object(registry, "record_broadcast_success", side_effect=sqlite3.OperationalError),
        pytest.raises(sqlite3.OperationalError),
    ):
        asyncio.run(broadcast_message(bot, registry, "News", release_id="first"))

    bot.send_message.assert_awaited_once()
    bot.reset_mock()
    with pytest.raises(BroadcastSafetyError, match="active or interrupted"):
        asyncio.run(broadcast_message(bot, registry, "News", release_id="first"))
    bot.send_message.assert_not_awaited()


def test_release_distinguishes_uncertain_transport_and_definite_api_failure(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "synthetic")
    bot = AsyncMock()
    bot.send_message.side_effect = NetworkError("response lost")

    result = asyncio.run(broadcast_message(bot, registry, "News", release_id="uncertain"))

    assert (result.sent, result.failed) == (0, 1)
    bot.reset_mock()
    with pytest.raises(BroadcastSafetyError, match="uncertain deliveries"):
        asyncio.run(broadcast_message(bot, registry, "News", release_id="uncertain"))
    bot.send_message.assert_not_awaited()
    bot.send_message.side_effect = BadRequest("rejected")
    asyncio.run(broadcast_message(bot, registry, "News", release_id="definite"))
    bot.send_message.side_effect = None
    result = asyncio.run(broadcast_message(bot, registry, "News", release_id="definite"))
    assert (result.sent, result.failed) == (1, 0)


@pytest.mark.parametrize(
    "error",
    [
        Forbidden("bot was blocked by the user"),
        Forbidden("Bot was blocked by user"),
        Forbidden("  BOT  was\tblocked BY the\n USER  "),
        "bot was blocked by the user",
        " FORBIDDEN : Bot  was blocked by user. ",
        "Forbidden: bot was blocked by the user!",
    ],
)
def test_user_block_detection_accepts_only_confirmed_message_variants(error) -> None:
    assert is_user_block_error(error)


@pytest.mark.parametrize(
    "error",
    [
        None,
        Forbidden("user is deactivated"),
        Forbidden("Forbidden"),
        Forbidden("bot was kicked from the group chat"),
        Forbidden("bot is not a member of the channel chat"),
        Forbidden("not enough rights to send text messages to the chat"),
        Forbidden("bot was blocked by the user in a group"),
        NetworkError("bot was blocked by the user"),
        BadRequest("bot was blocked by the user"),
        RuntimeError("bot was blocked by the user"),
        "NetworkError: bot was blocked by the user",
        "user was blocked by the bot",
        "bot was not blocked by the user",
        "blocked",
    ],
)
def test_user_block_detection_rejects_unconfirmed_and_nonprivate_reasons(error) -> None:
    assert not is_user_block_error(error)


def test_first_confirmed_user_block_is_permanent_and_has_no_appeal(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "synthetic", "Synthetic")

    registry.mark_forbidden(10, Forbidden("bot was blocked by the user"))

    blocked = registry.blocked_chat(10)
    assert blocked.permanent
    assert blocked.appeal_state == "permanent"
    assert registry.chat_ids() == ()
    assert registry.private_chat_count() == 0
    assert not registry.begin_appeal(10)
    assert not registry.submit_appeal(10, "Synthetic appeal text")
    assert not registry.resolve_appeal(10, approve=True, reviewer_id=1)
    assert registry.pending_appeals() == ()


@pytest.mark.parametrize("error", [None, Forbidden("user is deactivated"), Forbidden("Forbidden")])
def test_unknown_forbidden_still_creates_appealable_block(tmp_path, error) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()

    registry.mark_forbidden(10, error)

    assert not registry.blocked_chat(10).permanent
    assert registry.blocked_chat(10).appeal_state == "none"
    assert registry.begin_appeal(10)


def test_non_forbidden_exception_does_not_create_block(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()

    registry.mark_forbidden(10, NetworkError("bot was blocked by the user"))

    assert registry.blocked_chat(10) is None


@pytest.mark.parametrize("chat_id", [-1001, 0, True, "10", 10.0, 2**63])
def test_explicit_ban_and_forbidden_ignore_nonprivate_identifiers(tmp_path, chat_id) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()

    registry.ban(chat_id)
    registry.mark_forbidden(chat_id, Forbidden("bot was blocked by the user"))

    assert registry.chat_ids() == ()
    with sqlite3.connect(registry.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM blocked_chats").fetchone()[0] == 0


def _block_audit(registry, chat_id):
    with sqlite3.connect(registry.path) as connection:
        return connection.execute(
            """SELECT blocked_at,appeal_text,appeal_at,resolved_at,resolved_by
               FROM blocked_chats WHERE chat_id=?""",
            (chat_id,),
        ).fetchone()


def test_ban_promotion_and_duplicate_events_preserve_block_and_appeal_audit(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.mark_forbidden(10)
    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            """UPDATE blocked_chats SET blocked_at='2000-01-01 00:00:00',
               appeal_state='pending',appeal_text='Synthetic appeal',appeal_at='2000-01-02',
               resolved_at='2000-01-03',resolved_by=1 WHERE chat_id=10"""
        )
    before = _block_audit(registry, 10)

    registry.ban(10)
    registry.ban(10)
    registry.mark_forbidden(10)
    registry.mark_forbidden(10, Forbidden("bot was blocked by the user"))
    restarted = UserRegistry(registry.path)
    restarted.initialize()
    restarted.remember(10, "synthetic_updated", "Synthetic")

    assert _block_audit(restarted, 10) == before
    assert restarted.blocked_chat(10).permanent
    assert restarted.blocked_chat(10).appeal_state == "permanent"
    assert restarted.chat_ids() == ()
    assert not restarted.begin_appeal(10)


def test_backfill_promotes_only_current_active_confirmed_private_blocks(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    for chat_id in (10, 20, 30, 40):
        registry.mark_forbidden(chat_id)
    registry.ban(50)
    registry.remember(60)
    registry.remember(-1001)
    registry.begin_appeal(40)
    registry.submit_appeal(40, "Synthetic appeal text")
    assert registry.resolve_appeal(40, approve=True, reviewer_id=1)
    run_id = registry.create_broadcast_run("synthetic-hash", 7)
    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            "INSERT INTO blocked_chats(chat_id,active,appeal_state) VALUES(-1001,1,'none')"
        )
        connection.execute(
            """UPDATE blocked_chats SET blocked_at='2000-01-01 00:00:00',
               appeal_text='Synthetic appeal history',appeal_at='2000-01-02'
               WHERE chat_id IN (10,50)"""
        )
        connection.executemany(
            """INSERT INTO broadcast_failures(run_id,chat_id,error_type,error_text)
               VALUES(?,?,?,?)""",
            [
                (run_id, 10, "Forbidden", "bot was blocked by the user"),
                (run_id, 20, "Forbidden", "user is deactivated"),
                (run_id, 30, "NetworkError", "bot was blocked by the user"),
                (run_id, 40, "Forbidden", "bot was blocked by the user"),
                (run_id, 50, "Forbidden", "bot was blocked by the user"),
                (run_id, 60, "Forbidden", "bot was blocked by the user"),
                (run_id, -1001, "Forbidden", "bot was blocked by the user"),
            ],
        )
    before_promoted = _block_audit(registry, 10)
    before_permanent = _block_audit(registry, 50)

    assert registry.ban_known_blockers() == 1
    assert registry.ban_known_blockers() == 0

    assert registry.blocked_chat(10).permanent
    assert not registry.blocked_chat(20).permanent
    assert not registry.blocked_chat(30).permanent
    assert registry.blocked_chat(40) is None
    assert registry.blocked_chat(50).permanent
    assert registry.blocked_chat(60) is None
    assert not registry.blocked_chat(-1001).permanent
    assert _block_audit(registry, 10) == before_promoted
    assert _block_audit(registry, 50) == before_permanent
    assert registry.chat_ids() == (40, 60)


def test_broadcast_confirmed_block_bans_in_same_transaction_and_preserves_failure(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "synthetic", "Synthetic")
    bot = AsyncMock()
    bot.send_message.side_effect = Forbidden("bot was blocked by the user")

    result = asyncio.run(broadcast_message(bot, registry, "Synthetic message", release_id="test"))

    assert (result.sent, result.failed) == (0, 1)
    assert registry.blocked_chat(10).permanent
    assert registry.latest_broadcast_run().failures[0].error_type == "Forbidden"
    assert registry.latest_broadcast_run().failures[0].error_text == "bot was blocked by the user"
    assert registry.chat_ids() == ()


def test_invalid_broadcast_run_rolls_back_failure_and_permanent_ban(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "synthetic", "Synthetic")
    run_id = registry.create_broadcast_run("synthetic-hash", 1)
    registry.complete_broadcast_run(run_id)
    recipient = registry.recipients()[0]

    with pytest.raises(RuntimeError, match="already complete"):
        registry.record_broadcast_failure(
            run_id, recipient, Forbidden("bot was blocked by the user"), AsyncMock()
        )

    assert registry.blocked_chat(10) is None
    assert registry.latest_broadcast_run().failures == ()


def _historical_failure(registry, chat_id, text, *, error_type="Forbidden", failed_at=None):
    run_id = registry.create_broadcast_run("synthetic-hash", 1)
    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            """INSERT INTO broadcast_failures(run_id,chat_id,error_type,error_text,failed_at)
               VALUES(?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP))""",
            (run_id, chat_id, error_type, text, failed_at),
        )


def _approve_legacy_block(registry, chat_id):
    assert registry.begin_appeal(chat_id)
    assert registry.submit_appeal(chat_id, "Synthetic forgiven appeal")
    assert registry.resolve_appeal(chat_id, approve=True, reviewer_id=1)


def test_forgiven_confirmed_history_never_bans_a_later_deactivated_incident(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    # Model an appealable user-block record created before the permanent-block policy.
    registry.mark_forbidden(10)
    _historical_failure(
        registry, 10, "bot was blocked by the user", failed_at="2000-01-01 00:00:00"
    )
    _approve_legacy_block(registry, 10)
    bot = AsyncMock()
    bot.send_message.side_effect = Forbidden("user is deactivated")

    asyncio.run(broadcast_message(bot, registry, "Synthetic message"))

    assert not registry.blocked_chat(10).permanent
    assert registry.ban_known_blockers() == 0
    assert not registry.blocked_chat(10).permanent
    assert registry.begin_appeal(10)


def test_repeated_generic_failure_cannot_reuse_same_second_forgiven_evidence(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.mark_forbidden(10)
    _historical_failure(registry, 10, "bot was blocked by the user")
    _approve_legacy_block(registry, 10)

    registry.mark_forbidden(10, Forbidden("user is deactivated"))
    registry.mark_forbidden(10)

    assert registry.ban_known_blockers() == 0
    assert not registry.blocked_chat(10).permanent
    restarted = UserRegistry(registry.path)
    restarted.initialize()
    assert restarted.ban_known_blockers() == 0
    assert not restarted.blocked_chat(10).permanent


@pytest.mark.parametrize("latest_type", ["Forbidden", "NetworkError"])
def test_latest_unconfirmed_failure_supersedes_older_confirmed_evidence(
    tmp_path, latest_type
) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.mark_forbidden(10)
    with sqlite3.connect(registry.path) as connection:
        connection.execute("UPDATE blocked_chats SET blocked_at='2000-01-01 00:00:00'")
    _historical_failure(
        registry, 10, "bot was blocked by the user", failed_at="2000-01-02 00:00:00"
    )
    _historical_failure(
        registry,
        10,
        "user is deactivated",
        error_type=latest_type,
        failed_at="2000-01-03 00:00:00",
    )

    assert registry.ban_known_blockers() == 0
    assert not registry.blocked_chat(10).permanent


@pytest.mark.parametrize(
    ("failure_time", "expected_count"),
    [("2000-01-01 00:00:00", 0), ("2000-01-02 00:00:00", 1), ("2000-01-03 00:00:00", 1)],
)
def test_backfill_requires_confirmed_evidence_from_current_block_period(
    tmp_path, failure_time, expected_count
) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.mark_forbidden(10)
    with sqlite3.connect(registry.path) as connection:
        connection.execute("UPDATE blocked_chats SET blocked_at='2000-01-02 00:00:00'")
    _historical_failure(registry, 10, "bot was blocked by the user", failed_at=failure_time)

    assert registry.ban_known_blockers() == expected_count
    assert registry.blocked_chat(10).permanent is bool(expected_count)
    assert registry.ban_known_blockers() == 0


def test_exact_username_lookup_includes_bans_and_surfaces_case_insensitive_ambiguity(tmp_path):
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "Synthetic_User", "Synthetic")
    registry.remember(20, "synthetic_user", "Other synthetic")
    registry.remember(30, "syntheticXuser", "Near match")
    registry.remember(40, "synthetic_user_extra", "Prefix match")
    registry.remember(-1001, "synthetic_user", "Group")
    registry.ban(10)

    matches = registry.find_private_chats_by_username("  @SYNTHETIC_USER  ")

    assert [match.chat_id for match in matches] == [10, 20]
    assert matches[0].username == "Synthetic_User"
    assert registry.find_private_chats_by_username("unknown") == ()
    registry.remember(20, None, "No username")
    assert [
        match.chat_id for match in registry.find_private_chats_by_username("synthetic_user")
    ] == [10]


def test_exact_username_lookup_accepts_full_length_username_without_wildcards(tmp_path):
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "x" * 32)

    assert registry.find_private_chats_by_username("@" + "X" * 32)[0].chat_id == 10
    for invalid in ("", "@", "with spaces", "x" * 33, "synthetic%", "@@synthetic", None):
        with pytest.raises(ValueError):
            registry.find_private_chats_by_username(invalid)


def _manual_actions(registry):
    with sqlite3.connect(registry.path) as connection:
        return connection.execute(
            """SELECT actor_id,target_chat_id,action,created_at,revision
               FROM manual_access_actions ORDER BY id"""
        ).fetchall()


def test_manual_toggle_records_append_only_history_and_survives_restart(tmp_path):
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10, "synthetic", "Synthetic")
    initial = registry.access_state(10)
    assert (initial.active, initial.permanent, initial.appeal_state, initial.revision) == (
        False,
        False,
        "none",
        0,
    )

    banned = registry.set_manual_access(
        10, banned=True, actor_id=1, expected_revision=initial.revision
    )
    restored = registry.set_manual_access(
        10, banned=False, actor_id=2, expected_revision=banned.revision
    )
    rebanned = registry.set_manual_access(
        10, banned=True, actor_id=1, expected_revision=restored.revision
    )
    restarted = UserRegistry(registry.path)
    restarted.initialize()

    assert (banned.active, banned.permanent, banned.revision) == (True, True, 1)
    assert (restored.active, restored.permanent, restored.appeal_state, restored.revision) == (
        False,
        False,
        "approved",
        2,
    )
    assert restarted.access_state(10) == rebanned
    assert rebanned.revision == 3
    assert _block_audit(restarted, 10)[4] == 2
    actions = _manual_actions(restarted)
    assert [(row[0], row[1], row[2], row[4]) for row in actions] == [
        (1, 10, "ban", 1),
        (2, 10, "unban", 2),
        (1, 10, "ban", 3),
    ]
    assert all(row[3] for row in actions)
    assert restarted.blocked_chat(10).permanent


def test_manual_unban_can_restore_autobanned_account_without_losing_appeal_metadata(tmp_path):
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.mark_forbidden(10)
    registry.begin_appeal(10)
    registry.submit_appeal(10, "Synthetic historical appeal")
    registry.mark_forbidden(10, Forbidden("bot was blocked by the user"))
    before = _block_audit(registry, 10)
    state = registry.access_state(10)

    restored = registry.set_manual_access(
        10, banned=False, actor_id=2, expected_revision=state.revision
    )

    assert not restored.active and not restored.permanent
    assert restored.appeal_state == "approved"
    assert registry.blocked_chat(10) is None
    assert registry.chat_ids() == (10,)
    audit = _block_audit(registry, 10)
    assert audit[:3] == before[:3]
    assert audit[3] and audit[4] == 2
    registry.initialize()
    assert registry.access_state(10) == restored
    assert registry.ban_known_blockers() == 0


def test_stale_confirmation_rejects_state_cycle_and_new_auto_ban(tmp_path):
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10)
    registry.ban(10)
    original_ban = registry.access_state(10)
    unbanned = registry.set_manual_access(
        10, banned=False, actor_id=1, expected_revision=original_ban.revision
    )
    registry.mark_forbidden(10, Forbidden("bot was blocked by the user"))
    current = registry.access_state(10)
    assert current.active == original_ban.active
    assert current.permanent == original_ban.permanent
    assert current.revision > unbanned.revision > original_ban.revision

    for stale_revision in (original_ban.revision, unbanned.revision):
        with pytest.raises(StaleAccessState):
            registry.set_manual_access(
                10, banned=False, actor_id=1, expected_revision=stale_revision
            )
    assert registry.access_state(10) == current
    assert len(_manual_actions(registry)) == 1


def test_repeat_auto_ban_invalidates_open_unban_confirmation(tmp_path):
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.ban(10)
    preview = registry.access_state(10)

    registry.ban(10)

    with pytest.raises(StaleAccessState):
        registry.set_manual_access(10, banned=False, actor_id=1, expected_revision=preview.revision)
    assert registry.access_state(10).revision > preview.revision
    assert _manual_actions(registry) == []


def test_appeal_resolution_invalidates_previous_manual_confirmation(tmp_path):
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.mark_forbidden(10)
    before_appeal = registry.access_state(10)
    registry.begin_appeal(10)
    registry.submit_appeal(10, "Synthetic valid appeal")
    preview = registry.access_state(10)
    registry.resolve_appeal(10, approve=True, reviewer_id=1)

    assert preview.revision > before_appeal.revision
    assert registry.access_state(10).revision > preview.revision
    with pytest.raises(StaleAccessState):
        registry.set_manual_access(10, banned=True, actor_id=1, expected_revision=preview.revision)
    assert not registry.access_state(10).active


def test_manual_access_requires_known_target_and_valid_ids_but_does_not_authorize_roles(tmp_path):
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    assert registry.access_state(10) is None
    with pytest.raises(ValueError, match="not registered"):
        registry.set_manual_access(10, banned=True, actor_id=1, expected_revision=0)
    assert registry.chat_ids() == ()
    registry.remember(10)
    for kwargs in (
        {"banned": True, "actor_id": -1, "expected_revision": 0},
        {"banned": True, "actor_id": True, "expected_revision": 0},
        {"banned": "yes", "actor_id": 1, "expected_revision": 0},
        {"banned": True, "actor_id": 1, "expected_revision": True},
        {"banned": True, "actor_id": 1, "expected_revision": -1},
    ):
        with pytest.raises(ValueError):
            registry.set_manual_access(10, **kwargs)
    for invalid_id in (-1001, 0, True, "10"):
        with pytest.raises(ValueError):
            registry.access_state(invalid_id)
        with pytest.raises(ValueError):
            registry.set_manual_access(invalid_id, banned=True, actor_id=1, expected_revision=0)
    assert _manual_actions(registry) == []
    # The service/UI layer must authorize this actor; persistence does not infer roles.
    assert registry.set_manual_access(10, banned=True, actor_id=999, expected_revision=0).permanent


def test_concurrent_manual_confirmations_have_one_winner(tmp_path):
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10)

    def confirm(_):
        try:
            return registry.set_manual_access(10, banned=True, actor_id=1, expected_revision=0)
        except StaleAccessState:
            return None

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(confirm, range(3)))

    assert sum(result is not None for result in results) == 1
    assert len(_manual_actions(registry)) == 1
    assert registry.access_state(10).revision == 1


def test_manual_audit_failure_rolls_back_access_and_revision(tmp_path):
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10)
    before = registry.access_state(10)
    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            """CREATE TRIGGER synthetic_audit_failure BEFORE INSERT ON manual_access_actions
               BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic audit failure"):
        registry.set_manual_access(10, banned=True, actor_id=1, expected_revision=before.revision)

    assert registry.access_state(10) == before
    assert _manual_actions(registry) == []


def _unban_registry(tmp_path):
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.ban(10)
    return registry


def _accepted_checkout(registry, *, query_id="synthetic-query"):
    payload = registry.issue_unban_invoice(10)
    assert registry.reserve_unban_checkout(10, payload, UNBAN_CURRENCY, UNBAN_PRICE_XTR, query_id)
    return payload


def _pay(registry, payload, charge_id="synthetic-charge", *, payer=10, amount=100, currency="XTR"):
    return registry.settle_unban_payment(payer, payload, currency, amount, charge_id)


def _payments(registry):
    with sqlite3.connect(registry.path) as connection:
        return connection.execute(
            """SELECT charge_id,user_id,currency,amount,state,applied_revision,created_at
               FROM paid_unban_payments ORDER BY id"""
        ).fetchall()


def test_paid_unban_invoice_is_bound_reused_and_survives_restart(tmp_path):
    registry = _unban_registry(tmp_path)
    payload = registry.issue_unban_invoice(10)

    assert payload.startswith(UNBAN_INVOICE_PREFIX)
    assert len(payload.encode()) <= 128
    assert registry.issue_unban_invoice(10) == payload
    restarted = UserRegistry(registry.path)
    restarted.initialize()
    assert restarted.issue_unban_invoice(10) == payload
    with sqlite3.connect(registry.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM paid_unban_invoices").fetchone()[0] == 1


def test_invoice_is_not_issued_for_unknown_unblocked_or_temporary_accounts(tmp_path):
    registry = _unban_registry(tmp_path)
    registry.mark_forbidden(20)
    registry.remember(30)

    for user_id in (20, 30, 40, -1001, 0, True):
        assert registry.issue_unban_invoice(user_id) is None


def test_old_invoice_cannot_checkout_after_unban_reban_cycle(tmp_path):
    registry = _unban_registry(tmp_path)
    original_revision = registry.access_state(10).revision
    original_payload = registry.issue_unban_invoice(10)
    registry.set_manual_access(10, banned=False, actor_id=1, expected_revision=original_revision)
    registry.ban(10)

    current_payload = registry.issue_unban_invoice(10)
    assert current_payload != original_payload
    assert not registry.reserve_unban_checkout(10, original_payload, "XTR", 100, "synthetic-query")
    assert registry.reserve_unban_checkout(10, current_payload, "XTR", 100, "synthetic-query")


@pytest.mark.parametrize(
    ("payer", "currency", "amount", "query_id"),
    [
        (20, "XTR", 100, "q"),
        (10, "USD", 100, "q"),
        (10, "XTR", 99, "q"),
        (10, "XTR", True, "q"),
        (10, "XTR", 100, ""),
    ],
)
def test_checkout_rejects_wrong_payer_currency_price_and_invalid_query(
    tmp_path, payer, currency, amount, query_id
):
    registry = _unban_registry(tmp_path)
    payload = registry.issue_unban_invoice(10)

    assert not registry.reserve_unban_checkout(payer, payload, currency, amount, query_id)
    with sqlite3.connect(registry.path) as connection:
        assert (
            connection.execute("SELECT checkout_query_id FROM paid_unban_invoices").fetchone()[0]
            is None
        )


def test_checkout_reservation_is_idempotent_and_expires_for_another_query(tmp_path):
    registry = _unban_registry(tmp_path)
    payload = registry.issue_unban_invoice(10)
    with patch("mcc_bot.users.time.time", return_value=1000):
        assert registry.reserve_unban_checkout(10, payload, "XTR", 100, "first-query")
        assert registry.reserve_unban_checkout(10, payload, "XTR", 100, "first-query")
        assert not registry.reserve_unban_checkout(10, payload, "XTR", 100, "second-query")
    with patch("mcc_bot.users.time.time", return_value=1000 + UNBAN_CHECKOUT_RESERVATION_SECONDS):
        assert registry.reserve_unban_checkout(10, payload, "XTR", 100, "second-query")
        assert not registry.reserve_unban_checkout(10, payload, "XTR", 100, "first-query")


def test_concurrent_invoice_and_checkout_requests_have_one_reservation(tmp_path):
    registry = _unban_registry(tmp_path)
    with ThreadPoolExecutor(max_workers=3) as executor:
        invoices = list(executor.map(lambda _: registry.issue_unban_invoice(10), range(3)))
    assert len(set(invoices)) == 1
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(
            executor.map(
                lambda number: registry.reserve_unban_checkout(
                    10, invoices[0], "XTR", 100, f"query-{number}"
                ),
                range(3),
            )
        )
    assert results.count(True) == 1


def test_paid_unban_restores_access_and_records_payment_in_same_commit(tmp_path):
    registry = _unban_registry(tmp_path)
    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            """UPDATE blocked_chats SET appeal_text='Synthetic past appeal',
               appeal_at='2000-01-01',resolved_at='2000-01-02',resolved_by=1"""
        )
    before = registry.access_state(10)
    old_audit = _block_audit(registry, 10)
    payload = _accepted_checkout(registry)

    assert _pay(registry, payload) == "applied"

    current = registry.access_state(10)
    assert (current.active, current.permanent, current.appeal_state) == (False, False, "approved")
    assert current.revision == before.revision + 1
    assert registry.chat_ids() == (10,)
    assert _block_audit(registry, 10)[:3] == old_audit[:3]
    assert _block_audit(registry, 10)[4] is None
    assert _manual_actions(registry) == []
    payment = _payments(registry)[0]
    assert payment[:6] == ("synthetic-charge", 10, "XTR", 100, "applied", current.revision)
    assert payment[6]


def test_duplicate_charge_never_lifts_later_ban_after_restart(tmp_path):
    registry = _unban_registry(tmp_path)
    payload = _accepted_checkout(registry)
    assert _pay(registry, payload) == "applied"
    registry.ban(10)
    later_ban = registry.access_state(10)
    restarted = UserRegistry(registry.path)
    restarted.initialize()

    assert _pay(restarted, payload) == "duplicate_applied"
    assert restarted.access_state(10) == later_ban
    assert len(_payments(restarted)) == 1


@pytest.mark.parametrize("change", ["manual_ban", "auto_ban", "temporary_ban"])
def test_confirmed_accepted_payment_fulfills_even_after_moderation_changed_ban(tmp_path, change):
    registry = _unban_registry(tmp_path)
    payload = _accepted_checkout(registry)
    current = registry.access_state(10)
    if change == "manual_ban":
        registry.set_manual_access(10, banned=True, actor_id=1, expected_revision=current.revision)
    elif change == "auto_ban":
        registry.ban(10)
    else:
        registry.set_manual_access(10, banned=False, actor_id=1, expected_revision=current.revision)
        registry.mark_forbidden(10)
    changed = registry.access_state(10)

    assert _pay(registry, payload) == "applied"

    restored = registry.access_state(10)
    assert not restored.active and not restored.permanent
    assert restored.revision == changed.revision + 1


def test_already_unblocked_payment_is_applied_without_touching_access_revision_or_metadata(
    tmp_path,
):
    registry = _unban_registry(tmp_path)
    payload = _accepted_checkout(registry)
    before = registry.access_state(10)
    unblocked = registry.set_manual_access(
        10, banned=False, actor_id=1, expected_revision=before.revision
    )
    metadata = _block_audit(registry, 10)

    assert _pay(registry, payload) == "applied"

    assert registry.access_state(10) == unblocked
    assert _block_audit(registry, 10) == metadata
    assert _payments(registry)[0][5] == unblocked.revision
    assert len(_manual_actions(registry)) == 1


def test_different_valid_real_charges_on_one_accepted_invoice_are_each_fulfilled(tmp_path):
    registry = _unban_registry(tmp_path)
    payload = _accepted_checkout(registry)
    assert _pay(registry, payload, "first-charge") == "applied"
    registry.ban(10)

    assert _pay(registry, payload, "second-charge") == "applied"

    assert not registry.access_state(10).active
    assert [row[0] for row in _payments(registry)] == ["first-charge", "second-charge"]
    registry.ban(10)
    final_ban = registry.access_state(10)
    assert _pay(registry, payload, "first-charge") == "duplicate_applied"
    assert _pay(registry, payload, "second-charge") == "duplicate_applied"
    assert registry.access_state(10) == final_ban


def test_concurrent_duplicate_charge_events_apply_once(tmp_path):
    registry = _unban_registry(tmp_path)
    payload = _accepted_checkout(registry)
    before = registry.access_state(10)
    with ThreadPoolExecutor(max_workers=3) as executor:
        outcomes = list(executor.map(lambda _: _pay(registry, payload), range(3)))

    assert outcomes.count("applied") == 1
    assert outcomes.count("duplicate_applied") == 2
    assert registry.access_state(10).revision == before.revision + 1
    assert len(_payments(registry)) == 1


def test_concurrent_different_paid_charges_record_both_without_unnecessary_state_mutation(tmp_path):
    registry = _unban_registry(tmp_path)
    payload = _accepted_checkout(registry)
    before = registry.access_state(10)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(lambda charge: _pay(registry, payload, charge), ("charge-a", "charge-b"))
        )

    assert outcomes == ["applied", "applied"]
    assert registry.access_state(10).revision == before.revision + 1
    assert len(_payments(registry)) == 2


@pytest.mark.parametrize("anomaly", ["wrong_payer", "wrong_amount", "wrong_currency", "unknown"])
def test_anomalous_receipt_is_private_review_only_with_no_access_change(tmp_path, anomaly):
    registry = _unban_registry(tmp_path)
    payload = _accepted_checkout(registry)
    before = registry.access_state(10)
    kwargs = {}
    if anomaly == "wrong_payer":
        kwargs["payer"] = 20
    elif anomaly == "wrong_amount":
        kwargs["amount"] = 99
    elif anomaly == "wrong_currency":
        kwargs["currency"] = "USD"
    elif anomaly == "unknown":
        payload = UNBAN_INVOICE_PREFIX + "synthetic-unknown"

    assert _pay(registry, payload, **kwargs) == "needs_review"
    assert _pay(registry, payload, **kwargs) == "duplicate_needs_review"

    assert registry.access_state(10) == before
    assert _payments(registry)[0][4] == "needs_review"
    assert len(_payments(registry)) == 1


def test_trusted_successful_payment_fulfills_issued_invoice_without_checkout_history(tmp_path):
    registry = _unban_registry(tmp_path)
    payload = registry.issue_unban_invoice(10)
    registry.ban(10)
    before = registry.access_state(10)

    assert not registry.reserve_unban_checkout(10, payload, "XTR", 100, "late-query")
    assert _pay(registry, payload) == "applied"
    assert not registry.access_state(10).active
    assert registry.access_state(10).revision == before.revision + 1


def test_replayed_charge_with_changed_bindings_cannot_overwrite_payment_or_unban(tmp_path):
    registry = _unban_registry(tmp_path)
    payload = _accepted_checkout(registry)
    assert _pay(registry, payload) == "applied"
    registry.ban(10)
    before = registry.access_state(10)
    ledger = _payments(registry)

    assert _pay(registry, payload, payer=20) == "ignored"
    assert _pay(registry, payload, amount=101) == "ignored"
    assert _pay(registry, UNBAN_INVOICE_PREFIX + "changed") == "ignored"
    assert _pay(registry, "unrelated:payload") == "ignored"
    assert registry.access_state(10) == before
    assert _payments(registry) == ledger


def test_payment_audit_and_access_change_roll_back_together(tmp_path):
    registry = _unban_registry(tmp_path)
    payload = _accepted_checkout(registry)
    before = registry.access_state(10)
    with sqlite3.connect(registry.path) as connection:
        connection.execute(
            """CREATE TRIGGER synthetic_unban_failure BEFORE UPDATE ON blocked_chats
               BEGIN SELECT RAISE(ABORT,'synthetic payment failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic payment failure"):
        _pay(registry, payload)

    assert registry.access_state(10) == before
    assert _payments(registry) == []
    with sqlite3.connect(registry.path) as connection:
        assert (
            connection.execute("SELECT settled_charge_id FROM paid_unban_invoices").fetchone()[0]
            is None
        )
        connection.execute("DROP TRIGGER synthetic_unban_failure")
    assert _pay(registry, payload) == "applied"
