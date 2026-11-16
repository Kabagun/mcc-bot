"""Reminder choices keep moderation permissions and active forms intact."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from test_community_handlers import flow as flow
from test_community_handlers import start_data, update

from mcc_bot.community_handlers import callback


@pytest.mark.parametrize("user_id", [1, 2])
def test_choices_edit_notification_keep_draft_and_never_ban_or_remove_role(flow, user_id):
    service = flow.application.bot_data["community"]
    registry = flow.application.bot_data["user_registry"]
    start_data(flow, user=user_id)
    draft = service.draft(user_id)
    role = service.role(user_id)
    epoch = service.role_epoch(user_id)
    for enabled in (True, False):
        event = update(user_id, data=f"community:reminder:{int(enabled)}:{epoch}")
        event.callback_query.edit_message_text = AsyncMock()
        asyncio.run(callback(event, flow))
        assert service.digest_enabled(user_id) is enabled
        assert service.draft(user_id) == draft
        assert service.role(user_id) == role
        assert registry.blocked_chat(user_id) is None
        event.callback_query.edit_message_text.assert_awaited_once()
        event.effective_message.reply_text.assert_not_awaited()
        if not enabled:
            assert (
                "Вы остаётесь помощником"
                in event.callback_query.edit_message_text.await_args.args[0]
            )
            assert (
                "Напоминания отключены" in event.callback_query.edit_message_text.await_args.args[0]
            )


def test_stale_choice_after_role_regrant_cannot_subscribe_or_interrupt_form(flow):
    service = flow.application.bot_data["community"]
    epoch = service.role_epoch(2)
    service.set_role(1, 2, False)
    service.set_role(1, 2, True)
    start_data(flow, user=2)
    draft = service.draft(2)
    event = update(2, data=f"community:reminder:1:{epoch}")
    event.callback_query.edit_message_text = AsyncMock()
    asyncio.run(callback(event, flow))
    assert not service.digest_enabled(2)
    assert service.draft(2) == draft
    event.callback_query.edit_message_text.assert_awaited_once()
    assert "Доступ изменился" in event.callback_query.edit_message_text.await_args.args[0]


def test_ordinary_user_cannot_enable_reviewer_reminders(flow):
    event = update(10, data="community:reminder:1:0")
    event.callback_query.edit_message_text = AsyncMock()
    asyncio.run(callback(event, flow))
    assert not flow.application.bot_data["community"].digest_enabled(10)
    event.callback_query.edit_message_text.assert_awaited_once()
