"""Brand search and stateless, single-message MCC/card navigation."""

# Russian copy and ordinary Unicode buttons are intentional.
# ruff: noqa: RUF001

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from .formatting import format_match_pages
from .stores import StoreRepository, normalize_store_name

LOGGER = logging.getLogger(__name__)
_PAGE_SIZE = 8
_WARNING = "MCC может отличаться у разных касс и способов оплаты."
_CHANNELS = ("offline", "online")


@dataclass(frozen=True)
class _LegacyBrandFact:
    channel: str
    mcc: str
    note: str
    merchant_ids: tuple[int, ...]
    evidence_count: int


def _channel_label(channel: str, *, short: bool = False) -> str:
    if channel == "online":
        return "онлайн" if short else "🌐 Онлайн / приложение"
    return "офлайн" if short else "🏬 Офлайн / магазины"


def _get_brand(repository: StoreRepository, brand_id: int, *, include_archived: bool = False):
    getter = getattr(repository, "get_brand", None)
    if getter is not None:
        return getter(brand_id, include_archived=include_archived)
    return repository.get(brand_id, include_archived=include_archived)


def _brand_for_merchant(repository: StoreRepository, merchant_id: int):
    resolver = getattr(repository, "brand_for_merchant", None)
    if resolver is not None:
        return resolver(merchant_id)
    return repository.get(merchant_id)


def _brand_facts(repository: StoreRepository, brand_id: int, channel: str):
    getter = getattr(repository, "list_brand_mcc", None)
    if getter is not None:
        return getter(brand_id, channel=channel)
    merchant = repository.get(brand_id)
    if merchant is None or merchant.channel != channel:
        return ()
    return tuple(
        _LegacyBrandFact(
            channel,
            fact.mcc,
            getattr(fact, "note", ""),
            (merchant.id,),
            fact.evidence_count,
        )
        for fact in repository.list_mcc(merchant.id)
    )


def _header(brand, channel: str | None = None) -> str:
    result = f"🏪 <b>{escape(brand.name)}</b>"
    return result if channel is None else result + " · " + _channel_label(channel, short=True)


def _is_admin(context, user_id: int | None) -> bool:
    community = context.application.bot_data.get("community")
    return community is not None and user_id is not None and community.is_admin(user_id)


def _card_callback(brand_id: int, channel: str, mcc: str, page: int, details: bool) -> str:
    return f"store:cards:{brand_id}:{channel}:{mcc}:{page}:{int(details)}"


def _fact_label(fact, descriptions) -> str:
    note = getattr(fact, "note", "")
    if note:
        return f"MCC {fact.mcc} · {note}"
    description = descriptions.get(fact.mcc) or "описание не найдено"
    return f"MCC {fact.mcc} — {description}"


def _brand_view(repository, brand, page, context, user_id, *, private=True):
    """Render a brand card as Telegram HTML with its inline keyboard."""

    admin = private and _is_admin(context, user_id)
    descriptions = context.application.bot_data["descriptions"]
    grouped = [
        (channel, facts)
        for channel in _CHANNELS
        if (facts := tuple(_brand_facts(repository, brand.id, channel)))
    ]
    observed = [channel for channel, _facts in grouped]
    flat = [(channel, fact) for channel, facts in grouped for fact in facts]
    count = max(1, (len(flat) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(0, page), count - 1)
    shown = flat[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]
    shown_channels = {channel for channel, _ in shown}
    if not flat:
        shown_channels = set(observed)

    text = _header(brand)
    rows: list[list[InlineKeyboardButton]] = []
    for channel in observed:
        if channel not in shown_channels:
            continue
        facts = [fact for fact_channel, fact in shown if fact_channel == channel]
        text += "\n\n<b>" + _channel_label(channel) + "</b>"
        if facts:
            for fact in facts:
                note = getattr(fact, "note", "")
                text += f"\n• MCC {fact.mcc}" + (f" — {escape(note)}" if note else "")
                rows.append(
                    [
                        InlineKeyboardButton(
                            _fact_label(fact, descriptions),
                            callback_data=_card_callback(brand.id, channel, fact.mcc, 0, False),
                        )
                    ]
                )
    if not observed:
        text += "\n\nНаблюдений по офлайн- и онлайн-оплате пока нет."
    text += f"\n\n<i>{_WARNING}</i>"
    if private:
        rows.append(
            [
                InlineKeyboardButton(
                    "➕ Добавить MCC" if admin else "➕ Предложить MCC",
                    callback_data=f"community:start:{brand.id}",
                )
            ]
        )

    navigation = []
    if page:
        navigation.append(
            InlineKeyboardButton("←", callback_data=f"store:show:{brand.id}:{page - 1}")
        )
    if count > 1:
        navigation.append(
            InlineKeyboardButton(
                f"{page + 1}/{count}", callback_data=f"store:show:{brand.id}:{page}"
            )
        )
    if page + 1 < count:
        navigation.append(
            InlineKeyboardButton("→", callback_data=f"store:show:{brand.id}:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    if admin:
        rows.append(
            [
                InlineKeyboardButton(
                    "✏️ Редактировать магазин", callback_data=f"community:edit:{brand.id}"
                )
            ]
        )
    return text, InlineKeyboardMarkup(rows)


def _search_entities(repository: StoreRepository, query: str):
    result = repository.search(query, limit=100)
    matches = tuple(result.matches)
    exact = tuple(
        item
        for item in matches
        if normalize_store_name(query)
        in {normalize_store_name(value) for value in (item.name, *item.aliases)}
    )
    # Compatibility with repositories that used to mix exact and partial rows.
    return (exact or matches), tuple(result.suggestions)


def _search_view(repository, query, page, token):
    matches, suggestions = _search_entities(repository, query)
    entities = matches or suggestions
    total = len(entities)
    page = min(max(0, page), max(0, (total - 1) // _PAGE_SIZE))
    shown = entities[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]
    if matches:
        text = f"Магазины по запросу <b>{escape(query)}</b>. Выберите магазин:"
    elif suggestions:
        text = f"Точных совпадений для <b>{escape(query)}</b> нет. Возможно, вы имели в виду:"
    else:
        text = f"Магазин <b>{escape(query)}</b> не найден. Можно добавить его вместе с MCC."
    rows = [
        [InlineKeyboardButton(entity.name, callback_data=f"store:show:{entity.id}:0")]
        for entity in shown
    ]
    navigation = []
    if page:
        navigation.append(
            InlineKeyboardButton("←", callback_data=f"store:search:{token}:{page - 1}")
        )
    if (page + 1) * _PAGE_SIZE < total:
        navigation.append(
            InlineKeyboardButton("→", callback_data=f"store:search:{token}:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.append(
        [
            InlineKeyboardButton(
                "➕ Добавить новый магазин", callback_data=f"community:start:0:{token}"
            )
        ]
    )
    return text, InlineKeyboardMarkup(rows)


async def search_stores(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query: str | None = None
) -> None:
    """Search a supplied brand name or start a prefilled new-brand contribution."""

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
    matches, suggestions = _search_entities(repository, query)
    private = bool(update.effective_chat and update.effective_chat.type == "private")
    if (
        not matches
        and not suggestions
        and private
        and context.application.bot_data.get("community")
    ):
        from .community_handlers import begin_contribution

        await begin_contribution(update, context, name=query)
        return
    if len(matches) == 1:
        text, keyboard = _brand_view(
            repository,
            matches[0],
            0,
            context,
            update.effective_user.id if update.effective_user else None,
            private=private,
        )
    else:
        token = secrets.token_hex(4)
        searches = context.user_data.setdefault("store_searches", {})
        if len(searches) >= 20:
            searches.pop(next(iter(searches)))
        searches[token] = query
        text, keyboard = _search_view(repository, query, 0, token)
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


def _parse_bounded_int(value: str, maximum: int = 10000) -> int | None:
    if not value.isascii() or not value.isdecimal() or len(value) > 12:
        return None
    result = int(value)
    return result if 0 <= result <= maximum else None


async def handle_store_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Edit the originating brand message; invalid and expired callbacks are inert."""

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
    parts = callback.data.split(":")
    if len(parts) < 2 or parts[0] != "store":
        return
    repository: StoreRepository = context.application.bot_data["stores"]
    kind = parts[1]
    if kind == "search" and len(parts) == 4:
        page = _parse_bounded_int(parts[3])
        query = context.user_data.get("store_searches", {}).get(parts[2])
        if query is None or page is None:
            return
        text, keyboard = _search_view(repository, query, page, parts[2])
    elif kind == "show" and len(parts) == 4:
        brand_id, page = _parse_bounded_int(parts[2], 10**12), _parse_bounded_int(parts[3])
        if brand_id is None or not brand_id or page is None:
            return
        brand = _get_brand(repository, brand_id)
        if brand is None:
            text, keyboard = "Магазин изменён или архивирован. Повторите поиск по названию.", None
        else:
            text, keyboard = _brand_view(
                repository,
                brand,
                page,
                context,
                update.effective_user.id if update.effective_user else None,
                private=bool(update.effective_chat and update.effective_chat.type == "private"),
            )
    elif kind == "cards" and len(parts) in {6, 7}:
        if len(parts) == 7:
            brand_id = _parse_bounded_int(parts[2], 10**12)
            channel, mcc, raw_page, raw_details = parts[3:]
            brand = _get_brand(repository, brand_id) if brand_id else None
        else:
            merchant_id = _parse_bounded_int(parts[2], 10**12)
            merchant = repository.get(merchant_id) if merchant_id else None
            brand = _brand_for_merchant(repository, merchant_id) if merchant_id else None
            brand_id = brand.id if brand else None
            channel = merchant.channel if merchant else ""
            mcc, raw_page, raw_details = parts[3:]
        page_index = _parse_bounded_int(raw_page)
        if (
            brand is None
            or brand_id is None
            or channel not in _CHANNELS
            or len(mcc) != 4
            or not mcc.isascii()
            or not mcc.isdecimal()
            or page_index is None
            or raw_details not in {"0", "1"}
        ):
            return
        facts = {fact.mcc: fact for fact in _brand_facts(repository, brand_id, channel)}
        if mcc not in facts:
            return
        catalog = context.application.bot_data["catalog"]
        prefix = _header(brand, channel) + f"\n<i>{_WARNING}</i>\n\n"
        try:
            pages = format_match_pages(
                mcc,
                catalog.lookup(mcc),
                context.application.bot_data["descriptions"],
                html=True,
                max_length=3900 - len(prefix.encode("utf-16-le")) // 2,
            )
        except ValueError:
            text = (
                prefix + "Не удалось показать карты: запись слишком длинная. "
                "Откройте информацию по картам через /start."
            )
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("← К магазину", callback_data=f"store:show:{brand_id}:0")]]
            )
        else:
            if page_index >= len(pages):
                return
            details = raw_details == "1"
            text = prefix + (pages[page_index].expanded if details else pages[page_index].compact)
            rows = [
                [
                    InlineKeyboardButton(
                        "Скрыть подробности" if details else "🏦 Банки и минимальный платёж",
                        callback_data=_card_callback(
                            brand_id, channel, mcc, page_index, not details
                        ),
                    )
                ]
            ]
            navigation = []
            if page_index:
                navigation.append(
                    InlineKeyboardButton(
                        "←",
                        callback_data=_card_callback(
                            brand_id, channel, mcc, page_index - 1, details
                        ),
                    )
                )
            if len(pages) > 1:
                navigation.append(
                    InlineKeyboardButton(
                        f"{page_index + 1}/{len(pages)}",
                        callback_data=_card_callback(brand_id, channel, mcc, page_index, details),
                    )
                )
            if page_index + 1 < len(pages):
                navigation.append(
                    InlineKeyboardButton(
                        "→",
                        callback_data=_card_callback(
                            brand_id, channel, mcc, page_index + 1, details
                        ),
                    )
                )
            if navigation:
                rows.append(navigation)
            rows.append(
                [InlineKeyboardButton("← К магазину", callback_data=f"store:show:{brand_id}:0")]
            )
            keyboard = InlineKeyboardMarkup(rows)
    else:
        return
    try:
        await callback.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            LOGGER.info("Could not edit brand message")
    except (TelegramError, TypeError):
        LOGGER.info("Brand message is no longer accessible")
