"""Telegram Stars checkout and fulfillment for bot access restoration."""

# Russian UI copy is intentional.
# ruff: noqa: RUF001

from __future__ import annotations

import logging
import sqlite3

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from .community_handlers import keyboard_for
from .users import UNBAN_CURRENCY, UNBAN_INVOICE_PREFIX, UNBAN_PRICE_XTR, UserRegistry

LOGGER = logging.getLogger(__name__)
UNBAN_PRICE = f"{UNBAN_PRICE_XTR} ⭐"
UNBAN_TEXT = (
    "🚫 Доступ к боту заблокирован.\n\n"
    f"Разблокировка — {UNBAN_PRICE}. Доступ восстановится сразу после оплаты."
)


def unban_payment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Оплатить {UNBAN_PRICE}", callback_data="unban_stars:buy")],
            [InlineKeyboardButton("Поддержка по оплате", callback_data="unban_stars:support")],
        ]
    )


def _private_payer(update: Update) -> int | None:
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None or chat.type != "private" or user.id != chat.id:
        return None
    return user.id


def _support(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, InlineKeyboardMarkup | None]:
    data = context.application.bot_data
    owner_id = data["community"].owner_id
    registry: UserRegistry = data["user_registry"]
    owner = registry.private_recipient(owner_id) if owner_id is not None else None
    if owner is not None and owner.username:
        address = f"https://t.me/{owner.username}"
    elif owner_id is not None:
        address = f"tg://user?id={owner_id}"
    else:
        address = None
    text = "За платежи в боте отвечает его администратор. Напишите ему и приложите чек из Telegram."
    markup = (
        InlineKeyboardMarkup([[InlineKeyboardButton("Написать администратору", url=address)]])
        if address is not None
        else None
    )
    return text, markup


async def payment_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if _private_payer(update) is not None and message is not None:
        text, markup = _support(context)
        await message.reply_text(text, reply_markup=markup)
    raise ApplicationHandlerStop


async def unban_payment_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query, payer_id = update.callback_query, _private_payer(update)
    if query is None:
        return
    if payer_id is None:
        await query.answer("Оплата доступна в личном чате с ботом.", show_alert=True)
        raise ApplicationHandlerStop
    if query.data == "unban_stars:support":
        await query.answer()
        await payment_support(update, context)
        return

    registry: UserRegistry = context.application.bot_data["user_registry"]
    payload = registry.issue_unban_invoice(payer_id)
    if payload is None:
        await query.answer("Этот счёт больше не нужен.", show_alert=True)
        raise ApplicationHandlerStop
    await query.answer()
    await context.bot.send_invoice(
        chat_id=payer_id,
        title="Разблокировка бота",
        description="После оплаты доступ к боту для вашего аккаунта восстановится автоматически.",
        payload=payload,
        provider_token="",
        currency=UNBAN_CURRENCY,
        prices=[LabeledPrice("Разблокировка бота", UNBAN_PRICE_XTR)],
        start_parameter="unban-stars",
    )
    raise ApplicationHandlerStop


async def precheckout_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if query is None:
        return
    registry: UserRegistry = context.application.bot_data["user_registry"]
    try:
        accepted = registry.reserve_unban_checkout(
            query.from_user.id, query.invoice_payload, query.currency, query.total_amount, query.id
        )
    except sqlite3.Error:
        LOGGER.warning("Could not reserve Stars checkout")
        accepted = False
    if accepted:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Счёт устарел или платёж уже обрабатывается.")
    raise ApplicationHandlerStop


async def complete_unban_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, payer_id = update.effective_message, _private_payer(update)
    payment = message.successful_payment if message is not None else None
    if payment is None or not payment.invoice_payload.startswith(UNBAN_INVOICE_PREFIX):
        return
    if payer_id is None:
        raise ApplicationHandlerStop
    data = context.application.bot_data
    registry: UserRegistry = data["user_registry"]
    try:
        outcome = registry.settle_unban_payment(
            payer_id,
            payment.invoice_payload,
            payment.currency,
            payment.total_amount,
            payment.telegram_payment_charge_id,
        )
    except sqlite3.Error:
        LOGGER.error("Could not record confirmed Stars payment")
        await message.reply_text(
            "Telegram подтвердил оплату. Если доступ не восстановился, напишите в поддержку.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Поддержка по оплате", callback_data="unban_stars:support")]]
            ),
        )
        raise ApplicationHandlerStop from None
    if outcome == "applied":
        await message.reply_text(
            "✅ Оплата получена. Доступ к боту восстановлен.",
            reply_markup=keyboard_for(data["community"], payer_id),
        )
    elif outcome == "needs_review":
        text, markup = _support(context)
        await message.reply_text(f"По этому платежу нужна помощь.\n\n{text}", reply_markup=markup)
    raise ApplicationHandlerStop


def install_unban_payments(application: Application) -> None:
    """Allow payment updates and support even while ordinary bot access is blocked."""
    group = -5
    application.add_handler(
        CallbackQueryHandler(unban_payment_action, pattern=r"^unban_stars:(?:buy|support)$"),
        group=group,
    )
    application.add_handler(PreCheckoutQueryHandler(precheckout_unban), group=group)
    application.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, complete_unban_payment), group=group
    )
    application.add_handler(
        CommandHandler("paysupport", payment_support, filters=filters.ChatType.PRIVATE), group=group
    )
