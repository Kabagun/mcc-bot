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
from .partner_rewards import resolve_store_matches
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


@dataclass(frozen=True)
class _BrandFactGroup:
    channels: tuple[str, ...]
    mcc: str
    note: str
    merchant_ids: tuple[int, ...]
    evidence_count: int

    @property
    def channel(self) -> str:
        return self.channels[0] if len(self.channels) == 1 else "both"


def _channel_label(channel: str, *, short: bool = False) -> str:
    if channel == "both":
        return "офлайн и онлайн" if short else "🏬🌐 Офлайн и онлайн"
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


def _brand_fact_groups(repository: StoreRepository, brand_id: int):
    getter = getattr(repository, "list_brand_mcc_groups", None)
    if getter is not None:
        return getter(brand_id)
    grouped = {}
    for channel in _CHANNELS:
        for fact in _brand_facts(repository, brand_id, channel):
            key = fact.mcc, getattr(fact, "note", "")
            grouped.setdefault(key, []).append(fact)
    return tuple(
        _BrandFactGroup(
            tuple(fact.channel for fact in facts),
            mcc,
            note,
            tuple(merchant_id for fact in facts for merchant_id in fact.merchant_ids),
            sum(fact.evidence_count for fact in facts),
        )
        for (mcc, note), facts in grouped.items()
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


def _channel_choice_label(channel: str) -> str:
    return "🌐 Онлайн" if channel == "online" else "🏬 Офлайн"


def _resolved_matches(context, brand_id: int, channel: str, mcc: str):
    catalog = context.application.bot_data["catalog"]
    partners = context.application.bot_data.get("partners")
    if not hasattr(catalog, "lookup"):
        return ()
    return (
        resolve_store_matches(catalog, partners, brand_id, channel, mcc)
        if partners is not None
        else catalog.lookup(mcc)
    )


def _resolved_results_match(left, right, mcc: str, descriptions) -> bool:
    """Compare the complete compact and detailed results users can open."""

    try:
        left_pages = format_match_pages(mcc, left, descriptions, html=True, max_length=3900)
        right_pages = format_match_pages(mcc, right, descriptions, html=True, max_length=3900)
    except ValueError:
        return False
    return left_pages == right_pages


def _brand_view(repository, brand, page, context, user_id, *, private=True):
    """Render a brand card as Telegram HTML with its inline keyboard."""

    admin = private and _is_admin(context, user_id)
    descriptions = context.application.bot_data["descriptions"]
    facts = tuple(_brand_fact_groups(repository, brand.id))
    count = max(1, (len(facts) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(max(0, page), count - 1)
    shown = facts[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]

    text = _header(brand)
    rows: list[list[InlineKeyboardButton]] = []
    for scope in ("both", "offline", "online"):
        scoped_facts = [fact for fact in shown if fact.channel == scope]
        if not scoped_facts:
            continue
        text += "\n\n<b>" + _channel_label(scope) + "</b>"
        for fact in scoped_facts:
            note = getattr(fact, "note", "")
            channel_notes = tuple(getattr(fact, "channel_notes", ()))
            if channel_notes and len({value for _channel, value in channel_notes}) > 1:
                rendered_notes = " · ".join(
                    f"{_channel_choice_label(channel)} {escape(value) if value else 'без подписи'}"
                    for channel, value in channel_notes
                )
                text += f"\n• MCC {fact.mcc} — {rendered_notes}"
            else:
                text += f"\n• MCC {fact.mcc}" + (f" — {escape(note)}" if note else "")
            if scope == "both":
                channel_matches = {
                    channel: _resolved_matches(context, brand.id, channel, fact.mcc)
                    for channel in fact.channels
                }
                first, second = fact.channels
                if _resolved_results_match(
                    channel_matches[first],
                    channel_matches[second],
                    fact.mcc,
                    descriptions,
                ):
                    rows.append(
                        [
                            InlineKeyboardButton(
                                _fact_label(fact, descriptions),
                                callback_data=_card_callback(brand.id, "both", fact.mcc, 0, False),
                            )
                        ]
                    )
                    continue
                for channel in fact.channels:
                    if (
                        not rows
                        or len(rows[-1]) != 1
                        or rows[-1][0].text
                        not in {
                            "🏬 Офлайн",
                            "🌐 Онлайн",
                        }
                    ):
                        rows.append([])
                    rows[-1].append(
                        InlineKeyboardButton(
                            _channel_choice_label(channel),
                            callback_data=_card_callback(brand.id, channel, fact.mcc, 0, False),
                        )
                    )
            else:
                rows.append(
                    [
                        InlineKeyboardButton(
                            _fact_label(fact, descriptions),
                            callback_data=_card_callback(brand.id, scope, fact.mcc, 0, False),
                        )
                    ]
                )
    if not facts:
        text += "\n\nНаблюдений по офлайн- и онлайн-оплате пока нет."
        partners = context.application.bot_data.get("partners")
        offers = partners.list_active_offers(brand.id) if partners is not None else ()
        if offers:
            cards = {card.id: card for card in context.application.bot_data["catalog"].cards}
            text += "\n\n<b>🎁 Партнёрская выгода</b>"
            for offer, tier in offers:
                card = cards.get(offer.card_id)
                card_name = card.name if card else offer.card_id
                reward = "баллами" if offer.reward_kind == "points" else "деньгами"
                mode = "дополнительно" if offer.mode == "additional" else "итоговая выгода"
                value = format(tier.value.normalize(), "f").replace(".", ",")
                text += f"\n• {escape(card_name)} — {value}% {reward} · {mode}"
                if offer.conditions:
                    text += f"\n  {escape(offer.conditions)}"
            text += "\n\n⚠️ MCC магазина пока не указан; перед оплатой проверьте его в банке."
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
    if private:
        rows.append(
            [
                InlineKeyboardButton(
                    "✏️ Редактировать магазин", callback_data=f"community:edit:{brand.id}"
                )
            ]
        )
    pending = _pending_for_brand(context, brand.id)
    if pending:
        rows.append(
            [
                InlineKeyboardButton(
                    f"⚠️ Неподтверждённые предложения · {len(pending)}",
                    callback_data=f"community:pending:{brand.id}",
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


def _effect_brand_ids(context, effect: dict) -> set[int]:
    """Resolve confirmed source and target stores touched by one pending effect."""

    payload = effect["payload"]
    result = {payload["brand_id"]} if isinstance(payload.get("brand_id"), int) else set()
    repository = context.application.bot_data["stores"]
    if isinstance(payload.get("merchant_id"), int):
        brand = _brand_for_merchant(repository, payload["merchant_id"])
        if brand:
            result.add(brand.id)
    if isinstance(payload.get("offer_id"), int):
        partners = context.application.bot_data.get("partners")
        offer = partners.get_offer(payload["offer_id"], include_archived=True) if partners else None
        if offer:
            result.add(offer.brand_id)
    return result


def _pending_for_brand(context, brand_id: int) -> tuple[dict, ...]:
    service = context.application.bot_data.get("community")
    if service is None:
        return ()
    return tuple(
        effect
        for effect in service.pending_effects()
        if brand_id in _effect_brand_ids(context, effect)
    )


def _pending_new_stores(context, query: str) -> tuple[dict, ...]:
    """Return pending-only new stores, newest proposal winning the normalized name."""

    service = context.application.bot_data.get("community")
    if service is None:
        return ()
    needle = normalize_store_name(query)
    by_name: dict[str, dict] = {}
    for effect in service.pending_effects():
        payload = effect["payload"]
        if effect["kind"] != "mcc_save" or not isinstance(payload.get("name"), str):
            continue
        key = normalize_store_name(payload["name"])
        if needle in key:
            by_name[key] = effect
    return tuple(by_name.values())


def pending_overlay(context, brand_id: int) -> tuple[str, int, tuple[int, ...]]:
    """Fold pending effects over confirmed data in creation order for public preview."""

    repository = context.application.bot_data["stores"]
    brand = _get_brand(repository, brand_id)
    if brand is None:
        raise ValueError("Магазин больше недоступен.")
    facts: dict[tuple[str, str], str] = {}
    for item in _brand_fact_groups(repository, brand_id):
        for channel in item.channels:
            note = dict(getattr(item, "channel_notes", ())).get(channel, item.note)
            facts[(channel, item.mcc)] = note
    partners = context.application.bot_data.get("partners")
    offers: dict[tuple[str, str], dict] = {}
    if partners is not None:
        for offer, tier in partners.list_active_offers(brand_id):
            offers[(offer.card_id, offer.channel)] = {
                "card_id": offer.card_id,
                "channel": offer.channel,
                "value": format(tier.value.normalize(), "f"),
                "conditions": offer.conditions,
            }
    name, aliases = brand.name, list(brand.aliases)
    relevant = _pending_for_brand(context, brand_id)
    for effect in relevant:
        kind, payload = effect["kind"], effect["payload"]
        source_ids = _effect_brand_ids(context, effect)
        target_id = payload.get("brand_id")
        if kind == "store_metadata" and target_id == brand_id:
            name, aliases = payload["name"], list(payload.get("aliases", []))
        elif kind == "mcc_delete" and brand_id in source_ids:
            source = repository.get(payload["merchant_id"])
            channels = payload.get("channels") or ([source.channel] if source else [])
            for channel in channels:
                facts.pop((channel, payload["mcc"]), None)
        elif kind == "mcc_save":
            if isinstance(payload.get("merchant_id"), int) and brand_id in source_ids:
                source = repository.get(payload["merchant_id"])
                channels = payload.get("channels") or ([source.channel] if source else [])
                for channel in channels:
                    facts.pop((channel, payload.get("old_mcc", payload["mcc"])), None)
            if target_id == brand_id:
                channels = (
                    ("offline", "online")
                    if payload["channel"] == "both"
                    else (payload["channel"],)
                )
                for channel in channels:
                    facts[(channel, payload["mcc"])] = payload.get("note", "")
        elif kind == "partner_delete" and brand_id in source_ids and partners is not None:
            offer = partners.get_offer(payload["offer_id"], include_archived=True)
            if offer:
                offers.pop((offer.card_id, offer.channel), None)
        elif kind == "partner_save":
            if isinstance(payload.get("offer_id"), int) and brand_id in source_ids and partners:
                old = partners.get_offer(payload["offer_id"], include_archived=True)
                if old:
                    offers.pop((old.card_id, old.channel), None)
            if target_id == brand_id:
                offers[(payload["card_id"], payload["channel"])] = payload
    lines = [f"🏪 <b>{escape(name)}</b>"]
    if aliases:
        lines.append("Другие названия: " + escape(", ".join(aliases)))
    for channel in ("offline", "online"):
        channel_facts = sorted(
            ((mcc, note) for (item_channel, mcc), note in facts.items() if item_channel == channel)
        )
        if channel_facts:
            lines.extend(("", f"<b>{_channel_label(channel)}</b>"))
            lines.extend(
                f"• MCC {mcc}" + (f" — {escape(note)}" if note else "")
                for mcc, note in channel_facts
            )
    if offers:
        cards = {card.id: card for card in context.application.bot_data["catalog"].cards}
        lines.extend(("", "<b>🎁 Партнёрская выгода</b>"))
        for payload in offers.values():
            card = cards.get(payload["card_id"])
            lines.append(
                f"• {escape(card.name if card else payload['card_id'])} — "
                f"{escape(str(payload['value']).replace('.', ','))}%"
            )
    if not facts:
        lines.extend(("", "⚠️ MCC магазина пока не указан."))
    lines.extend(("", "<i>Так будет выглядеть результат, если принять все предложения.</i>"))
    return "\n".join(lines), len(relevant), tuple(effect["id"] for effect in relevant)


def pending_new_overlay(context, proposal_id: int) -> tuple[str, tuple[int, ...]]:
    """Render a pending-only new store without exposing its author publicly."""

    effects = context.application.bot_data["community"].pending_effects()
    target = next((item for item in effects if item["id"] == proposal_id), None)
    if target is None or target["kind"] != "mcc_save" or "name" not in target["payload"]:
        raise ValueError("Предложение больше недоступно.")
    payload = target["payload"]
    channels = (
        ("offline", "online") if payload["channel"] == "both" else (payload["channel"],)
    )
    lines = [f"🏪 <b>{escape(payload['name'])}</b>"]
    for channel in channels:
        lines.extend(
            ("", f"<b>{_channel_label(channel)}</b>", f"• MCC {payload['mcc']}")
        )
    lines.extend(
        ("", "<i>Магазин появится в подтверждённых данных после проверки предложения.</i>")
    )
    return "\n".join(lines), (proposal_id,)


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
    pending_new = _pending_new_stores(context, query)
    private = bool(update.effective_chat and update.effective_chat.type == "private")
    if (
        not matches
        and not suggestions
        and not pending_new
        and private
        and context.application.bot_data.get("community")
    ):
        from .community_handlers import begin_contribution

        await begin_contribution(update, context, name=query)
        return
    if len(matches) == 1 and not pending_new:
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
        if pending_new:
            text += "\n\n⚠️ Есть неподтверждённые магазины:"
            pending_rows = [
                [
                    InlineKeyboardButton(
                        item["payload"]["name"],
                        callback_data=f"community:pending_new:{item['id']}",
                    )
                ]
                for item in pending_new
            ]
            keyboard = InlineKeyboardMarkup([*keyboard.inline_keyboard[:-1], *pending_rows])
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


def _parse_bounded_int(value: str, maximum: int = 10000) -> int | None:
    if not value.isascii() or not value.isdecimal() or len(value) > 12:
        return None
    result = int(value)
    return result if 0 <= result <= maximum else None


async def _edit_store_message(callback, text: str, keyboard) -> None:
    """Edit one store screen while tolerating expired Telegram messages."""

    try:
        await callback.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            LOGGER.info("Could not edit brand message")
    except (TelegramError, TypeError):
        LOGGER.info("Brand message is no longer accessible")


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
            or channel not in {*_CHANNELS, "both"}
            or len(mcc) != 4
            or not mcc.isascii()
            or not mcc.isdecimal()
            or page_index is None
            or raw_details not in {"0", "1"}
        ):
            return
        if channel == "both":
            fact = next(
                (
                    fact
                    for fact in _brand_fact_groups(repository, brand_id)
                    if fact.channel == "both" and fact.mcc == mcc
                ),
                None,
            )
            if fact is None:
                return
            first, second = fact.channels
            matches = _resolved_matches(context, brand_id, first, mcc)
            other_matches = _resolved_matches(context, brand_id, second, mcc)
            if not _resolved_results_match(
                matches,
                other_matches,
                mcc,
                context.application.bot_data["descriptions"],
            ):
                text = (
                    _header(brand) + "\n\nУсловия для офлайн- и онлайн-оплаты теперь различаются. "
                    "Выберите способ оплаты."
                )
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🏬 Офлайн",
                                callback_data=_card_callback(
                                    brand_id, first, mcc, 0, raw_details == "1"
                                ),
                            ),
                            InlineKeyboardButton(
                                "🌐 Онлайн",
                                callback_data=_card_callback(
                                    brand_id, second, mcc, 0, raw_details == "1"
                                ),
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "← К магазину", callback_data=f"store:show:{brand_id}:0"
                            )
                        ],
                    ]
                )
                await _edit_store_message(callback, text, keyboard)
                return
        else:
            facts = {fact.mcc: fact for fact in _brand_facts(repository, brand_id, channel)}
            if mcc not in facts:
                return
            matches = _resolved_matches(context, brand_id, channel, mcc)
        prefix = _header(brand, channel) + f"\n<i>{_WARNING}</i>\n\n"
        try:
            pages = format_match_pages(
                mcc,
                matches,
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
    await _edit_store_message(callback, text, keyboard)
