"""Private feedback composition and silent, explicit delivery to the bot owner."""

# Russian UI copy and ordinary Unicode buttons are intentional.
# ruff: noqa: RUF001

from __future__ import annotations

import logging
import re
from contextlib import suppress
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from .community import CommunityError, CommunityService
from .community_handlers import (
    CANCEL_DRAFT,
    FEEDBACK,
    draft_keyboard_for,
    keyboard_for,
)
from .feedback import (
    MAX_FEEDBACK_PHOTOS,
    MAX_FEEDBACK_TEXT,
    FeedbackDraft,
    FeedbackError,
    FeedbackRepository,
    FeedbackSubmission,
    StaleFeedback,
    text_units,
)

LOGGER = logging.getLogger(__name__)
FORM_INTRO = (
    "💬 Отзыв админу\n\n"
    "Что стоит исправить или улучшить? Расскажите, что произошло и какого результата "
    "вы ожидали. Можно просто поделиться впечатлениями.\n\n"
    "Бот развивается благодаря вашим данным, проверкам и отзывам. "
    "Без обратной связи ошибки остаются, а полезных данных не становится больше.\n\n"
    "Напишите текст и при желании прикрепите картинки. "
    "Когда всё готово, нажмите «Отправить» под этим сообщением. "
    "До этого админ ничего не получит."
)
SENT_MESSAGE = (
    "Спасибо! Отзыв отправлен админу.\n\n"
    "Ваши замечания помогают находить ошибки и делать бота полезнее. "
    "Спасибо, что развиваете его вместе с нами!"
)
CALLBACK = re.compile(r"feedback:(send|clear_text|clear_photos):([a-f0-9]{32}):([0-9]{1,9})")
RETRY_CALLBACK = re.compile(r"feedback:retry:([a-f0-9]{32})")


def _identity(update: Update) -> int | None:
    chat, user = getattr(update, "effective_chat", None), getattr(update, "effective_user", None)
    if chat is None or user is None or chat.type != "private" or chat.id != user.id:
        return None
    return user.id


def _repository(context: ContextTypes.DEFAULT_TYPE) -> FeedbackRepository | None:
    return context.application.bot_data.get("feedback")


def form_text(draft: FeedbackDraft, *, notice: str = "") -> str:
    """Show the exact composed text and attachment count before explicit submission."""
    text = escape(FORM_INTRO)
    if notice:
        text += f"\n\n<b>{escape(notice)}</b>"
    text += f"\n\n<b>Текст ({text_units(draft.text)}/{MAX_FEEDBACK_TEXT}):</b>\n"
    text += escape(draft.text) if draft.text else "Пока не добавлен."
    text += f"\n\nКартинок: {len(draft.photos)}/{MAX_FEEDBACK_PHOTOS}."
    return text


def form_keyboard(draft: FeedbackDraft) -> InlineKeyboardMarkup:
    rows = []
    if draft.text or draft.photos:
        rows.append(
            [
                InlineKeyboardButton(
                    "📨 Отправить", callback_data=f"feedback:send:{draft.id}:{draft.version}"
                )
            ]
        )
    for label, action, visible in (
        ("Очистить текст", "clear_text", bool(draft.text)),
        ("Убрать картинки", "clear_photos", bool(draft.photos)),
    ):
        if visible:
            rows.append(
                [
                    InlineKeyboardButton(
                        label, callback_data=f"feedback:{action}:{draft.id}:{draft.version}"
                    )
                ]
            )
    return InlineKeyboardMarkup(rows)


async def _render(
    update: Update, context: ContextTypes.DEFAULT_TYPE, draft: FeedbackDraft, *, notice: str = ""
) -> None:
    repo = _repository(context)
    text, markup = form_text(draft, notice=notice), form_keyboard(draft)
    if draft.message is not None:
        chat_id, message_id = draft.message
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return
        except BadRequest as error:
            if "message is not modified" in str(error).casefold():
                return
        except TelegramError:
            pass
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            # Keep the bound form. A failed delete must never produce a second form.
            await update.effective_message.reply_text(
                "Не удалось обновить форму. Ваш текст и картинки сохранены. "
                "Попробуйте ещё раз или нажмите «❌ Отменить»."
            )
            return
    sent = await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=draft_keyboard_for()
    )
    repo.bind_message(draft.user_id, sent.chat_id, sent.message_id, draft_id=draft.id)
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=sent.chat_id,
            message_id=sent.message_id,
            reply_markup=markup,
        )
    except TelegramError:
        LOGGER.info("Could not attach feedback actions; the saved form can be resumed")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Keep an existing feedback form and its cancel-only interaction mode."""
    user_id, repo = _identity(update), _repository(context)
    if user_id is None or repo is None:
        return False
    draft = repo.draft(user_id)
    if draft is None:
        return False
    await _render(update, context, draft)
    return True


async def _begin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    service: CommunityService = context.application.bot_data["community"]
    try:
        current = service.draft(user_id)
    except CommunityError:
        current = None
    if current is not None:
        if (
            current.data.get("draft_mode")
            or current.data.get("dirty")
            or service.draft_has_media(user_id, current.id)
        ):
            await update.effective_message.reply_text(
                "Сначала завершите текущее действие или нажмите «❌ Отменить». "
                "Затем откройте отзыв.",
                reply_markup=draft_keyboard_for(),
            )
            return
        service.cancel_draft(user_id, current.id, current.version)
    draft = _repository(context).begin(user_id)
    await _render(update, context, draft)


async def _end(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    bound: tuple[int, int] | None,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Retire the form and restore the main menu without duplicating a live form."""
    retired = True
    if bound is not None:
        try:
            await context.bot.delete_message(chat_id=bound[0], message_id=bound[1])
        except TelegramError:
            retired = False
            try:
                await context.bot.edit_message_text(
                    chat_id=bound[0],
                    message_id=bound[1],
                    text=text,
                    reply_markup=markup,
                )
            except TelegramError:
                LOGGER.info("Could not retire a closed feedback message")
    service = context.application.bot_data["community"]
    sent = await update.effective_message.reply_text(
        text if retired else "Главное меню.", reply_markup=keyboard_for(service, user_id)
    )
    if retired and markup is not None:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=sent.chat_id,
                message_id=sent.message_id,
                reply_markup=markup,
            )
        except TelegramError:
            LOGGER.info("Could not attach a feedback retry action")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message, repo = update.effective_message, _repository(context)
    if repo is None or message is None or not isinstance(message.text, str):
        return False
    user_id = _identity(update)
    if user_id is None:
        if message.text.strip() == FEEDBACK:
            await message.reply_text("Написать отзыв можно в личном чате с ботом.")
            return True
        return False
    draft = repo.draft(user_id)
    if message.text.strip() == FEEDBACK:
        if draft is None:
            await _begin(update, context, user_id)
        else:
            await _render(update, context, draft)
        return True
    if draft is None:
        return False
    if message.text.strip() == CANCEL_DRAFT:
        repo.discard(user_id, draft_id=draft.id)
        await _end(
            update, context, user_id, draft.message, "Отзыв отменён. Админу ничего не отправлено."
        )
        return True
    try:
        draft = repo.append_input(
            user_id,
            text=message.text,
            draft_id=draft.id,
            expected_version=draft.version,
            update_id=getattr(update, "update_id", None),
        )
    except FeedbackError:
        await _render(
            update,
            context,
            draft,
            notice=f"Не удалось добавить текст. Лимит — {MAX_FEEDBACK_TEXT} знаков.",
        )
    else:
        await _render(update, context, draft)
    return True


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id, repo = _identity(update), _repository(context)
    if user_id is None or repo is None:
        return False
    draft = repo.draft(user_id)
    if draft is None:
        return False
    message = update.effective_message
    photos = getattr(message, "photo", None)
    if not photos:
        await _render(update, context, draft, notice="Пришлите картинку как фото, а не как файл.")
        return True
    photo = photos[-1]
    try:
        draft = repo.append_input(
            user_id,
            text=getattr(message, "caption", None) or "",
            file_id=photo.file_id,
            unique_id=photo.file_unique_id,
            draft_id=draft.id,
            expected_version=draft.version,
            update_id=getattr(update, "update_id", None),
        )
    except FeedbackError:
        await _render(
            update,
            context,
            draft,
            notice=(
                f"Не удалось добавить картинку с подписью. Лимиты: {MAX_FEEDBACK_PHOTOS} картинок "
                f"и {MAX_FEEDBACK_TEXT} знаков текста."
            ),
        )
    else:
        await _render(update, context, draft)
    return True


def owner_message(submission: FeedbackSubmission) -> str:
    name = " ".join(value for value in (submission.first_name, submission.last_name) if value)
    identity = escape(name or "Пользователь")
    if submission.username:
        identity += f" · @{escape(submission.username)}"
    return f"💬 <b>Новый отзыв</b>\n\nОт: {identity}\nКартинок: {len(submission.photos)}\n\n" + (
        escape(submission.text) if submission.text else "Отзыв без текста, с картинками."
    )


async def _deliver(
    context: ContextTypes.DEFAULT_TYPE, submission: FeedbackSubmission
) -> FeedbackSubmission:
    repo = _repository(context)
    owner_id = context.application.bot_data["community"].owner_id
    for part in submission.parts:
        reserved = repo.reserve_delivery_part(submission.id, part.position)
        if reserved is None:
            continue
        try:
            if part.kind == "text":
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=owner_message(submission),
                    parse_mode=ParseMode.HTML,
                    disable_notification=True,
                )
            else:
                await context.bot.send_photo(
                    chat_id=owner_id,
                    photo=part.file_id,
                    disable_notification=True,
                )
        except (BadRequest, Forbidden, RetryAfter):
            repo.finish_delivery_part(submission.id, part.position, "pending")
            break
        except TelegramError:
            repo.finish_delivery_part(submission.id, part.position, "uncertain")
            break
        else:
            repo.finish_delivery_part(submission.id, part.position, "sent")
    return repo.submission(submission.id, user_id=submission.user_id)


def _result(submission: FeedbackSubmission) -> tuple[str, InlineKeyboardMarkup | None]:
    if submission.delivery_state == "sent":
        return SENT_MESSAGE, None
    if submission.delivery_state == "uncertain":
        return (
            "Отзыв сохранён, но Telegram не подтвердил доставку всех частей. "
            "Часть сообщения могла дойти до админа. Повторно автоматически не отправляем, "
            "чтобы не прислать дубликат.",
            None,
        )
    return (
        "Отзыв сохранён. Не удалось доставить его полностью. "
        "Нажмите «Повторить отправку» — уже доставленные части повторно не уйдут.",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Повторить отправку", callback_data=f"feedback:retry:{submission.id}"
                    )
                ]
            ]
        ),
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query, repo, user_id = update.callback_query, _repository(context), _identity(update)
    if query is None:
        return
    if user_id is None or repo is None or not isinstance(query.data, str):
        await query.answer("Откройте отзыв в личном чате с ботом.")
        return
    match, retry = CALLBACK.fullmatch(query.data), RETRY_CALLBACK.fullmatch(query.data)
    if match is None and retry is None:
        await query.answer("Кнопка устарела. Откройте отзыв заново.")
        return
    if context.application.bot_data["community"].owner_id is None:
        await query.answer("Отправка отзывов пока не настроена.", show_alert=True)
        return
    await query.answer()
    was_current = False
    try:
        if retry is not None:
            submission = repo.submission(retry[1], user_id=user_id)
        else:
            action, draft_id, version = match[1], match[2], int(match[3])
            draft = repo.draft(user_id)
            was_current = draft is not None and draft.id == draft_id
            if was_current and draft.message != (
                update.effective_chat.id,
                getattr(query.message, "message_id", None),
            ):
                raise StaleFeedback("Feedback message does not match its form")
            if action != "send":
                if draft is None or draft.id != draft_id:
                    raise StaleFeedback("Old feedback action")
                if action == "clear_text":
                    draft = repo.set_text(user_id, "", draft_id=draft_id, expected_version=version)
                else:
                    draft = repo.clear_photos(user_id, draft_id=draft_id, expected_version=version)
                await _render(update, context, draft)
                return
            user = update.effective_user
            submission = repo.submit(
                user_id,
                draft_id,
                version,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
            )
        submission = await _deliver(context, submission)
    except FeedbackError:
        draft = repo.draft(user_id)
        if draft is not None:
            await _render(
                update,
                context,
                draft,
                notice="Форма изменилась. Проверьте её и нажмите кнопку ещё раз.",
            )
        return
    text, markup = _result(submission)
    if repo.draft(user_id) is not None or retry is not None or not was_current:
        # Replayed old buttons cannot replace the new form's cancel-only keyboard.
        with suppress(TelegramError):
            await query.edit_message_text(text, reply_markup=markup)
        return
    await _end(update, context, user_id, submission.message, text, markup)


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep callbacks from old screens from opening another form over feedback."""
    repo, user_id = _repository(context), _identity(update)
    if repo is None or user_id is None or repo.draft(user_id) is None:
        return
    query = update.callback_query
    if query is not None:
        data = query.data if isinstance(query.data, str) else ""
        if data.startswith(("feedback:", "community:reminder:")):
            return
        await query.answer("Сначала отправьте отзыв или нажмите «❌ Отменить».", show_alert=True)
        raise ApplicationHandlerStop
    text = getattr(update.effective_message, "text", "") or ""
    if text.startswith("/") and text.split()[0].split("@")[0].casefold() != "/start":
        await resume(update, context)
        raise ApplicationHandlerStop
