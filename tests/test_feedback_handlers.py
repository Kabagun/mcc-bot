"""Feedback flows with real persistence and synthetic Telegram transport."""

# Russian UI copy and ordinary Unicode buttons are intentional.
# ruff: noqa: RUF001

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError
from telegram.ext import ApplicationHandlerStop
from test_community_handlers import flow as flow
from test_community_handlers import start_data, update

from mcc_bot.bot import lookup_media, lookup_text, start
from mcc_bot.community_handlers import CANCEL_DRAFT, FEEDBACK, GUIDE, keyboard_for
from mcc_bot.feedback import FeedbackRepository
from mcc_bot.feedback_handlers import (
    callback,
    form_keyboard,
    form_text,
    guard,
    handle_media,
    handle_text,
    owner_message,
)


@pytest.fixture
def feedback_flow(flow):
    repo = FeedbackRepository(flow.application.bot_data["community"].stores)
    repo.initialize()
    flow.application.bot_data["feedback"] = repo
    flow.bot.edit_message_text = AsyncMock()
    flow.bot.edit_message_reply_markup = AsyncMock()
    flow.bot.delete_message = AsyncMock()
    flow.bot.send_photo = AsyncMock()
    flow.next_message_id = 100
    return flow


def event(flow, user=10, **kwargs):
    result = update(user, **kwargs)
    result.effective_message.message_id = 50
    result.effective_message.chat_id = user
    flow.next_message_id += 1
    result.effective_message.reply_text.return_value = SimpleNamespace(
        chat_id=user, message_id=flow.next_message_id
    )
    if result.callback_query:
        result.callback_query.edit_message_text = AsyncMock()
    return result


def send(flow, text, *, user=10, update_id=None):
    result = event(flow, user, text=text, update_id=update_id)
    assert asyncio.run(handle_text(result, flow))
    return result


def click(flow, action="send", *, user=10, data=None):
    repo = flow.application.bot_data["feedback"]
    draft = repo.draft(user)
    data = data or f"feedback:{action}:{draft.id}:{draft.version}"
    result = event(flow, user, data=data)
    if draft:
        result.effective_message.message_id = draft.message[1]
    asyncio.run(callback(result, flow))
    return result


def photo(flow, *, user=10, caption=None, update_id=None):
    result = event(
        flow,
        user,
        photo=[SimpleNamespace(file_id="photo-ref", file_unique_id="photo-unique")],
        caption=caption,
        update_id=update_id,
    )
    assert asyncio.run(handle_media(result, flow))
    return result


def keyboard_texts(markup):
    return [button.text for row in markup.keyboard for button in row]


@pytest.mark.parametrize("user", [1, 2, 10])
def test_every_role_can_open_feedback_without_delivering_it(feedback_flow, user):
    flow = feedback_flow
    service = flow.application.bot_data["community"]
    assert FEEDBACK in keyboard_texts(keyboard_for(service, user))
    opened = send(flow, FEEDBACK, user=user)
    draft = flow.application.bot_data["feedback"].draft(user)
    assert draft.message == (user, opened.effective_message.reply_text.return_value.message_id)
    assert keyboard_texts(
        opened.effective_message.reply_text.await_args.kwargs["reply_markup"]
    ) == [CANCEL_DRAFT]
    assert not form_keyboard(draft).inline_keyboard
    flow.bot.send_message.assert_not_awaited()
    flow.bot.send_photo.assert_not_awaited()


def test_actual_routes_compose_text_and_photo_then_deliver_silently_once(feedback_flow):
    flow = feedback_flow
    repo = flow.application.bot_data["feedback"]
    asyncio.run(lookup_text(event(flow, text=FEEDBACK), flow))
    original = repo.draft(10).message
    text_event = event(flow, text="<b>broken</b> & unclear", update_id=1)
    asyncio.run(lookup_text(text_event, flow))
    asyncio.run(lookup_text(text_event, flow))
    media = event(
        flow,
        photo=[SimpleNamespace(file_id="photo-ref", file_unique_id="unique")],
        caption="Step two",
        update_id=2,
    )
    asyncio.run(lookup_media(media, flow))
    asyncio.run(lookup_media(media, flow))
    draft = repo.draft(10)
    assert draft.text == "<b>broken</b> & unclear\n\nStep two"
    assert len(draft.photos) == 1
    assert draft.message == original
    text_event.effective_message.reply_text.assert_not_awaited()
    media.effective_message.reply_text.assert_not_awaited()
    flow.bot.send_message.assert_not_awaited()
    result = click(flow)
    assert repo.draft(10) is None
    header = flow.bot.send_message.await_args.kwargs
    assert header["chat_id"] == 1
    assert header["disable_notification"] is True
    assert "&lt;b&gt;broken&lt;/b&gt; &amp; unclear" in header["text"]
    assert "@tester" in header["text"]
    flow.bot.send_photo.assert_awaited_once_with(
        chat_id=1, photo="photo-ref", disable_notification=True
    )
    assert GUIDE in keyboard_texts(
        result.effective_message.reply_text.await_args.kwargs["reply_markup"]
    )


def test_cancellation_discards_draft_and_restores_main_menu(feedback_flow):
    flow = feedback_flow
    send(flow, FEEDBACK)
    send(flow, "An idea")
    photo(flow)
    cancelled = send(flow, CANCEL_DRAFT)
    assert flow.application.bot_data["feedback"].draft(10) is None
    assert FEEDBACK in keyboard_texts(
        cancelled.effective_message.reply_text.await_args.kwargs["reply_markup"]
    )
    flow.bot.send_message.assert_not_awaited()
    flow.bot.send_photo.assert_not_awaited()


def test_clear_controls_and_stale_callbacks_keep_new_content(feedback_flow):
    flow = feedback_flow
    repo = flow.application.bot_data["feedback"]
    send(flow, FEEDBACK)
    send(flow, "Old text")
    draft = repo.draft(10)
    stale = f"feedback:clear_text:{draft.id}:{draft.version}"
    photo(flow)
    click(flow, data=stale)
    assert repo.draft(10).text == "Old text"
    click(flow, "clear_text")
    assert not repo.draft(10).text
    assert len(repo.draft(10).photos) == 1
    click(flow, "clear_photos")
    assert not repo.draft(10).photos
    assert not form_keyboard(repo.draft(10)).inline_keyboard


def test_empty_submission_never_sends_to_owner(feedback_flow):
    send(feedback_flow, FEEDBACK)
    click(feedback_flow)
    assert feedback_flow.application.bot_data["feedback"].draft(10) is not None
    feedback_flow.bot.send_message.assert_not_awaited()


def test_maximum_feedback_and_profile_fit_one_telegram_message(feedback_flow):
    repo = feedback_flow.application.bot_data["feedback"]
    draft = repo.begin(10)
    draft = repo.set_text(10, "😀" * 1500)
    preview = form_text(draft, notice="Форма изменилась. Проверьте её и нажмите кнопку ещё раз.")
    assert len(preview.encode("utf-16-le")) // 2 < 4096
    submission = repo.submit(
        10,
        draft.id,
        draft.version,
        first_name="😀" * 128,
        last_name="😀" * 128,
        username="synthetic_feedback_sender",
    )
    assert len(owner_message(submission).encode("utf-16-le")) // 2 < 4096


def test_photo_only_feedback_is_accepted(feedback_flow):
    flow = feedback_flow
    send(flow, FEEDBACK)
    photo(flow)
    click(flow)
    assert flow.application.bot_data["feedback"].draft(10) is None
    assert "без текста" in flow.bot.send_message.await_args.kwargs["text"]
    flow.bot.send_photo.assert_awaited_once()


def test_confirmed_photo_failure_retries_only_unsent_part(feedback_flow):
    flow = feedback_flow
    repo = flow.application.bot_data["feedback"]
    send(flow, FEEDBACK)
    send(flow, "An idea")
    photo(flow)
    flow.bot.send_photo.side_effect = Forbidden("synthetic rejection")
    result = click(flow)
    markup = flow.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    retry_data = markup.inline_keyboard[0][0].callback_data
    submission = repo.submission(retry_data.split(":")[-1])
    assert submission.delivery_state == "pending"
    assert [part.state for part in submission.parts] == ["sent", "pending"]
    assert "Не удалось" in result.effective_message.reply_text.await_args.args[0]
    flow.bot.send_photo.side_effect = None
    click(flow, data=retry_data)
    assert repo.submission(submission.id).delivery_state == "sent"
    assert flow.bot.send_message.await_count == 1
    assert flow.bot.send_photo.await_count == 2


def test_ambiguous_send_is_not_retried_after_restart_or_double_click(feedback_flow):
    flow = feedback_flow
    repo = flow.application.bot_data["feedback"]
    send(flow, FEEDBACK)
    send(flow, "An idea")
    photo(flow)
    draft = repo.draft(10)
    payload = f"feedback:send:{draft.id}:{draft.version}"
    flow.bot.send_photo.side_effect = NetworkError("synthetic timeout")
    click(flow, data=payload)
    flow.application.bot_data["feedback"] = FeedbackRepository(repo.stores)
    flow.application.bot_data["feedback"].initialize()
    flow.bot.send_photo.side_effect = None
    duplicate = click(flow, data=payload)
    assert flow.bot.send_message.await_count == 1
    assert flow.bot.send_photo.await_count == 1
    assert "не подтвердил" in duplicate.callback_query.edit_message_text.await_args.args[0]
    duplicate.effective_message.reply_text.assert_not_awaited()


def test_old_send_cannot_replace_new_form_or_deliver_it(feedback_flow):
    flow = feedback_flow
    repo = flow.application.bot_data["feedback"]
    send(flow, FEEDBACK)
    send(flow, "First")
    draft = repo.draft(10)
    old = f"feedback:send:{draft.id}:{draft.version}"
    click(flow)
    send(flow, FEEDBACK)
    send(flow, "Second")
    current = repo.draft(10)
    duplicate = click(flow, data=old)
    assert repo.draft(10) == current
    assert flow.bot.send_message.await_count == 1
    duplicate.effective_message.reply_text.assert_not_awaited()


def test_wrong_user_and_wrong_bound_message_cannot_submit(feedback_flow):
    flow = feedback_flow
    repo = flow.application.bot_data["feedback"]
    send(flow, FEEDBACK)
    send(flow, "An idea")
    draft = repo.draft(10)
    payload = f"feedback:send:{draft.id}:{draft.version}"
    click(flow, user=2, data=payload)
    forged = event(flow, data=payload)
    asyncio.run(callback(forged, flow))
    assert repo.draft(10) == draft
    flow.bot.send_message.assert_not_awaited()


def test_unconfigured_owner_preserves_form(feedback_flow):
    flow = feedback_flow
    send(flow, FEEDBACK)
    send(flow, "An idea")
    flow.application.bot_data["community"].owner_id = None
    result = click(flow)
    assert flow.application.bot_data["feedback"].draft(10) is not None
    assert "не настроена" in result.callback_query.answer.await_args.args[0]
    flow.bot.send_message.assert_not_awaited()


def test_feedback_does_not_discard_active_contribution_form(feedback_flow):
    flow = feedback_flow
    start_data(flow)
    service = flow.application.bot_data["community"]
    draft = service.draft(10)
    send(flow, FEEDBACK)
    assert service.draft(10) == draft
    assert flow.application.bot_data["feedback"].draft(10) is None


def test_guard_blocks_old_form_navigation_but_allows_reminder_choices(feedback_flow):
    flow = feedback_flow
    send(flow, FEEDBACK)
    blocked = event(flow, data="community:add:store_mcc")
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(guard(blocked, flow))
    blocked.callback_query.answer.assert_awaited_once()
    assert flow.application.bot_data["community"].draft(10) is None
    choice = event(flow, data="community:reminder:1:0")
    asyncio.run(guard(choice, flow))
    choice.callback_query.answer.assert_not_awaited()


def test_start_resumes_same_feedback_message(feedback_flow):
    flow = feedback_flow
    send(flow, FEEDBACK)
    send(flow, "An idea")
    draft = flow.application.bot_data["feedback"].draft(10)
    result = event(flow, text="/start")
    asyncio.run(start(result, flow))
    assert flow.application.bot_data["feedback"].draft(10) == draft
    result.effective_message.reply_text.assert_not_awaited()
    assert flow.bot.edit_message_text.await_args.kwargs["message_id"] == draft.message[1]


def test_failed_edit_and_delete_keep_one_bound_form(feedback_flow):
    flow = feedback_flow
    send(flow, FEEDBACK)
    original = flow.application.bot_data["feedback"].draft(10).message
    flow.bot.edit_message_text.side_effect = BadRequest("cannot edit synthetic message")
    flow.bot.delete_message.side_effect = Forbidden("cannot delete synthetic message")
    result = send(flow, "An idea")
    assert flow.application.bot_data["feedback"].draft(10).message == original
    notice = result.effective_message.reply_text.await_args
    assert "Не удалось обновить форму" in notice.args[0]
    assert "reply_markup" not in notice.kwargs


def test_deleted_old_form_is_replaced_and_rebound(feedback_flow):
    flow = feedback_flow
    send(flow, FEEDBACK)
    original = flow.application.bot_data["feedback"].draft(10).message
    flow.bot.edit_message_text.side_effect = BadRequest("cannot edit synthetic message")
    result = send(flow, "An idea")
    flow.bot.delete_message.assert_awaited_once_with(chat_id=original[0], message_id=original[1])
    current = flow.application.bot_data["feedback"].draft(10)
    assert current.message == (10, result.effective_message.reply_text.return_value.message_id)
    assert current.message != original


def test_document_does_not_replace_photo_or_send_anything(feedback_flow):
    flow = feedback_flow
    send(flow, FEEDBACK)
    result = event(flow, document=SimpleNamespace(mime_type="application/pdf"))
    assert asyncio.run(handle_media(result, flow))
    assert not flow.application.bot_data["feedback"].draft(10).photos
    assert "как фото" in flow.bot.edit_message_text.await_args.kwargs["text"]
    flow.bot.send_message.assert_not_awaited()


def test_group_feedback_is_rejected_without_private_draft(feedback_flow):
    flow = feedback_flow
    result = event(flow, text=FEEDBACK, chat_type="group")
    assert asyncio.run(handle_text(result, flow))
    assert flow.application.bot_data["feedback"].draft(10) is None
    assert "личном чате" in result.effective_message.reply_text.await_args.args[0]
