"""Merchant search and stateless, single-message MCC/card navigation."""

# Russian copy and ordinary Unicode buttons are intentional.
# ruff: noqa: RUF001

from __future__ import annotations

import logging
import re
import secrets
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from .formatting import format_match_pages
from .stores import Merchant, StoreRepository

LOGGER = logging.getLogger(__name__)
_PAGE_SIZE = 8
_CALLBACK = re.compile(
    r"store:(show|cards|search):([A-Za-z0-9]+)(?::([0-9]+))?(?::([0-9]+))?(?::([01]))?"
)
_WARNING = "MCC может отличаться у разных касс и способов оплаты."


def _header(merchant: Merchant) -> str:
    channel = "онлайн / приложение" if merchant.channel == "online" else "магазины / сеть"
    return f"🏪 <b>{escape(merchant.name)}</b> · {channel}"


def _actions(context, merchant_id, user_id, *, private=True):
    if not private:
        return []
    community = context.application.bot_data.get("community")
    admin = community is not None and user_id is not None and community.is_admin(user_id)
    action = "edit" if admin else "report"
    label = "✏️ Редактировать магазин" if admin else "✏️ Дополнить или исправить"
    return [[InlineKeyboardButton(label, callback_data=f"community:{action}:{merchant_id}")]]


def _card_callback(merchant_id, mcc, page, details):
    return f"store:cards:{merchant_id}:{mcc}:{page}:{int(details)}"


def _merchant_view(repository, merchant, page, context, user_id, *, private=True):
    facts = repository.list_mcc(merchant.id)
    descriptions = context.application.bot_data["descriptions"]
    count = max(1, (len(facts) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(page, count - 1)
    text = (
        _header(merchant)
        + "\n\n"
        + ("Выберите MCC, чтобы увидеть карты с манибэком." if facts else "MCC пока не добавлен.")
    )
    text += f"\n<i>{_WARNING}</i>"
    rows = [
        [
            InlineKeyboardButton(
                f"MCC {fact.mcc} — {descriptions.get(fact.mcc) or 'описание не найдено'}",
                callback_data=_card_callback(merchant.id, fact.mcc, 0, False),
            )
        ]
        for fact in facts[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]
    ]
    navigation = []
    if page:
        navigation.append(
            InlineKeyboardButton("←", callback_data=f"store:show:{merchant.id}:{page - 1}")
        )
    if count > 1:
        navigation.append(
            InlineKeyboardButton(
                f"{page + 1}/{count}", callback_data=f"store:show:{merchant.id}:{page}"
            )
        )
    if page + 1 < count:
        navigation.append(
            InlineKeyboardButton("→", callback_data=f"store:show:{merchant.id}:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.extend(_actions(context, merchant.id, user_id, private=private))
    return text, InlineKeyboardMarkup(rows)


def _search_view(repository, query, page, token):
    result = repository.search(query, limit=_PAGE_SIZE, offset=page * _PAGE_SIZE)
    merchants = result.matches or result.suggestions
    if result.matches:
        text = f"Магазины по запросу <b>{escape(query)}</b>. Выберите магазин:"
    elif merchants:
        text = f"Точных совпадений для <b>{escape(query)}</b> нет. Возможно, вы имели в виду:"
    else:
        text = f"Магазин <b>{escape(query)}</b> не найден. Можно добавить его вместе с MCC."
    rows = [
        [
            InlineKeyboardButton(
                f"{merchant.name} · {'онлайн' if merchant.channel == 'online' else 'магазины'}",
                callback_data=f"store:show:{merchant.id}:0",
            )
        ]
        for merchant in merchants
    ]
    navigation = []
    if page:
        navigation.append(
            InlineKeyboardButton("←", callback_data=f"store:search:{token}:{page - 1}")
        )
    if (page + 1) * _PAGE_SIZE < result.total:
        navigation.append(
            InlineKeyboardButton("→", callback_data=f"store:search:{token}:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("➕ Добавить магазин", callback_data="community:start")])
    return text, InlineKeyboardMarkup(rows)


async def search_stores(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query: str | None = None
) -> None:
    """Search a supplied merchant name or a normal text message."""

    message = update.effective_message
    if message is None:
        return
    if query is None:
        query = " ".join(context.args or ()) if context.args else message.text or ""
        if query.startswith("/"):
            query = ""
    query = query.strip()
    if not query or len(query) > 180:
        await message.reply_text("Укажите название магазина, например: Евроопт.")
        return
    repository: StoreRepository = context.application.bot_data["stores"]
    result = repository.search(query, limit=_PAGE_SIZE)
    if len(result.matches) == 1 and result.total == 1:
        text, keyboard = _merchant_view(
            repository,
            result.matches[0],
            0,
            context,
            update.effective_user.id if update.effective_user else None,
            private=bool(update.effective_chat and update.effective_chat.type == "private"),
        )
    else:
        token = secrets.token_hex(4)
        searches = context.user_data.setdefault("store_searches", {})
        if len(searches) >= 20:
            searches.pop(next(iter(searches)))
        searches[token] = query
        text, keyboard = _search_view(repository, query, 0, token)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def handle_store_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Edit the originating merchant message; invalid/expired callbacks are inert."""

    callback = update.callback_query
    if callback is None:
        return
    try:
        await callback.answer()
    except TelegramError:
        return
    if (
        not isinstance(callback.data, str)
        or len(callback.data) > 64
        or callback.message is None
        or not callback.message.is_accessible
    ):
        return
    match = _CALLBACK.fullmatch(callback.data)
    if match is None:
        return
    kind, identifier, raw_mcc_or_page, raw_page, raw_details = match.groups()
    repository: StoreRepository = context.application.bot_data["stores"]
    if kind == "search":
        if raw_page is not None or raw_details is not None or raw_mcc_or_page is None:
            return
        query = context.user_data.get("store_searches", {}).get(identifier)
        if query is None or int(raw_mcc_or_page) > 10000:
            return
        text, keyboard = _search_view(repository, query, int(raw_mcc_or_page), identifier)
    else:
        if not identifier.isdigit() or len(identifier) > 12:
            return
        merchant = repository.get(int(identifier))
        if merchant is None:
            text, keyboard = "Магазин изменён или архивирован. Повторите поиск по названию.", None
        elif kind == "show":
            if raw_page is not None or raw_details is not None or raw_mcc_or_page is None:
                return
            text, keyboard = _merchant_view(
                repository,
                merchant,
                int(raw_mcc_or_page),
                context,
                update.effective_user.id if update.effective_user else None,
                private=bool(update.effective_chat and update.effective_chat.type == "private"),
            )
        else:
            if (
                raw_mcc_or_page is None
                or len(raw_mcc_or_page) != 4
                or raw_page is None
                or raw_details is None
                or int(raw_page) > 10000
            ):
                return
            mcc, page_index, details = raw_mcc_or_page, int(raw_page), raw_details == "1"
            if mcc not in {fact.mcc for fact in repository.list_mcc(merchant.id)}:
                return
            catalog = context.application.bot_data["catalog"]
            prefix = _header(merchant) + f"\n<i>{_WARNING}</i>\n\n"
            try:
                pages = format_match_pages(
                    mcc,
                    catalog.lookup(mcc),
                    context.application.bot_data["descriptions"],
                    html=True,
                    max_length=3900 - len(prefix.encode("utf-16-le")) // 2,
                )
            except ValueError:
                text = prefix + "Не удалось показать карты: запись слишком длинная. Лимиты: /limits"
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "← MCC магазина", callback_data=f"store:show:{merchant.id}:0"
                            )
                        ]
                    ]
                )
            else:
                if page_index >= len(pages):
                    return
                text = prefix + (
                    pages[page_index].expanded if details else pages[page_index].compact
                )
                rows = [
                    [
                        InlineKeyboardButton(
                            "Скрыть подробности" if details else "🏦 Банки и минимальный платёж",
                            callback_data=_card_callback(merchant.id, mcc, page_index, not details),
                        )
                    ]
                ]
                navigation = []
                if page_index:
                    navigation.append(
                        InlineKeyboardButton(
                            "←",
                            callback_data=_card_callback(merchant.id, mcc, page_index - 1, details),
                        )
                    )
                if len(pages) > 1:
                    navigation.append(
                        InlineKeyboardButton(
                            f"{page_index + 1}/{len(pages)}",
                            callback_data=_card_callback(merchant.id, mcc, page_index, details),
                        )
                    )
                if page_index + 1 < len(pages):
                    navigation.append(
                        InlineKeyboardButton(
                            "→",
                            callback_data=_card_callback(merchant.id, mcc, page_index + 1, details),
                        )
                    )
                if navigation:
                    rows.append(navigation)
                rows.append(
                    [
                        InlineKeyboardButton(
                            "← MCC магазина", callback_data=f"store:show:{merchant.id}:0"
                        )
                    ]
                )
                keyboard = InlineKeyboardMarkup(rows)
    try:
        await callback.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            LOGGER.info("Could not edit merchant message")
    except (TelegramError, TypeError):
        LOGGER.info("Merchant message is no longer accessible")
