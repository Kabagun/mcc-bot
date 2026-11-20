"""Private block events and send failures enforce the same permanent access rule."""

# Russian product copy is intentional.

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.error import Forbidden, NetworkError
from test_application_flow import incoming, rendered, tapped
from test_application_flow import telegram_app as telegram_app
from test_community_handlers import flow as flow
from test_notifications import invite, pending
from test_notifications import service as service

from mcc_bot.bot import report_error
from mcc_bot.community_handlers import _mark_forbidden
from mcc_bot.users import UserRegistry


def membership(
    app,
    *,
    status="kicked",
    chat_id=101,
    chat_type="private",
    actor_id=101,
    bot_id=999,
    is_bot=True,
    old_status="member",
    sequence=80,
):
    return Update.de_json(
        {
            "update_id": sequence,
            "my_chat_member": {
                "chat": {"id": chat_id, "type": chat_type},
                "from": {"id": actor_id, "is_bot": False, "first_name": "Synthetic"},
                "date": 1,
                "old_chat_member": {
                    "status": old_status,
                    **({"until_date": 0} if old_status == "kicked" else {}),
                    "user": {"id": bot_id, "is_bot": is_bot, "first_name": "Bot"},
                },
                "new_chat_member": {
                    "status": status,
                    **({"until_date": 0} if status == "kicked" else {}),
                    "user": {"id": bot_id, "is_bot": is_bot, "first_name": "Bot"},
                },
            },
        },
        app.bot,
    )


def test_private_block_event_bans_immediately_and_unblock_does_not_restore_access(telegram_app):
    app, calls = telegram_app
    registry = app.bot_data["user_registry"]
    app.bot_data["community"].set_role(42, 101, True)

    async def scenario():
        async with app:
            calls.clear()
            await app.process_update(membership(app))
            assert registry.blocked_chat(101).permanent
            assert not rendered(calls)
            await app.process_update(membership(app, sequence=81))
            await app.process_update(
                membership(app, status="member", old_status="kicked", sequence=82)
            )
            assert registry.blocked_chat(101).permanent
            assert not registry.begin_appeal(101)
            calls.clear()
            await app.process_update(incoming(app, "📋 Разобрать очередь", sequence=83))
            assert rendered(calls)[-1]["text"] == (
                "🚫 Доступ к боту заблокирован.\n\n"
                "Разблокировка — 100 ⭐. Доступ восстановится сразу после оплаты."
            )
            assert rendered(calls)[-1]["reply_markup"]["inline_keyboard"][0][0] == {
                "text": "Оплатить 100 ⭐",
                "callback_data": "unban_stars:buy",
            }
            calls.clear()
            await app.process_update(tapped(app, "community:queue:0", sequence=84))
            assert "100 ⭐" in rendered(calls)[-1]["text"]
            assert "Поддержка по оплате" in str(rendered(calls)[-1]["reply_markup"])
            assert all(recipient.chat_id != 101 for recipient in registry.recipients())

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chat_id": -1001, "chat_type": "supergroup"},
        {"status": "member", "old_status": "kicked"},
        {"status": "left"},
        {"actor_id": 102},
        {"bot_id": 998},
        {"is_bot": False},
    ],
)
def test_unrelated_membership_events_do_not_ban_private_user(telegram_app, kwargs):
    app, calls = telegram_app

    async def scenario():
        async with app:
            await app.process_update(membership(app, **kwargs))
            assert app.bot_data["user_registry"].blocked_chat(101) is None
            assert not rendered(calls)

    asyncio.run(scenario())


def test_unhandled_private_reply_forbidden_records_confirmed_block_without_retry(tmp_path):
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    event = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1,
                "text": "5411",
                "chat": {"id": 101, "type": "private"},
                "from": {"id": 101, "is_bot": False, "first_name": "Synthetic"},
            },
        },
        None,
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"user_registry": registry}),
        error=Forbidden("bot was blocked by the user"),
    )
    asyncio.run(report_error(event, context))
    assert registry.blocked_chat(101).permanent


@pytest.mark.parametrize(
    "error,permanent",
    [
        (Forbidden("bot was blocked by the user"), True),
        (Forbidden("user is deactivated"), False),
    ],
)
def test_caught_community_send_failure_preserves_its_real_reason(flow, error, permanent):
    _mark_forbidden(flow, 10, error)
    assert flow.application.bot_data["user_registry"].blocked_chat(10).permanent is permanent


def test_network_error_never_bans_a_user(flow):
    _mark_forbidden(flow, 10, NetworkError("temporary network problem"))
    assert flow.application.bot_data["user_registry"].blocked_chat(10) is None


def test_reminder_forbidden_records_a_permanent_user_block(service, tmp_path):
    pending(service)
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(2)
    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=Forbidden("bot was blocked by the user"))
    )
    assert invite(service, bot, registry=registry)["uncertain"] == 1
    assert registry.blocked_chat(2).permanent
