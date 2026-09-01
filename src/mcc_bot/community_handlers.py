"""Private Telegram contribution, moderation and management conversations."""

# Russian UI text and ordinary Unicode menu emojis are intentional.
# ruff: noqa: RUF001

from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .community import (
    MAX_LOCATION,
    MAX_MEDIA_BYTES,
    MAX_NAME,
    MAX_NOTE,
    CommunityError,
    CommunityService,
    Draft,
    Proposal,
    StaleAction,
    clean_text,
)
from .formatting import format_limits, split_message
from .stores import BrandMccGroup, normalize_store_name

LOGGER = logging.getLogger(__name__)
INFO = "ℹ️ Информация по картам"
GUIDE = "❓ Как пользоваться"
SUGGEST = "➕ Предложить данные"
ADD = "➕ Добавить данные"
VOLUNTEER = "🙋 Хочу помогать"
APPLICATION_PENDING = "⏳ Заявка отправлена"
LEGACY_MINE = "👤 Мои предложения"
QUEUE = "📋 Разобрать очередь"
MANAGE = "⚙️ Управление"
MANAGE_HISTORY = "📜 История изменений"
MANAGE_DIGEST_ON = "🔕 Включить сводку в 20:00"
MANAGE_DIGEST_OFF = "🔔 Отключить сводку"
MANAGE_ROLES = "👥 Помощники и заявки"
MAIN_MENU = "⬅️ Главное меню"
CANCEL_DRAFT = "❌ Отменить"
OPTIONAL_FIELDS = "Необязательные поля"
HIDE_OPTIONAL_FIELDS = "Скрыть необязательные поля"
HISTORY_PAGE_SIZE = 10
FACT_PAGE_SIZE = 10
MAX_HISTORY_OFFSET = 1000000
KINDS = {
    "add_merchant": "Добавить магазин, способ оплаты и MCC",
    "add_mcc": "Добавить MCC",
    "add_mcc_both": "Добавить MCC для офлайн- и онлайн-оплаты",
    "replace_mcc": "Заменить ошибочный MCC",
    "rename_merchant": "Исправить название",
    "merge_merchant": "Объединить дубль",
    "aliases": "Изменить другие названия",
    "archive_merchant": "Убрать магазин из поиска",
    "archive_mcc": "Убрать ошибочный MCC",
    "revert": "Отменить изменение",
    "rename_brand": "Исправить название магазина",
    "brand_aliases": "Изменить названия магазина",
    "edit_brand_names": "Изменить названия магазина",
    "set_brand_membership": "Изменить способ оплаты",
    "merge_brand": "Объединить магазины",
    "edit_mcc_note": "Изменить подпись к MCC",
    "card_partnership": "Добавить партнёрство по карте",
}
HISTORY_KINDS = {**KINDS, "import": "Импорт данных из tannei.by"}
CLEAN_NAVIGATION_STAGES = {
    "report",
    "editor",
    "brand_editor",
    "brand_names",
    "alias_actions",
    "mcc_facts",
    "mcc_fact",
    "history",
    "form_menu",
}
MCC_BUTTON_ONLY_STAGES = CLEAN_NAVIGATION_STAGES | {
    "channel",
    "note_choice",
    "evidence",
    "response_evidence",
    "preview_more",
    "preview",
    "cancel_confirm",
    "review_preview",
}


class _ButtonOnlyInput(CommunityError):
    """Text was sent while the durable draft expects an inline-button choice."""


def _service(context: ContextTypes.DEFAULT_TYPE) -> CommunityService:
    return context.application.bot_data["community"]


def _brand(service: CommunityService, brand_id: int, *, include_archived: bool = False):
    getter = getattr(service.stores, "get_brand", None)
    if getter is not None:
        return getter(brand_id, include_archived=include_archived)
    return service.stores.get(brand_id, include_archived=include_archived)


def _brand_for_merchant(
    service: CommunityService, merchant_id: int, *, include_archived: bool = False
):
    resolver = getattr(service.stores, "brand_for_merchant", None)
    if resolver is not None:
        return resolver(merchant_id, include_archived=include_archived)
    return service.stores.get(merchant_id, include_archived=include_archived)


def _brand_members(
    service: CommunityService,
    brand_id: int,
    *,
    channel: str | None = None,
    include_archived: bool = False,
):
    getter = getattr(service.stores, "list_brand_members", None)
    if getter is not None:
        return getter(brand_id, channel=channel, include_archived=include_archived)
    merchant = service.stores.get(brand_id, include_archived=include_archived)
    if merchant is None or (channel is not None and merchant.channel != channel):
        return ()
    return (merchant,)


def _brand_channels(service: CommunityService, brand_id: int) -> dict[str, tuple[Any, ...]]:
    getter = getattr(service.stores, "list_brand_channels", None)
    if getter is not None:
        return {key: tuple(value) for key, value in getter(brand_id).items() if value}
    members = _brand_members(service, brand_id)
    return {members[0].channel: tuple(members)} if members else {}


def _brand_facts(service: CommunityService, brand_id: int, channel: str):
    getter = getattr(service.stores, "list_brand_mcc", None)
    if getter is not None:
        return getter(brand_id, channel=channel)
    members = _brand_members(service, brand_id, channel=channel)
    return service.stores.list_mcc(members[0].id) if members else ()


def _brand_fact_groups(service: CommunityService, brand_id: int):
    getter = getattr(service.stores, "list_brand_mcc_groups", None)
    if getter is not None:
        return getter(brand_id)
    result = []
    for channel in ("offline", "online"):
        for fact in _brand_facts(service, brand_id, channel):
            merchant_ids = getattr(fact, "merchant_ids", None)
            result.append(
                BrandMccGroup(
                    (channel,),
                    fact.mcc,
                    getattr(fact, "note", ""),
                    tuple(merchant_ids or (fact.merchant_id,)),
                    fact.evidence_count,
                )
            )
    return tuple(result)


def _member_for_channel(service: CommunityService, brand_id: int, channel: str):
    members = _brand_members(service, brand_id, channel=channel)
    return members[0] if members else None


def _tannei_snapshot(service: CommunityService, brand_id: int) -> dict[str, Any] | None:
    getter = getattr(service.stores, "tannei_snapshot", None)
    return getter(brand_id) if getter is not None else None


def _identity(update: Update) -> int | None:
    user, chat = update.effective_user, update.effective_chat
    if user is None or chat is None or chat.type != "private" or user.id <= 0:
        return None
    return user.id


def _keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data="community:" + data) for label, data in row]
            for row in rows
        ]
    )


def _role_identity(candidate: dict[str, Any], *, compact: bool = False) -> str:
    username = f"@{candidate['username']}" if candidate.get("username") else "без @username"
    full_name = " ".join(
        value for value in (candidate.get("first_name"), candidate.get("last_name")) if value
    )
    if compact:
        identity = username if not full_name else f"{username} · {full_name}"
        return identity if len(identity) <= 48 else identity[:47] + "…"
    return (
        f"{username}\nИмя в Telegram: {full_name or 'не указано'}\n"
        f"Telegram ID: {candidate['user_id']}"
    )


def _audit_identity(actor: dict[str, Any]) -> str:
    if actor.get("automated"):
        return "tannei.by · автоматический импорт"
    username = f"@{actor['username']}" if actor.get("username") else None
    full_name = " ".join(
        value for value in (actor.get("first_name"), actor.get("last_name")) if value
    )
    identity = " · ".join(value for value in (username, full_name) if value)
    return f"{identity + ' · ' if identity else ''}Telegram ID {actor['user_id']}"


def keyboard_for(service: CommunityService, user_id: int) -> ReplyKeyboardMarkup:
    """Build a persistent keyboard from the user's current effective role."""

    if service.is_admin(user_id):
        rows = [[INFO], [ADD], [QUEUE, MANAGE], [GUIDE]]
    else:
        application = (
            APPLICATION_PENDING if service.role_request_status(user_id) == "pending" else VOLUNTEER
        )
        rows = [[INFO], [SUGGEST], [application], [GUIDE]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def draft_keyboard_for() -> ReplyKeyboardMarkup:
    """Build the only lower keyboard used while a real form draft is active."""

    return ReplyKeyboardMarkup([[CANCEL_DRAFT]], resize_keyboard=True, is_persistent=True)


def _current_reply_keyboard(service: CommunityService, user_id: int) -> ReplyKeyboardMarkup:
    """Keep a real form in cancel-only mode after recoverable validation errors."""

    try:
        draft = service.draft(user_id)
    except CommunityError:
        draft = None
    return (
        draft_keyboard_for()
        if draft is not None and draft.data.get("draft_mode")
        else keyboard_for(service, user_id)
    )


def management_keyboard_for(service: CommunityService, user_id: int) -> InlineKeyboardMarkup:
    """Build role-aware inline management actions without replacing the main menu."""

    if not service.is_admin(user_id):
        raise CommunityError("Управление доступно только действующим помощникам.")
    epoch = service.role_epoch(user_id)
    rows: list[list[tuple[str, str]]] = [[(MANAGE_HISTORY, "recent:0")]]
    rows.append(
        [
            (
                MANAGE_DIGEST_OFF if service.digest_enabled(user_id) else MANAGE_DIGEST_ON,
                f"digest:{int(not service.digest_enabled(user_id))}:{epoch}",
            )
        ]
    )
    if service.role(user_id) == "owner":
        rows.append([(MANAGE_ROLES, "roles:0")])
    return _keyboard(rows)


async def _say(
    update: Update, text: str, markup: Any = None, *, parse_mode: str | None = None
) -> None:
    if update.effective_message is not None:
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            kwargs = {"reply_markup": markup if index == len(chunks) - 1 else None}
            if parse_mode is not None:
                kwargs["parse_mode"] = parse_mode
            await update.effective_message.reply_text(chunk, **kwargs)


async def _say_inline(
    update: Update,
    text: str,
    markup: InlineKeyboardMarkup,
    *,
    parse_mode: str | None = None,
) -> None:
    """Edit the current callback message when Telegram permits a compact inline view."""

    query = update.callback_query
    if (
        query is not None
        and len(split_message(text)) == 1
        and hasattr(query, "edit_message_text")
        and getattr(query, "message", None) is not None
        and getattr(query.message, "is_accessible", True)
    ):
        try:
            kwargs = {"reply_markup": markup}
            if parse_mode is not None:
                kwargs["parse_mode"] = parse_mode
            await query.edit_message_text(text, **kwargs)
            return
        except TelegramError:
            LOGGER.info("Could not edit contribution message; sending the current view")
    await _say(update, text, markup, parse_mode=parse_mode)


async def _say_with_restored_menu(
    update: Update,
    text: str,
    menu: ReplyKeyboardMarkup,
    inline: InlineKeyboardMarkup | None = None,
) -> None:
    """Restore the persistent menu and keep one visible result message.

    Telegram cannot attach a reply keyboard and inline buttons to one send. The
    initial send restores the persistent keyboard; editing that same bot message
    then attaches the object action without creating a second visible result.
    """

    message = update.effective_message
    if message is None:
        return
    sent = await message.reply_text(text, reply_markup=menu)
    if inline is None:
        return
    try:
        await sent.edit_text(text, reply_markup=inline)
    except (TelegramError, AttributeError):
        LOGGER.info("Could not attach the saved-store action to the completion message")
        await message.reply_text("Открыть магазин:", reply_markup=inline)


async def _close_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    draft: Draft,
) -> None:
    """Discard a draft and restore its brand card when it has a live origin."""

    if draft.data.get("response_id"):
        proposal = service.cancel_response(draft.user_id, draft.id, draft.version)
        await _say(
            update,
            f"Ответ отменён. Заявка №{proposal.id} возвращена на проверку.",
            keyboard_for(service, draft.user_id),
        )
        return
    brand_id = draft.data.get("brand_id")
    service.cancel_draft(draft.user_id, draft.id, draft.version)
    if isinstance(brand_id, int):
        if draft.data.get("draft_mode"):
            await _say(update, "Действие отменено.", keyboard_for(service, draft.user_id))
        from .store_handlers import _brand_view

        brand = _brand(service, brand_id)
        if brand is not None:
            text, markup = _brand_view(
                service.stores, brand, 0, context, draft.user_id, private=True
            )
            await _say_inline(update, text, markup, parse_mode=ParseMode.HTML)
            return
    await _say(update, "Действие отменено.", keyboard_for(service, draft.user_id))


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a private role-aware start menu without clearing a durable draft."""

    user_id = _identity(update)
    if user_id is None:
        await _say(update, "Предложения и управление доступны в личном чате с ботом.")
        return
    service = _service(context)
    draft = service.draft(user_id)
    if draft:
        actions = [("Продолжить", f"d:{draft.id}:{draft.version}:resume")]
        if not draft.data.get("draft_mode"):
            actions.append(("Отменить", f"d:{draft.id}:{draft.version}:cancel"))
        await _say(
            update,
            "У вас есть незавершённое действие.",
            _keyboard([actions]),
        )
    await _say(
        update,
        "Отправьте четырёхзначный MCC или название магазина.\n"
        "Например: 5411 или Евроопт.\n"
        "Данные магазинов пополняют пользователи; проверяйте MCC перед покупкой.\n\n"
        f"👥 Пользователей: "
        f"{context.application.bot_data['user_registry'].private_chat_count()} · "
        f"🤝 Помощников: {service.helper_count()}",
        (
            draft_keyboard_for()
            if draft and draft.data.get("draft_mode")
            else keyboard_for(service, user_id)
        ),
    )


def _guide_text(service: CommunityService, user_id: int) -> str:
    """Return the complete role-aware guide in user-facing language."""

    if service.is_admin(user_id):
        return (
            "Как пользоваться ботом и помогать\n\n"
            "Поиск и выгода\n"
            "• Отправьте название магазина или четырёхзначный MCC из операции банка.\n"
            "• Офлайн и онлайн показаны отдельно: у кассы и на сайте одного магазина MCC "
            "могут различаться.\n"
            "• Бот складывает совместимые части выгоды. Например, «3% + 5% баллами» "
            "означает обычное начисление карты и дополнительные баллы; условия, минимальную сумму "
            "и лимиты смотрите в подробностях. Перед оплатой сверяйте MCC.\n\n"
            "Добавление и исправление данных\n"
            "• Нажмите «➕ Добавить данные» и выберите: новый магазин, MCC магазина или "
            "партнёрство по карте. Из карточки магазина можно сразу открыть нужное изменение.\n"
            "• Помощник сохраняет проверенные данные сразу. Для партнёрства укажите условия, "
            "исключения и официальный источник; если ссылки нет, приложите скриншот.\n"
            "• Пока форма открыта, нижняя кнопка «❌ Отменить» закрывает её; после сохранения "
            "обычное меню возвращается.\n\n"
            "Очередь и проверка\n"
            "• Откройте «📋 Разобрать очередь» и выберите заявку. Она резервируется за вами "
            "ровно на 15 минут; продлить резерв нельзя.\n"
            "• Сверьте магазин, офлайн/онлайн, MCC либо карту, партнёра, условия, исключения "
            "и источник. «Принять» сохраняет данные, «Отклонить» сразу закрывает заявку, "
            "«Уточнить» отправляет вопрос автору.\n"
            "• «Отменить разбор» или истечение 15 минут возвращает заявку в общую очередь. "
            "После ответа на уточнение она снова появится в очереди.\n\n"
            "Управление\n"
            "• «⚙️ Управление» открывает inline-действия: историю изменений и отмену доступных "
            "записей, вечернюю сводку, а у владельца — помощников и заявки.\n"
            "• История, списки и страницы переключаются кнопками под текущим сообщением."
        )
    return (
        "Как пользоваться ботом\n\n"
        "Поиск и выгода\n"
        "• Напишите название магазина, например «Евроопт», или отправьте четыре цифры MCC "
        "из операции в приложении банка, например 5411. Если вариантов несколько, выберите "
        "нужный кнопкой.\n"
        "• Офлайн и онлайн показаны отдельно: у кассы и на сайте одного магазина MCC могут "
        "различаться. Перед оплатой сверяйте MCC в приложении банка.\n"
        "• Бот складывает совместимые части выгоды. Например, «3% + 5% баллами» означает "
        "обычное начисление карты и дополнительные баллы. Откройте подробности, чтобы увидеть "
        "условия, "
        "минимальную сумму и лимиты.\n\n"
        "Предложить данные\n"
        "• Нажмите «➕ Предложить данные» и выберите: новый магазин, MCC магазина или "
        "партнёрство по карте. Для MCC укажите, относится ли он к офлайн- или онлайн-оплате.\n"
        "• Для партнёрства укажите карту, партнёра, выгоду, условия и исключения. Нужна "
        "официальная ссылка или скриншот источника. В остальных формах ссылка или скриншот "
        "также помогают помощнику проверить сведения.\n"
        "• Проверьте заполненные данные и отправьте их. Дополнительного подтверждения нет: "
        "предложение сразу попадёт помощникам на проверку.\n"
        "• Если помощник попросит уточнение, бот пришлёт вопрос. Ответьте в открывшейся форме; "
        "после ответа предложение вернётся на проверку.\n"
        "• Пока форма открыта, нижняя кнопка «❌ Отменить» закрывает её; затем обычное меню "
        "возвращается."
    )


def _draft_buttons(
    draft: Draft, rows: list[list[tuple[str, str]]] | None = None
) -> InlineKeyboardMarkup:
    prefix = f"d:{draft.id}:{draft.version}:"
    result = [[(label, prefix + action) for label, action in row] for row in (rows or [])]
    if draft.stage == "history_entry" and "recent_offset" in draft.data:
        return _keyboard(result)
    if draft.stage in {"cancel_confirm", "editor"} or draft.data.get("draft_mode"):
        return _keyboard(result)
    navigation = []
    if draft.data.get("back"):
        navigation.append(("⬅️ Назад", prefix + "back"))
    elif draft.data.get("brand_id"):
        navigation.append(("⬅️ К магазину", prefix + "close"))
    if not draft.data.get("draft_mode"):
        navigation.append(("Отмена", prefix + "cancel"))
    if navigation:
        result.append(navigation)
    return _keyboard(result)


def _display_payload(service: CommunityService, kind: str, payload: dict[str, Any]) -> str:
    if kind == "card_partnership":
        brand = _brand(service, payload["brand_id"], include_archived=True)
        channel = {
            "offline": "офлайн",
            "online": "онлайн",
            "any": "офлайн и онлайн",
        }[payload["channel"]]
        reward = "денежный возврат" if payload["reward_kind"] == "cash" else "баллы"
        mode = (
            "дополнительно к обычной выгоде"
            if payload["mode"] == "additional"
            else "итоговая выгода"
        )
        value = payload["tiers"][0]["value"]
        excluded = (
            ", ".join(item["mcc"] for item in payload["exclusions"] if item.get("mcc")) or "нет"
        )
        return "\n".join(
            [
                KINDS[kind],
                f"Магазин: {brand.name if brand else 'не найден'} (№{payload['brand_id']})",
                f"Карта: {payload['card_id']}",
                f"Оплата: {channel}",
                f"Выгода: {value}% · {reward} · {mode}",
                f"Условия: {payload['conditions']}",
                f"Исключённые MCC: {excluded}",
                "Официальная ссылка: " + (payload["source_url"] or "не указана, приложен скриншот"),
            ]
        )
    parts = [KINDS.get(kind, "Предложение")]
    brand_id = payload.get("brand_id")
    if brand_id:
        brand = _brand(service, brand_id, include_archived=True)
        parts.append(f"Магазин: {brand.name if brand else 'не найден'} (№{brand_id})")
    merchant_id = payload.get("merchant_id")
    if merchant_id:
        merchant = service.stores.get(merchant_id, include_archived=True)
        brand = _brand_for_merchant(service, merchant_id) if merchant else None
        parts.append(f"Магазин: {brand.name if brand else 'не найден'}")
        if merchant:
            parts.append(
                "Способ оплаты: " + ("онлайн" if merchant.channel == "online" else "офлайн")
            )
    if "name" in payload:
        parts.append("Название: " + payload["name"])
    if "location" in payload:
        parts.append("Где находится: " + (payload["location"] or "не указано"))
    if "channel" in payload:
        parts.append("Способ оплаты: " + _channel_label(payload["channel"]))
    if "old_mcc" in payload:
        parts.append("Старый MCC: " + payload["old_mcc"])
    if "mcc" in payload:
        parts.append("MCC: " + payload["mcc"])
    if "note" in payload:
        label = "Подпись для новых фактов" if kind == "add_mcc_both" else "Подпись к MCC"
        parts.append(label + ": " + (payload["note"] or "не указана"))
    if "target_id" in payload:
        target = (
            _brand(service, payload["target_id"], include_archived=True)
            if kind == "merge_brand"
            else service.stores.get(payload["target_id"], include_archived=True)
        )
        parts.append(
            f"Оставить: {target.name if target else 'не найден'} (№{payload['target_id']})"
        )
    if "aliases" in payload:
        parts.append("Другие названия: " + (", ".join(payload["aliases"]) or "нет"))
    if "audit_id" in payload:
        parts.append(f"Изменение №{payload['audit_id']}. Независимые подтверждения сохранятся.")
    if kind == "add_mcc_both":
        parts.append("Способ оплаты: 🏬🌐 Офлайн и онлайн")
        parts.append("Существующие подписи останутся без изменений.")
    if payload.get("source_url"):
        parts.append("Официальная ссылка: " + payload["source_url"])
    return "\n".join(parts)


def _channel_label(channel: str) -> str:
    if channel == "both":
        return "офлайн и онлайн"
    return "онлайн" if channel == "online" else "офлайн"


def _channel_title(channel: str) -> str:
    if channel == "both":
        return "🏬🌐 Офлайн и онлайн"
    return "🌐 Онлайн" if channel == "online" else "🏬 Офлайн"


def _brand_fact_items(service: CommunityService, brand_id: int) -> list[tuple[str, Any]]:
    """Return logical public facts in the same deterministic order as the brand card."""

    return [(fact.channel, fact) for fact in _brand_fact_groups(service, brand_id)]


def _short_fact_note(note: str, maximum: int = 36) -> str:
    note = note.strip()
    if not note:
        return "без подписи"
    return note if len(note) <= maximum else note[: maximum - 1] + "…"


def _manual_brand_history(
    service: CommunityService, brand_id: int, *, limit: int, offset: int
) -> tuple[Any, ...]:
    """Page non-import audits while legacy per-import rows stay out of Telegram."""

    getter = getattr(service.stores, "brand_history", None)
    if getter is None:
        getter = service.stores.history
    wanted = offset + limit
    raw_offset = 0
    result: list[Any] = []
    while len(result) < wanted and raw_offset <= MAX_HISTORY_OFFSET:
        batch = tuple(getter(brand_id, limit=100, offset=raw_offset))
        if not batch:
            break
        result.extend(entry for entry in batch if entry.kind != "import")
        raw_offset += len(batch)
        if len(batch) < 100:
            break
    return tuple(result[offset:wanted])


def _audit_brand(service: CommunityService, entry: Any):
    brand = (
        _brand(service, entry.brand_id, include_archived=True)
        if isinstance(entry.brand_id, int)
        else None
    )
    return brand or _brand_for_merchant(service, entry.merchant_id, include_archived=True)


def _history_label(service: CommunityService, entry: Any, *, include_brand: bool) -> str:
    summary = getattr(entry, "summary", HISTORY_KINDS.get(entry.kind, entry.kind))
    if not include_brand:
        return f"№{entry.id} · {summary}"[:80]
    brand = _audit_brand(service, entry)
    name = brand.name if brand else f"магазин №{entry.merchant_id}"
    return f"№{entry.id} · {name} · {summary}"[:96]


def _tannei_snapshot_text(snapshot: dict[str, Any]) -> str:
    lines = [
        "tannei.by · сводка импорта",
        f"Источников в снимке: {int(snapshot.get('source_count', 0))}",
    ]
    channels = snapshot.get("channels", {})
    for channel in ("offline", "online"):
        facts = channels.get(channel, {}) if isinstance(channels, dict) else {}
        if not isinstance(facts, dict) or not facts:
            continue
        lines.append(_channel_title(channel) + ":")
        for mcc, details in sorted(facts.items()):
            details = details if isinstance(details, dict) else {}
            first_seen, last_seen = details.get("first_seen"), details.get("last_seen")
            period = (
                str(first_seen)
                if first_seen and first_seen == last_seen
                else " — ".join(str(value) for value in (first_seen, last_seen) if value)
            )
            suffix = f" · {period}" if period else ""
            lines.append(f"• MCC {mcc} · импорт: 1{suffix}")
    return "\n".join(lines)


def _brand_editor_summary(service: CommunityService, brand_id: int) -> str:
    brand = _brand(service, brand_id, include_archived=True)
    if brand is None:
        return "Магазин недоступен."
    aliases = ", ".join(brand.aliases) or "нет"
    lines = [f"Редактирование: {brand.name}", f"Другие названия: {aliases}"]
    location_getter = getattr(service.stores, "brand_location_summary", None)
    location = (
        location_getter(brand.id, include_archived=True)
        if location_getter is not None
        else getattr(brand, "location", None)
    )
    if location:
        lines.append(f"Где находится: {location}")
    source_getter = getattr(service.stores, "brand_source_ids", None)
    source_ids = (
        source_getter(brand.id, include_archived=True) if source_getter is not None else ()
    )
    if source_ids:
        shown = ", ".join(source_ids[:3])
        if len(source_ids) > 3:
            shown += f" · ещё {len(source_ids) - 3}"
        lines.append(f"Источник: tannei.by · ID {shown}")
    for channel in ("offline", "online"):
        facts = list(_brand_facts(service, brand_id, channel))
        if not facts:
            continue
        shown = ", ".join(fact.mcc for fact in facts[:8])
        if len(facts) > 8:
            shown += f" … ещё {len(facts) - 8}"
        lines.append(f"{_channel_title(channel)}: {shown}")
    if len(lines) == 2:
        lines.append("MCC пока не добавлены.")
    return "\n".join(lines)


def _selected_fact(service: CommunityService, data: dict[str, Any]):
    brand_id = data.get("brand_id")
    channel = data.get("channel")
    mcc = data.get("selected_mcc") or data.get("mcc")
    if (
        not isinstance(brand_id, int)
        or channel not in {"offline", "online", "both"}
        or not isinstance(mcc, str)
    ):
        return None
    facts = (
        _brand_fact_groups(service, brand_id)
        if channel == "both"
        else _brand_facts(service, brand_id, channel)
    )
    return next((fact for fact in facts if fact.mcc == mcc and fact.channel == channel), None)


def _selected_fact_text(service: CommunityService, data: dict[str, Any]) -> str:
    brand = _brand(service, data["brand_id"], include_archived=True)
    text = (
        f"Магазин: {brand.name if brand else 'недоступен'}\n"
        f"Способ оплаты: {_channel_title(data['channel'])}\n"
        f"MCC: {data['selected_mcc']}\n"
        f"Подпись: {data.get('original_note') or 'отсутствует'}"
    )
    return text


def _note_change_text(old: str, new: str) -> str:
    if old == new:
        return f"Подпись: {new or 'отсутствует'} (без изменений)"
    if not old:
        return f"Подпись: добавлена «{new}»" if new else "Подпись: отсутствует"
    if not new:
        return f"Подпись: «{old}» → удалена"
    return f"Подпись: «{old}» → «{new}»"


def _administrative_preview(
    service: CommunityService,
    kind: str,
    payload: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> str:
    """Render compact confirmations for direct administrative changes."""

    if kind in {"archive_mcc", "archive_merchant"}:
        merchant = service.stores.get(payload["merchant_id"], include_archived=True)
        brand = (
            _brand_for_merchant(service, payload["merchant_id"], include_archived=True)
            if merchant
            else None
        )
        name = brand.name if brand else "магазин недоступен"
        channel = _channel_label(merchant.channel) if merchant else "способ оплаты недоступен"
        if data and data.get("channel") == "both":
            channel = _channel_label("both")
        if kind == "archive_mcc":
            return (
                f"Убрать MCC {payload['mcc']} у магазина «{name}» ({channel})?\n"
                "Другие MCC останутся без изменений."
            )
        return (
            f"Убрать способ оплаты «{channel}» у магазина «{name}» из поиска?\n"
            "Все его MCC перестанут показываться."
        )
    if kind in {"add_mcc", "add_mcc_both", "replace_mcc", "edit_mcc_note"}:
        if kind == "add_mcc_both":
            brand = _brand(service, payload.get("brand_id", 0), include_archived=True)
            lines = [
                "Проверьте изменение:",
                "",
                f"Магазин: {brand.name if brand else payload.get('name', 'магазин недоступен')}",
                "Способ оплаты: 🏬🌐 Офлайн и онлайн",
                f"MCC: {payload['mcc']}",
                f"Подпись для новых фактов: {payload.get('note') or 'отсутствует'}",
                "Существующие подписи останутся без изменений.",
            ]
            return "\n".join(lines)
        merchant = service.stores.get(payload["merchant_id"], include_archived=True)
        brand = (
            _brand_for_merchant(service, payload["merchant_id"], include_archived=True)
            if merchant
            else None
        )
        lines = [
            "Проверьте изменение:",
            "",
            f"Магазин: {brand.name if brand else 'магазин недоступен'}",
            "Способ оплаты: "
            + (
                _channel_title(str(data.get("channel")))
                if data and data.get("channel") in {"offline", "online", "both"}
                else _channel_title(merchant.channel)
                if merchant
                else "недоступен"
            ),
        ]
        if kind == "replace_mcc":
            lines.append(f"MCC: {payload['old_mcc']} → {payload['mcc']}")
        else:
            lines.append(f"MCC: {payload['mcc']}")
        if kind == "add_mcc":
            lines.append(f"Подпись: {payload.get('note') or 'отсутствует'}")
        else:
            state = data or {}
            lines.append(
                _note_change_text(str(state.get("original_note", "")), payload.get("note", ""))
            )
        return "\n".join(lines)
    return "Проверьте изменение:\n\n" + _display_payload(service, kind, payload)


def _button_only_guidance(service: CommunityService, draft: Draft) -> str:
    """Explain how to continue when text cannot be consumed by the current stage."""

    stage = draft.stage
    if stage == "editor":
        instruction = "Нажмите нужное действие, например «Изменить MCC», под сообщением бота выше."
    elif stage == "brand_editor":
        instruction = "Нажмите, какие данные магазина нужно изменить, под сообщением бота выше."
    elif stage == "mcc_facts":
        instruction = "Выберите строку с нужными способом оплаты и MCC."
    elif stage == "mcc_fact":
        instruction = "Выберите, что изменить в выбранном MCC, кнопкой под сообщением бота выше."
    elif stage == "channel":
        instruction = "Нажмите кнопку нужного способа оплаты."
    elif stage == "note_choice":
        instruction = "Выберите, что сделать с подписью, кнопкой под сообщением бота выше."
    elif stage == "preview":
        direct = service.is_admin(draft.user_id) and not draft.data.get("response_id")
        label = "Сохранить в базу" if direct else "Отправить на проверку"
        instruction = f"Нажмите «{label}», «Назад» или «Отмена» под сообщением бота выше."
    elif stage == "cancel_confirm":
        instruction = "Нажмите «Да, отменить» или «Нет, продолжить» под сообщением бота выше."
    elif stage == "report":
        instruction = "Нажмите «Предложить MCC» под сообщением бота выше."
    elif stage in {"evidence", "response_evidence"}:
        instruction = (
            "Пришлите скриншот как фотографию или нажмите «Без скриншота» под сообщением бота выше."
        )
    else:
        instruction = "Нажмите нужную кнопку под сообщением бота выше."
    return f"Текст на этом шаге не используется. {instruction}"


async def _render_draft(
    update: Update,
    service: CommunityService,
    draft: Draft,
    *,
    notice: str | None = None,
) -> None:
    stage, data = draft.stage, draft.data
    if stage in {"reason", "review_preview"}:
        service.validate_review(draft.user_id, data["proposal_id"], data["proposal_version"])
    rows: list[list[tuple[str, str]]] = []
    if stage == "name":
        text = "Как называется новый магазин? Введите название (до 160 символов)."
    elif stage == "store_name":
        text = "Как называется магазин, для которого нужно добавить MCC?"
    elif stage == "partner_store_name":
        text = "Для какого магазина действует партнёрство по карте? Введите название."
    elif stage in {
        "choose",
        "store_choose",
        "partner_store_choose",
        "edit_choose",
        "target_choose",
        "preview_choose",
    }:
        text = "Выберите магазин. Если нужного нет, уточните название сообщением."
        for brand_id in data.get("matches", [])[:10]:
            brand = _brand(service, brand_id)
            if brand:
                rows.append(
                    [
                        (
                            f"{brand.name[:48]} · №{brand.id}",
                            f"select:{brand.id}",
                        )
                    ]
                )
        if stage in {"choose", "preview_choose"}:
            rows.append([("Создать новый магазин", "new")])
    elif stage == "partner_card":
        cards = data.get("cards", [])
        offset = int(data.get("card_offset", 0))
        page = cards[offset : offset + 10]
        text = "Выберите карту, для которой действует партнёрство."
        rows = [
            [(card["name"][:48], f"partner_card:{offset + index}")]
            for index, card in enumerate(page)
        ]
        navigation = []
        if offset:
            navigation.append(("⬅️ Назад", f"partner_card_page:{max(0, offset - 10)}"))
        if len(cards) > offset + 10:
            navigation.append(("Дальше ➡️", f"partner_card_page:{offset + 10}"))
        if navigation:
            rows.append(navigation)
    elif stage == "partner_channel":
        text = "Где действует партнёрство?"
        rows = [
            [("🏬🌐 Офлайн и онлайн", "partner_channel:any")],
            [("🏬 Офлайн", "partner_channel:offline"), ("🌐 Онлайн", "partner_channel:online")],
        ]
    elif stage == "partner_mode":
        text = "Как учитывать партнёрскую выгоду?"
        rows = [
            [("➕ Добавляется к обычной", "partner_mode:additional")],
            [("= Это итоговая выгода", "partner_mode:total")],
        ]
    elif stage == "partner_reward_kind":
        text = "В чём начисляется партнёрская выгода?"
        rows = [
            [("💵 Денежный возврат", "partner_reward:cash")],
            [("🟡 Баллы", "partner_reward:points")],
        ]
    elif stage == "partner_value":
        text = "Введите процент партнёрской выгоды, например 5 или 2,5."
    elif stage == "partner_conditions":
        text = (
            "Опишите условия партнёрства: минимальную сумму, лимит, нужную оплату или другие "
            "важные ограничения (до 1000 символов)."
        )
    elif stage == "partner_exclusions":
        text = (
            "Перечислите исключённые MCC через запятую, например 4814, 4900. "
            "Если исключений нет, отправьте «нет»."
        )
    elif stage == "partner_source":
        text = (
            "Пришлите полную официальную ссылку на условия (http:// или https://) либо "
            "скриншот официального источника фотографией."
        )
    elif stage == "channel":
        text = f"Выберите способ оплаты для MCC у магазина «{data['name']}»:"
        rows = [
            [("🏬🌐 Офлайн и онлайн", "channel:both")],
            [("🏬 Офлайн", "channel:offline"), ("🌐 Онлайн", "channel:online")],
        ]
    elif stage == "mcc":
        if data.get("kind") == "replace_mcc" and data.get("selected_mcc"):
            text = _selected_fact_text(service, data) + "\n\nВведите новый MCC: ровно четыре цифры."
        else:
            text = (
                f"Магазин: {data.get('name', 'магазин недоступен')}\n"
                f"Способ оплаты: {_channel_title(data['channel'])}\n\n"
                "Введите MCC: ровно четыре цифры."
            )
    elif stage == "note_choice":
        brand = _brand(service, data.get("brand_id", 0), include_archived=True)
        name = brand.name if brand else data.get("name", "магазин недоступен")
        if data.get("kind") == "replace_mcc":
            text = (
                f"Магазин: {name}\nСпособ оплаты: {_channel_title(data['channel'])}\n"
                f"MCC: {data['old_mcc']} → {data['mcc']}\n"
                f"Текущая подпись: {data.get('note') or 'отсутствует'}\n\n"
                "Что сделать с подписью?"
            )
        elif data.get("kind") == "add_mcc_both" or data.get("channel") == "both":
            text = (
                f"Магазин: {name}\nСпособ оплаты: 🏬🌐 Офлайн и онлайн\n"
                f"MCC: {data['mcc']}\n"
                f"Подпись для новых фактов: {data.get('note') or 'отсутствует'}\n\n"
                "Существующие подписи не изменятся. Подпись необязательна. Что сделать?"
            )
        else:
            text = (
                f"Магазин: {name}\nСпособ оплаты: {_channel_title(data['channel'])}\n"
                f"MCC: {data['mcc']}\n"
                f"Подпись: {data.get('note') or 'отсутствует'}\n\n"
                "Подпись необязательна. Что сделать?"
            )
        if data.get("note"):
            rows = [
                [("Оставить подпись", "note_keep")],
                [("✏️ Изменить подпись", "note_edit")],
                [("🗑 Убрать подпись", "note_remove")],
            ]
        else:
            rows = [[("➕ Добавить подпись", "note_edit"), ("Без подписи", "note_keep")]]
    elif stage in {"evidence", "response_evidence"}:
        text = (
            "Пришлите скриншот операции из банка, где видны магазин и MCC.\n"
            "Заранее закройте имя, номер карты, баланс и другие личные данные. "
            "Можно добавить подпись до 1000 символов. Изображение — до 10 МБ.\n"
            "Скриншот увидят только вы и действующие помощники; ссылка удалится "
            "через 5 дней после завершения проверки."
        )
        if (
            stage == "response_evidence"
            or service.is_admin(draft.user_id)
            or data.get("source_url")
        ):
            rows = [[("Без скриншота", "skip")]]
        else:
            rows = [[("🔗 Указать официальную ссылку", "source")]]
    elif stage == "source_url":
        text = (
            "Пришлите полную официальную ссылку на страницу с этими данными "
            "(http:// или https://). Вместо ссылки можно сразу прислать скриншот фотографией."
        )
        rows = [[("📷 Приложить скриншот", "source_screenshot")]]
    elif stage == "note":
        current = data.get("note", "")
        text = (
            f"Текущая подпись: {current or 'отсутствует'}\n\n"
            f"Введите новую подпись к MCC (до {MAX_NOTE} символов). Её увидят все."
        )
        rows = [[("🗑 Убрать подпись", "skip")]] if current else [[("Без подписи", "skip")]]
    elif stage == "private_comment":
        text = (
            "Введите приватный комментарий для помощников (до 1000 символов). "
            "Его не будут показывать в карточке магазина."
        )
        rows = [[("Без комментария", "skip")]]
    elif stage == "comment":
        text = (
            "Добавьте комментарий: например, дата покупки или адрес. Не указывайте личные данные."
        )
        rows = [[("Без комментария", "skip")]]
    elif stage == "response":
        text = "Напишите уточнение для помощника (до 1000 символов)."
    elif stage == "preview_more":
        text = "Что ещё нужно указать? Все эти поля необязательны."
        if not data.get("brand_id"):
            rows.append(
                [
                    (
                        "Другие названия"
                        + (f" · {len(data.get('aliases', []))}" if data.get("aliases") else ""),
                        "more:aliases",
                    )
                ]
            )
        rows.append(
            [
                (
                    "✏️ Подпись к MCC" if data.get("note") else "➕ Подпись к MCC",
                    "more:note",
                )
            ]
        )
        rows.append([("Скриншот", "more:evidence")])
        rows.append(
            [
                (
                    "✏️ Официальная ссылка" if data.get("source_url") else "🔗 Официальная ссылка",
                    "more:source",
                )
            ]
        )
        if not service.is_admin(draft.user_id) and not data.get("response_id"):
            rows.append([("Комментарий проверяющему", "more:comment")])
    elif stage == "new_aliases":
        aliases = ", ".join(data.get("aliases", [])) or "нет"
        text = (
            f"Другие названия сейчас: {aliases}\n\n"
            "Введите названия через запятую. Например: Green, Грин. "
            "Чтобы удалить все, отправьте «-»."
        )
    elif stage == "preview":
        administrative = bool(data.get("editing"))
        text = (
            _administrative_preview(service, data["kind"], data["payload"], data)
            if administrative
            else "Проверьте перед отправкой:\n\n"
            + _display_payload(service, data["kind"], data["payload"])
        )
        if not administrative:
            if data.get("comment"):
                text += "\n\nПриватный комментарий проверяющему: " + data["comment"]
            text += (
                "\nСкриншот: приложен"
                if service.draft_has_media(draft.user_id, draft.id)
                else "\nСкриншот: Без скриншота"
            )
        direct = service.is_admin(draft.user_id) and not data.get("response_id")
        if administrative:
            rows = []
            if data["kind"] in {"add_mcc", "add_mcc_both"} and not data.get("selected_mcc"):
                rows.extend(
                    [
                        [("💳 MCC и способ оплаты", "preview:payment")],
                        [("⋯ Дополнительно", "preview:more")],
                    ]
                )
            elif data["kind"] in {
                "add_mcc",
                "add_mcc_both",
                "replace_mcc",
                "edit_mcc_note",
            }:
                rows.append(
                    [
                        (
                            "✏️ Изменить подпись" if data.get("note") else "➕ Добавить подпись",
                            "preview:note",
                        )
                    ]
                )
            rows.append([("✅ Сохранить в базу", "submit")])
        elif data["kind"] in {
            "add_merchant",
            "add_mcc",
            "add_mcc_both",
            "replace_mcc",
        } and {
            "name",
            "channel",
            "mcc",
        } <= set(data):
            rows = [
                [("🏪 Магазин", "preview:brand")],
                [("💳 MCC и способ оплаты", "preview:payment")],
                [("⋯ Дополнительно", "preview:more")],
                [
                    (
                        "✅ Сохранить в базу" if direct else "Отправить на проверку",
                        "submit",
                    )
                ],
            ]
            if data.get("source_url"):
                rows.insert(-1, [("🔗 Изменить источник", "preview:source")])
            elif not direct and not service.draft_has_media(draft.user_id, draft.id):
                rows.insert(-1, [("🔗 Добавить источник", "preview:source")])
        else:
            rows = [[("Сохранить в базу" if direct else "Отправить на проверку", "submit")]]
    elif stage == "cancel_confirm":
        text = "В черновике уже есть данные. Точно отменить и удалить черновик?"
        rows = [[("Да, отменить", "cancel_yes"), ("Нет, продолжить", "cancel_no")]]
    elif stage == "report":
        text = "Что нужно дополнить или исправить?"
        rows = [
            [
                (
                    "Добавить MCC" if service.is_admin(draft.user_id) else "Предложить MCC",
                    "operation:add_mcc",
                )
            ]
        ]
    elif stage == "edit_name":
        text = "Введите название магазина для редактирования."
    elif stage == "editor":
        text = _brand_editor_summary(service, data["brand_id"])
        rows = [
            [("➕ Добавить MCC", "operation:add_mcc")],
            [("✏️ Изменить MCC", "mcc_actions")],
            [("⋯ Ещё", "brand_actions")],
            [("⬅️ К магазину", "close")],
        ]
    elif stage == "brand_editor":
        text = _brand_editor_summary(service, data["brand_id"])
        rows = [[("Названия", "operation:names"), ("Объединить магазины", "operation:merge_brand")]]
        rows.append([("История / отмена", "history")])
    elif stage == "brand_names":
        brand = _brand(service, data["brand_id"], include_archived=True)
        if brand is None:
            raise StaleAction("Магазин больше недоступен.")
        text = f"Названия магазина\n\nОсновное: {brand.name}"
        text += "\nДругие названия: " + (", ".join(brand.aliases) or "нет")
        rows = [[("✏️ Изменить основное", "name_primary")]]
        rows.extend(
            [[(f"{index + 1}. {alias[:48]}", f"name_alias:{index}")]]
            for index, alias in enumerate(brand.aliases)
        )
        rows.append([("➕ Добавить другое название", "name_add")])
    elif stage == "alias_actions":
        brand = _brand(service, data["brand_id"], include_archived=True)
        index = data.get("name_index")
        if brand is None or not isinstance(index, int) or not 0 <= index < len(brand.aliases):
            raise StaleAction("Название уже изменилось. Откройте список заново.")
        text = f"Другое название: {brand.aliases[index]}\n\nЧто сделать?"
        rows = [
            [("⭐ Сделать основным", "name_promote")],
            [("✏️ Изменить", "name_edit"), ("🗑 Удалить", "name_delete")],
        ]
    elif stage == "name_value":
        brand = _brand(service, data["brand_id"], include_archived=True)
        mode = data.get("name_mode")
        if brand is None:
            raise StaleAction("Магазин больше недоступен.")
        if mode == "primary":
            text = f"Текущее основное название: {brand.name}\n\nВведите новое основное название."
        elif mode == "add":
            text = "Введите ещё одно название магазина."
        else:
            index = data.get("name_index")
            if not isinstance(index, int) or not 0 <= index < len(brand.aliases):
                raise StaleAction("Название уже изменилось. Откройте список заново.")
            text = f"Текущее название: {brand.aliases[index]}\n\nВведите новое значение."
    elif stage == "mcc_facts":
        items = _brand_fact_items(service, data["brand_id"])
        offset = min(max(0, int(data.get("fact_offset", 0))), max(0, len(items) - 1))
        offset -= offset % FACT_PAGE_SIZE
        page = items[offset : offset + FACT_PAGE_SIZE]
        brand = _brand(service, data["brand_id"], include_archived=True)
        text = f"Какой MCC изменить у магазина «{brand.name if brand else 'магазин недоступен'}»?"
        for number, (channel, fact) in enumerate(page, start=offset + 1):
            text += (
                f"\n{number}. {_channel_title(channel)} · MCC {fact.mcc} — "
                f"{_short_fact_note(getattr(fact, 'note', ''), MAX_NOTE)}"
            )
            rows.append(
                [
                    (
                        f"{number} · {_channel_title(channel)} · {fact.mcc}",
                        f"fact:{channel}:{fact.mcc}",
                    )
                ]
            )
        if not page:
            text += "\nMCC пока не добавлены."
        pagination = []
        if offset:
            pagination.append(("⬅️", f"fact_page:{max(0, offset - FACT_PAGE_SIZE)}"))
        if offset + FACT_PAGE_SIZE < len(items):
            pagination.append(("➡️", f"fact_page:{offset + FACT_PAGE_SIZE}"))
        if pagination:
            rows.append(pagination)
    elif stage == "mcc_fact":
        text = _selected_fact_text(service, data) + "\n\nЧто изменить?"
        if data.get("channel") == "both":
            text += (
                "\nОбщие действия изменят офлайн- и онлайн-оплату вместе. "
                "При необходимости выберите один способ отдельно."
            )
        rows = [
            [("✏️ Заменить MCC", "fact_replace")],
            [
                (
                    "✏️ Изменить подпись" if data.get("original_note") else "➕ Добавить подпись",
                    "fact_note",
                )
            ],
            [("🗑 Убрать MCC", "fact_remove")],
        ]
        if data.get("channel") == "both":
            rows.append(
                [
                    ("🏬 Только офлайн", "fact_channel:offline"),
                    ("🌐 Только онлайн", "fact_channel:online"),
                ]
            )
        elif data.get("combined_channels") == ["offline", "online"]:
            rows.append([("⬅️ Оба способа оплаты", "fact_all")])
    elif stage == "edit_value":
        brand = _brand(service, data["brand_id"], include_archived=True)
        if data["kind"] == "rename_brand":
            current = brand.name if brand else "магазин недоступен"
            text = f"Текущее название: {current}\n\nВведите новое название магазина."
        elif data["kind"] == "brand_aliases":
            aliases = ", ".join(brand.aliases) if brand else ""
            text = (
                f"Текущие другие названия: {aliases or 'нет'}\n\n"
                "Введите другие названия через запятую (до 20). Для удаления всех отправьте «-»."
            )
        else:
            text = "Введите новое значение."
    elif stage == "target_name":
        brand = _brand(service, data["brand_id"], include_archived=True)
        text = (
            f"Сейчас редактируется: {brand.name if brand else 'магазин недоступен'}\n\n"
            "Введите название магазина, который нужно оставить после объединения."
        )
    elif stage == "history":
        offset = data.get("history_offset", 0)
        brand = _brand(service, data["brand_id"], include_archived=True)
        entries = _manual_brand_history(
            service,
            data["brand_id"],
            limit=HISTORY_PAGE_SIZE + 1,
            offset=offset,
        )
        page = entries[:HISTORY_PAGE_SIZE]
        undo_ids = [
            entry.id
            for entry in page
            if not entry.reverted_by and entry.kind not in {"import", "revert"}
        ]
        view = {
            "history_offset": offset,
            "history_ids": [entry.id for entry in page],
            "history_undo_ids": undo_ids,
            "history_has_next": len(entries) > HISTORY_PAGE_SIZE and offset < MAX_HISTORY_OFFSET,
        }
        if any(data.get(key) != value for key, value in view.items()):
            data = {**data, **view}
            draft = service.advance(draft.user_id, draft.id, draft.version, stage, data)
        name = brand.name if brand else f"магазин №{data['brand_id']}"
        text = (
            f"История магазина «{name}» · страница {offset // HISTORY_PAGE_SIZE + 1}.\n"
            "Выберите изменение, чтобы посмотреть подробности."
        )
        snapshot = _tannei_snapshot(service, data["brand_id"])
        if offset == 0 and snapshot:
            text += "\n\n" + _tannei_snapshot_text(snapshot)
        for entry in page:
            rows.append(
                [(_history_label(service, entry, include_brand=False), f"entry:{entry.id}")]
            )
        if not page:
            text += "\nНа этой странице изменений нет."
        navigation = []
        if offset:
            navigation.append(
                ("⬅️ Предыдущая страница", f"history_page:{offset - HISTORY_PAGE_SIZE}")
            )
        if view["history_has_next"]:
            navigation.append(
                ("Следующая страница ➡️", f"history_page:{offset + HISTORY_PAGE_SIZE}")
            )
        if navigation:
            rows.append(navigation)
    elif stage == "history_entry":
        audit_id = data.get("audit_id")
        if audit_id not in data.get("history_ids", []):
            raise StaleAction("Запись истории больше недоступна.")
        entry = service.stores.audit_entry(audit_id)
        if entry is None:
            raise StaleAction("Запись истории больше недоступна.")
        brand = _audit_brand(service, entry)
        name = brand.name if brand else f"магазин №{entry.merchant_id}"
        text = f"{name}\n\n№{entry.id} · {entry.summary}"
        for detail in entry.details:
            text += f"\n• {detail}"
        text += f"\nИзменил: {_audit_identity(service.audit_actor(draft.user_id, entry.actor_id))}"
        if entry.reverted_by:
            text += f"\nИзменение уже отменено записью №{entry.reverted_by}."
        elif entry.id in data.get("history_undo_ids", []):
            rows.append([("Отменить это изменение", f"undo:{entry.id}")])
        if "recent_offset" in data:
            rows.append([("⬅️ К списку изменений", "recent_back")])
    elif stage == "reason":
        text = "Что нужно уточнить у автора?"
    elif stage == "review_preview":
        text = "Проверьте сообщение автору:\n\n" + data["reason"]
        rows = [[("Отправить решение", "decision")]]
    else:
        raise CommunityError("Неизвестный шаг. Отмените действие и начните заново.")
    if notice:
        text = notice + "\n\n" + text
    markup: Any = _draft_buttons(draft, rows)
    if draft.data.get("draft_mode") and not rows:
        markup = draft_keyboard_for()
    await (
        _say(update, text, markup)
        if isinstance(markup, ReplyKeyboardMarkup)
        else _say_inline(update, text, markup)
    )


async def _queue(
    update: Update,
    service: CommunityService,
    user_id: int,
    offset: int = 0,
    *,
    notice: str | None = None,
) -> None:
    proposals = service.queue(user_id, offset=offset)
    rows = [
        [
            (
                _queue_proposal_label(service, proposal),
                f"q:{proposal.id}:{proposal.version}:claim",
            )
        ]
        for proposal in proposals
    ]
    if offset:
        rows.append([("⬅️ Назад", f"queue:{max(0, offset - 10)}")])
    if len(proposals) == 10:
        rows.append([("Дальше ➡️", f"queue:{offset + 10}")])
    await _say_inline(
        update,
        ((notice + "\n\n") if notice else "")
        + ("Выберите предложение для разбора." if proposals else "Очередь пуста."),
        _keyboard(rows),
    )


def _queue_proposal_label(service: CommunityService, proposal: Proposal) -> str:
    """Render one compact queue label with a brand and a plain-language action."""

    payload = proposal.payload
    brand = None
    if isinstance(payload.get("brand_id"), int):
        brand = _brand(service, payload["brand_id"], include_archived=True)
    if brand is None and isinstance(payload.get("merchant_id"), int):
        brand = _brand_for_merchant(service, payload["merchant_id"], include_archived=True)
    name = brand.name if brand else str(payload.get("name") or "Магазин")
    actions = {
        "add_merchant": "добавление MCC",
        "add_mcc": "добавление MCC",
        "add_mcc_both": "добавление MCC",
        "replace_mcc": "изменение MCC",
        "archive_mcc": "изменение MCC",
        "edit_mcc_note": "изменение MCC",
        "rename_merchant": "изменение названия",
        "rename_brand": "изменение названия",
        "aliases": "добавление названия",
        "brand_aliases": "добавление названия",
        "edit_brand_names": "изменение названия",
        "merge_merchant": "объединение магазинов",
        "merge_brand": "объединение магазинов",
        "card_partnership": "партнёрство по карте",
    }
    return f"№{proposal.id} · {name[:32]} — {actions.get(proposal.kind, 'изменение данных')}"


async def _review_view(update: Update, service: CommunityService, proposal: Proposal) -> None:
    prefix = f"q:{proposal.id}:{proposal.version}:"
    text = f"Разбор №{proposal.id} · резерв на 15 минут\n\n" + _display_payload(
        service, proposal.kind, proposal.payload
    )
    if proposal.comment:
        text += "\nПриватный комментарий автора: " + proposal.comment
    has_media = service.proposal_has_media(proposal.reviewer_id or proposal.user_id, proposal.id)
    text += "\nСкриншот: приложен" if has_media else "\nСкриншот: Без скриншота"
    if proposal.reason:
        text += "\nПредыдущее уточнение: " + proposal.reason
    approve = "Добавить как ещё один MCC" if proposal.kind == "add_mcc" else "Принять"
    rows = [[(approve, prefix + "approve")]]
    if proposal.kind in {"store_metadata", "mcc_save", "partner_save"}:
        rows.append([("✏️ Исправить данные", prefix + "edit")])
    if proposal.kind == "add_mcc":
        rows.append([("Заменить ошибочный MCC", prefix + "replace")])
    rows.append([("Отклонить", prefix + "reject"), ("Уточнить", prefix + "clarify")])
    rows.append([("Отклонить с комментарием", prefix + "reject_reason")])
    if has_media:
        rows.append([("Скриншот", f"media:{proposal.id}:{proposal.version}")])
    rows.append([("Отменить разбор", prefix + "release")])
    await _say_inline(update, text, _keyboard(rows))


async def _recent_history(
    update: Update, service: CommunityService, user_id: int, offset: int
) -> None:
    """Show compact global history labels while keeping details one tap away."""

    if not service.is_admin(user_id):
        raise CommunityError("История доступна только действующим помощникам.")
    entries = service.stores.history(limit=HISTORY_PAGE_SIZE + 1, offset=offset)
    rows = [
        [
            (
                _history_label(service, entry, include_brand=True),
                f"history_entry:{entry.id}:{offset}",
            )
        ]
        for entry in entries[:HISTORY_PAGE_SIZE]
    ]
    navigation = []
    if offset:
        navigation.append(("⬅️ Предыдущая страница", f"recent:{offset - HISTORY_PAGE_SIZE}"))
    if len(entries) > HISTORY_PAGE_SIZE and offset < MAX_HISTORY_OFFSET:
        navigation.append(("Следующая страница ➡️", f"recent:{offset + HISTORY_PAGE_SIZE}"))
    if navigation:
        rows.append(navigation)
    rows.append([("⬅️ К управлению", "manage")])
    await _say_inline(
        update,
        f"Изменения, включая скрытые магазины · страница {offset // HISTORY_PAGE_SIZE + 1}."
        if entries
        else "На этой странице изменений нет.",
        _keyboard(rows),
    )


async def _management(update: Update, service: CommunityService, user_id: int) -> None:
    if not service.is_admin(user_id):
        raise CommunityError("Управление доступно только действующим помощникам.")
    await _say_inline(update, "Управление", management_keyboard_for(service, user_id))


async def _add_menu(update: Update, service: CommunityService, user_id: int) -> None:
    """Show the one role-aware entry point for all supported contribution kinds."""

    verb = "Добавить" if service.is_admin(user_id) else "Предложить"
    await _say_inline(
        update,
        f"Что хотите {verb.lower()}?",
        _keyboard(
            [
                [("🏪 Новый магазин", "add:new_store")],
                [("🧾 MCC магазина", "add:store_mcc")],
                [("🤝 Партнёрство по карте", "add:partner")],
            ]
        ),
    )


async def _role_list(
    update: Update, service: CommunityService, user_id: int, offset: int = 0
) -> None:
    """Show the owner-only helper list from either keyboard or legacy callback."""

    candidates = service.role_candidates(user_id)
    rows = []
    for item in candidates[offset : offset + 10]:
        state = "Помощник" if item["active"] else "Заявка"
        rows.append(
            [
                (
                    f"{state} · {_role_identity(item, compact=True)}",
                    f"role_view:{item['user_id']}:{item['epoch']}",
                )
            ]
        )
    if offset:
        rows.append([("⬅️ Назад", f"roles:{max(0, offset - 10)}")])
    if len(candidates) > offset + 10:
        rows.append([("Дальше ➡️", f"roles:{offset + 10}")])
    rows.append([("⬅️ К управлению", "manage")])
    await _say_inline(
        update,
        "Помощники и заявки" if candidates else "Заявок и помощников пока нет.",
        _keyboard(rows),
    )


async def _notify(context: ContextTypes.DEFAULT_TYPE, proposal: Proposal) -> None:
    if proposal.status != "clarification":
        return
    rows = [[("Ответить на уточнение", f"respond:{proposal.id}:{proposal.version}")]]
    text = f"Нужно уточнить предложение №{proposal.id}."
    if proposal.reason:
        text += "\n" + proposal.reason
    try:
        await context.bot.send_message(
            chat_id=proposal.user_id, text=text, reply_markup=_keyboard(rows)
        )
    except TelegramError:
        LOGGER.info("Could not deliver a contribution clarification request")


async def _notify_role(
    context: ContextTypes.DEFAULT_TYPE, service: CommunityService, user_id: int, text: str
) -> bool:
    try:
        await context.bot.send_message(
            chat_id=user_id, text=text, reply_markup=keyboard_for(service, user_id)
        )
    except TelegramError:
        LOGGER.info("Could not deliver role notification; role change remains committed")
        return False
    return True


def _advance(
    service: CommunityService,
    draft: Draft,
    stage: str,
    data: dict[str, Any] | None = None,
    *,
    update_id: int | None = None,
    media: tuple[str, str] | None = None,
) -> Draft:
    state = dict(draft.data if data is None else data)
    if stage != draft.stage:
        # Store only stage names, never nested snapshots or media references.
        state["back"] = [*draft.data.get("back", [])[-11:], draft.stage]
    return service.advance(
        draft.user_id, draft.id, draft.version, stage, state, update_id=update_id, media=media
    )


_FORM_TITLES = {
    "store_create": "Новый магазин и первый MCC",
    "store_metadata": "Данные магазина",
    "mcc_save": "MCC магазина",
    "partner_save": "Партнёрство по карте",
}


def _form_fields(form: str) -> tuple[tuple[str, str, bool], ...]:
    """Return ordered editor fields as key, Russian label and mandatory flag."""

    if form == "store_create":
        return (
            ("name", "Название", True),
            ("mcc", "MCC", True),
            ("channel", "Способ оплаты", True),
            ("aliases", "Другие названия", False),
            ("location", "Где находится", False),
            ("note", "Подпись к MCC", False),
            ("source_url", "Источник", False),
            ("screenshot", "Скриншот", False),
        )
    if form == "store_metadata":
        return (
            ("name", "Название", True),
            ("aliases", "Другие названия", False),
            ("location", "Где находится", False),
        )
    if form == "mcc_save":
        return (
            ("brand_id", "Магазин", True),
            ("mcc", "MCC", True),
            ("channel", "Способ оплаты", True),
            ("note", "Подпись к MCC", False),
            ("source_url", "Источник", False),
            ("screenshot", "Скриншот", False),
        )
    if form == "partner_save":
        return (
            ("brand_id", "Магазин", True),
            ("card_id", "Карта", True),
            ("channel", "Где действует", True),
            ("value", "Размер выгоды", True),
            ("conditions", "Условия", False),
            ("starts_on", "Дата начала", False),
            ("ends_on", "Дата окончания", False),
            ("min_purchase", "Минимальная покупка", False),
            ("max_purchase", "Максимальная покупка", False),
            ("per_transaction_cap", "Лимит за операцию", False),
            ("excluded_mccs", "Исключённые MCC", False),
            ("source_url", "Источник", False),
            ("screenshot", "Скриншот", False),
        )
    raise CommunityError("Неизвестный редактор.")


def _form_card(context: ContextTypes.DEFAULT_TYPE, card_id: str | None):
    catalog = context.application.bot_data.get("catalog")
    return next((card for card in getattr(catalog, "cards", ()) if card.id == card_id), None)


def _partner_policy_is_explicit(card: Any) -> bool:
    """Return whether a card is intentionally enabled for partnership editing."""

    return bool(getattr(card, "partner_policy_explicit", False))


def _form_value_text(
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    draft: Draft,
    key: str,
) -> str:
    values = draft.data.get("values", {})
    value = values.get(key)
    if key == "brand_id":
        if isinstance(value, int):
            brand = _brand(service, value, include_archived=True)
            return brand.name if brand else "магазин недоступен"
        return str(values.get("name") or "")
    if key == "card_id":
        card = _form_card(context, value)
        return card.name if card else ""
    if key == "channel":
        return {
            "offline": "🏬 Офлайн",
            "online": "🌐 Онлайн",
            "both": "🏬🌐 Офлайн и онлайн",
            "any": "🏬🌐 Офлайн и онлайн",
        }.get(value, "")
    if key == "aliases":
        return ", ".join(value or ())
    if key == "location":
        return str(value or "")
    if key == "excluded_mccs":
        return ", ".join(value or ())
    if key == "screenshot":
        return "приложен" if service.draft_has_media(draft.user_id, draft.id) else ""
    return str(value) if value not in {None, ""} else ""


def _location_required(
    service: CommunityService | None, data: dict[str, Any]
) -> bool:
    """Whether an entered name collides and therefore needs a location."""

    if service is None or data.get("form") not in {"store_create", "store_metadata"}:
        return False
    values = data.get("values", {})
    name = values.get("name")
    if not isinstance(name, str) or not name.strip():
        return False
    excluded = values.get("brand_id") if data.get("form") == "store_metadata" else None
    needle = normalize_store_name(name)
    for brand in service.stores.list_brands():
        if excluded == brand.id:
            continue
        names = {
            normalize_store_name(brand.name),
            *(normalize_store_name(alias) for alias in brand.aliases),
        }
        if needle in names:
            return True
    return False


def _form_complete(
    data: dict[str, Any], service: CommunityService | None = None
) -> bool:
    values = data.get("values", {})
    form = data.get("form")
    for key, _label, required in _form_fields(form):
        if key == "location" and _location_required(service, data):
            required = True
        if not required:
            continue
        if key == "brand_id":
            if not isinstance(values.get("brand_id"), int) and not values.get("name"):
                return False
        elif values.get(key) in {None, ""}:
            return False
    return True


def _form_payload(
    context: ContextTypes.DEFAULT_TYPE, service: CommunityService, data: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    form = data["form"]
    values = dict(data.get("values", {}))
    if form == "store_create":
        payload = {
            "name": values["name"],
            "aliases": list(values.get("aliases", [])),
            "mcc": values["mcc"],
            "channel": values["channel"],
            "note": values.get("note", ""),
            "source_url": values.get("source_url", ""),
        }
        if values.get("location"):
            payload["location"] = values["location"]
        return "mcc_save", payload
    if form == "store_metadata":
        return "store_metadata", {
            "brand_id": values["brand_id"],
            "name": values["name"],
            "aliases": list(values.get("aliases", [])),
            "location": values.get("location"),
        }
    if form == "mcc_save":
        payload: dict[str, Any] = {
            "brand_id": values["brand_id"],
            "mcc": values["mcc"],
            "channel": values["channel"],
            "note": values.get("note", ""),
            "source_url": values.get("source_url", ""),
        }
        for key in ("merchant_id", "merchant_ids", "channels", "old_mcc"):
            if key in values:
                payload[key] = values[key]
        return "mcc_save", payload
    if form == "partner_save":
        payload = {
            "card_id": values["card_id"],
            "channel": values["channel"],
            "value": values["value"],
        }
        if isinstance(values.get("brand_id"), int):
            payload["brand_id"] = values["brand_id"]
        else:
            payload["name"] = values["name"]
        if isinstance(values.get("offer_id"), int):
            payload["offer_id"] = values["offer_id"]
        for key in (
            "conditions",
            "starts_on",
            "ends_on",
            "min_purchase",
            "max_purchase",
            "per_transaction_cap",
            "excluded_mccs",
            "source_url",
        ):
            if values.get(key) not in (None, "", []):
                payload[key] = values[key]
        card = _form_card(context, values.get("card_id"))
        if card is None or not _partner_policy_is_explicit(card):
            raise CommunityError("Карта больше недоступна.")
        payload["mode"] = card.partner_policy.mode
        payload["reward_kind"] = card.partner_policy.reward_kind
        return "partner_save", payload
    raise CommunityError("Неизвестный редактор.")


def _form_prompt(key: str) -> str:
    return {
        "name": "Отправьте название одним сообщением.",
        "brand_id": "Отправьте название магазина. Затем выберите его кнопкой.",
        "mcc": "Отправьте MCC: ровно четыре цифры.",
        "value": "Отправьте размер выгоды в процентах, например 5 или 2,5.",
        "aliases": "Отправьте названия через запятую. Чтобы очистить поле, отправьте «-».",
        "location": "Отправьте адрес или описание места. Чтобы очистить поле, отправьте «-».",
        "note": "Отправьте подпись к MCC. Чтобы очистить поле, отправьте «-».",
        "conditions": "Отправьте условия. Чтобы очистить поле, отправьте «-».",
        "starts_on": "Отправьте дату в формате ГГГГ-ММ-ДД или «-».",
        "ends_on": "Отправьте дату в формате ГГГГ-ММ-ДД или «-».",
        "min_purchase": "Отправьте сумму или «-».",
        "max_purchase": "Отправьте сумму или «-».",
        "per_transaction_cap": "Отправьте лимит за операцию или «-».",
        "excluded_mccs": "Отправьте MCC через запятую или «-».",
        "source_url": "Отправьте полную ссылку http:// или https:// либо «-».",
        "screenshot": "Пришлите скриншот фотографией Telegram.",
        "card_id": "Выберите карту кнопкой ниже.",
        "channel": "Выберите вариант кнопкой ниже.",
    }.get(key, "Отправьте новое значение.")


def _form_rows(
    context: ContextTypes.DEFAULT_TYPE, service: CommunityService, draft: Draft
) -> list[list[tuple[str, str]]]:
    data = draft.data
    active = data.get("active_field")
    rows: list[list[tuple[str, str]]] = []
    if active == "brand_id":
        for brand_id in data.get("matches", [])[:10]:
            brand = _brand(service, brand_id)
            if brand:
                rows.append([(brand.name[:48], f"form_select:{brand.id}")])
        if data.get("new_store_name") and service.is_admin(draft.user_id):
            rows.append(
                [(f"Создать «{data['new_store_name'][:38]}»", "form_new_store")]
            )
    elif active == "card_id":
        cards = getattr(context.application.bot_data.get("catalog"), "cards", ())
        rows.extend(
            [(card.name[:48], f"form_card:{index}")]
            for index, card in enumerate(cards)
            if _partner_policy_is_explicit(card)
        )
    elif active == "channel":
        options = (
            (
                ("🏬🌐 Офлайн и онлайн", "both"),
                ("🏬 Офлайн", "offline"),
                ("🌐 Онлайн", "online"),
            )
            if data.get("form") != "partner_save"
            else (
                ("🏬🌐 Офлайн и онлайн", "any"),
                ("🏬 Офлайн", "offline"),
                ("🌐 Онлайн", "online"),
            )
        )
        rows.append([(label, f"form_channel:{value}") for label, value in options[:1]])
        rows.append([(label, f"form_channel:{value}") for label, value in options[1:]])
    else:
        optional_open = bool(data.get("optional_open")) or _location_required(service, data)
        for key, label, required in _form_fields(data["form"]):
            if key == "location" and _location_required(service, data):
                required = True
            if not required and not optional_open:
                continue
            value = _form_value_text(context, service, draft, key)
            marker = "❗ " if required and not value else ""
            rows.append([(f"{marker}{label}{' *' if required else ''}", f"form_field:{key}")])
        rows.append(
            [
                (
                    HIDE_OPTIONAL_FIELDS if optional_open else OPTIONAL_FIELDS,
                    "form_more",
                )
            ]
        )
        if _form_complete(data, service):
            rows.append([("💾 Сохранить", "form_save")])
        rows.append([("❌ Отменить", "form_cancel")])
    return rows


async def _render_form_editor(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    draft: Draft,
    *,
    notice: str | None = None,
) -> None:
    """Render and update the one durable Telegram message for a form."""

    title = _FORM_TITLES[draft.data["form"]]
    lines = [title, ""]
    for key, label, required in _form_fields(draft.data["form"]):
        if key == "location" and _location_required(service, draft.data):
            required = True
        value = _form_value_text(context, service, draft, key)
        marker = "❗" if required and not value else "•"
        lines.append(f"{marker} {label}{' *' if required else ''}: {value or 'не заполнено'}")
    active = draft.data.get("active_field")
    if active:
        lines.extend(("", _form_prompt(active)))
    else:
        lines.extend(("", "Поля можно заполнять в любом порядке."))
    if notice:
        lines.extend(("", notice))
    text = "\n".join(lines)
    markup = _draft_buttons(draft, _form_rows(context, service, draft))
    query = update.callback_query
    if query is not None and getattr(query, "message", None) is not None:
        await _say_inline(update, text, markup)
        message = query.message
        chat = getattr(message, "chat", None)
        chat_id = getattr(message, "chat_id", getattr(chat, "id", None))
        message_id = getattr(message, "message_id", None)
        if isinstance(chat_id, int) and isinstance(message_id, int):
            service.bind_editor_message(draft.user_id, draft.id, chat_id, message_id)
        return
    bound = service.editor_message(draft.user_id, draft.id)
    if bound is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=bound[0], message_id=bound[1], text=text, reply_markup=markup
            )
            return
        except TelegramError:
            LOGGER.info("Could not update bound contribution editor; replacing it")
    message = update.effective_message
    if message is None:
        return
    sent = await message.reply_text(text, reply_markup=markup)
    chat = getattr(sent, "chat", None)
    chat_id = getattr(sent, "chat_id", getattr(chat, "id", None))
    message_id = getattr(sent, "message_id", None)
    if isinstance(chat_id, int) and isinstance(message_id, int):
        service.bind_editor_message(draft.user_id, draft.id, chat_id, message_id)


def _new_form_data(
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    form: str,
    *,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "draft_mode": True,
        "dirty": False,
        "form": form,
        "values": dict(values or {}),
        "optional_open": False,
    }
    if form == "partner_save":
        data["cards"] = [card.id for card in context.application.bot_data["catalog"].cards]
    return data


async def _begin_form(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    form: str,
    *,
    values: dict[str, Any] | None = None,
) -> None:
    service = _service(context)
    user_id = _identity(update)
    if user_id is None:
        raise CommunityError("Редактор доступен в личном чате с ботом.")
    draft = service.begin(
        user_id,
        stage="form_editor",
        data=_new_form_data(context, service, form, values=values),
        privileged=False,
    )
    await _render_form_editor(update, context, service, draft)


async def begin_contribution(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    brand_id: int | None = None,
    channel: str | None = None,
    name: str | None = None,
    flow_kind: str = "new_store",
) -> None:
    """Open the canonical one-message MCC/store editor with optional context."""

    service = _service(context)
    if flow_kind not in {"new_store", "store_mcc"}:
        raise CommunityError("Неизвестный вид данных.")
    values: dict[str, Any] = {}
    if brand_id is not None:
        brand = _brand(service, brand_id)
        if brand is None:
            raise CommunityError("Магазин больше недоступен. Найдите его заново.")
        values["brand_id"] = brand.id
    elif name:
        values["name"] = clean_text(name, maximum=MAX_NAME)
    if channel is not None:
        if channel not in {"offline", "online"} or brand_id is None:
            raise CommunityError("Некорректный способ оплаты.")
        values["channel"] = channel
    form = "store_create" if flow_kind == "new_store" else "mcc_save"
    await _begin_form(update, context, form, values=values)


async def begin_partner_contribution(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the canonical one-message partnership editor."""

    catalog = context.application.bot_data.get("catalog")
    cards = getattr(catalog, "cards", ())
    if not cards:
        raise CommunityError("Список карт сейчас недоступен.")
    await _begin_form(update, context, "partner_save")


def _consume_form_text(
    service: CommunityService, draft: Draft, text: str, update_id: int | None
) -> Draft:
    """Apply one field value while keeping the editor on its single form stage."""

    data = dict(draft.data)
    values = dict(data.get("values", {}))
    field = data.get("active_field")
    if not isinstance(field, str):
        raise CommunityError("Сначала выберите поле кнопкой в редакторе.")
    if field in {"channel", "card_id", "screenshot"}:
        raise CommunityError(_form_prompt(field))
    if field == "brand_id":
        query = clean_text(text, maximum=MAX_NAME)
        results = service.stores.search(query, limit=10)
        matches = (*results.matches, *results.suggestions)
        data["matches"] = list(dict.fromkeys(item.id for item in matches))[:10]
        data["new_store_name"] = query if data.get("form") == "partner_save" else ""
        data["dirty"] = True
        return service.advance(
            draft.user_id,
            draft.id,
            draft.version,
            "form_editor",
            {**data, "values": values},
            update_id=update_id,
        )
    if field == "name":
        values[field] = clean_text(text, maximum=MAX_NAME)
    elif field == "mcc":
        if not re.fullmatch(r"[0-9]{4}", text):
            raise CommunityError("Введите MCC: ровно четыре цифры.")
        if not service.is_known_mcc(text):
            raise CommunityError(f"MCC {text} не найден в справочнике. Проверьте код.")
        values[field] = text
    elif field == "aliases":
        values[field] = (
            []
            if text == "-"
            else list(
                dict.fromkeys(clean_text(item, maximum=MAX_NAME) for item in text.split(","))
            )
        )
        if len(values[field]) > 20:
            raise CommunityError("Можно сохранить не больше 20 названий.")
    elif field == "location":
        if text == "-":
            values.pop(field, None)
        else:
            values[field] = clean_text(text, maximum=MAX_LOCATION)
    elif field == "excluded_mccs":
        items = [] if text == "-" else [item.strip() for item in text.split(",")]
        if len(items) > 50 or any(not re.fullmatch(r"[0-9]{4}", item) for item in items):
            raise CommunityError("Перечислите не больше 50 MCC из четырёх цифр.")
        values[field] = list(dict.fromkeys(items))
    elif field == "source_url":
        if text == "-":
            values.pop(field, None)
        else:
            parsed = urlparse(text)
            if len(text) > 2048 or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise CommunityError("Укажите полную ссылку с http:// или https://.")
            values[field] = text
    elif field in {"starts_on", "ends_on"}:
        if text == "-":
            values.pop(field, None)
        else:
            try:
                date.fromisoformat(text)
            except ValueError as exc:
                raise CommunityError("Введите дату в формате ГГГГ-ММ-ДД.") from exc
            values[field] = text
    elif field in {"value", "min_purchase", "max_purchase", "per_transaction_cap"}:
        if text == "-" and field != "value":
            values.pop(field, None)
        else:
            normalized = text.replace(",", ".")
            try:
                amount = Decimal(normalized)
            except Exception as exc:
                raise CommunityError("Введите числовое значение.") from exc
            if not amount.is_finite() or amount < 0 or (field == "value" and amount <= 0):
                raise CommunityError("Введите положительное числовое значение.")
            values[field] = format(amount.normalize(), "f")
    elif field in {"note", "conditions"}:
        if text == "-":
            values.pop(field, None)
        else:
            values[field] = clean_text(text, maximum=MAX_NOTE if field == "note" else 1000)
    else:
        raise CommunityError("Поле больше недоступно.")
    data.update(values=values, active_field=None, dirty=True)
    data.pop("matches", None)
    data.pop("new_store_name", None)
    return service.advance(
        draft.user_id,
        draft.id,
        draft.version,
        "form_editor",
        data,
        update_id=update_id,
    )


async def _request_helper_role(update: Update, service: CommunityService, user_id: int) -> None:
    """Create one helper application without refreshing an existing pending request."""

    if service.role_request_status(user_id) == "pending":
        await _say(
            update,
            "Заявка уже отправлена. Доступ появится после подтверждения владельцем.",
            keyboard_for(service, user_id),
        )
        return
    user = update.effective_user
    service.request_role(
        user_id,
        getattr(user, "username", None),
        getattr(user, "first_name", None),
        getattr(user, "last_name", None),
    )
    username = getattr(user, "username", None)
    identity = f"@{username}" if username else "вашим именем в Telegram"
    await _say(
        update,
        f"Заявка от {identity} отправлена владельцу. Доступ появится только после подтверждения.",
        keyboard_for(service, user_id),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume role menu actions and active draft text, returning whether handled."""

    message = update.effective_message
    if message is None or not isinstance(message.text, str):
        return False
    text = message.text.strip()
    user_id = _identity(update)
    if user_id is None:
        if text in {
            INFO,
            GUIDE,
            ADD,
            SUGGEST,
            VOLUNTEER,
            APPLICATION_PENDING,
            LEGACY_MINE,
            QUEUE,
            MANAGE,
            MANAGE_HISTORY,
            MANAGE_DIGEST_ON,
            MANAGE_DIGEST_OFF,
            MANAGE_ROLES,
            MAIN_MENU,
            CANCEL_DRAFT,
        }:
            await _say(update, "Откройте личный чат с ботом.")
            return True
        return False
    service = _service(context)
    try:
        if text == INFO:
            catalog = context.application.bot_data["catalog"]
            await _say(
                update,
                format_limits(catalog.cards),
                _current_reply_keyboard(service, user_id),
            )
        elif text == GUIDE:
            await _say(
                update,
                _guide_text(service, user_id),
                _current_reply_keyboard(service, user_id),
            )
        elif text in {ADD, SUGGEST}:
            if text == ADD and not service.is_admin(user_id):
                raise CommunityError("Роль изменилась. Используйте «➕ Предложить данные».")
            await _add_menu(update, service, user_id)
        elif text in {VOLUNTEER, APPLICATION_PENDING}:
            await _request_helper_role(update, service, user_id)
        elif text == LEGACY_MINE:
            await _say(
                update,
                "Этот раздел больше не используется. Откройте /start.",
                keyboard_for(service, user_id),
            )
        elif text == QUEUE:
            await _queue(update, service, user_id)
        elif text == MANAGE:
            await _management(update, service, user_id)
        elif text == MANAGE_HISTORY:
            await _recent_history(update, service, user_id, 0)
        elif text in {MANAGE_DIGEST_ON, MANAGE_DIGEST_OFF}:
            service.set_digest(user_id, text == MANAGE_DIGEST_ON)
            await _management(update, service, user_id)
        elif text == MANAGE_ROLES:
            if service.role(user_id) != "owner":
                raise CommunityError("Помощниками может управлять только владелец.")
            await _role_list(update, service, user_id)
        elif text == MAIN_MENU:
            await show_menu(update, context)
        elif text == CANCEL_DRAFT:
            draft = service.draft(user_id)
            if draft is None or not draft.data.get("draft_mode"):
                await show_menu(update, context)
            elif draft.stage in {"form_editor", "form_delete", "form_menu"}:
                await _close_draft(update, context, service, draft)
            else:
                await _draft_callback(update, context, service, draft, ["cancel"])
        else:
            draft = service.draft(user_id)
            if draft is None:
                return False
            if draft.stage == "form_editor":
                draft = _consume_form_text(
                    service, draft, text, getattr(update, "update_id", None)
                )
                await _render_form_editor(update, context, service, draft)
                return True
            if (
                re.fullmatch(r"[0-9]{4}", text)
                and draft.stage in MCC_BUTTON_ONLY_STAGES
                and not service.is_known_mcc(text)
            ):
                raise CommunityError(f"MCC {text} не найден в справочнике. Проверьте код.")
            if (
                draft.stage in CLEAN_NAVIGATION_STAGES
                and not draft.data.get("dirty")
                and not service.draft_has_media(user_id, draft.id)
            ):
                service.cancel_draft(user_id, draft.id, draft.version)
                return False
            draft = _consume_text(service, draft, text, getattr(update, "update_id", None))
            await _render_draft(update, service, draft)
    except _ButtonOnlyInput as exc:
        draft = service.draft(user_id)
        if draft is None:
            await _say(update, str(exc), keyboard_for(service, user_id))
        else:
            await _render_draft(update, service, draft, notice=str(exc))
    except (CommunityError, ValueError) as exc:
        await _say(update, str(exc), _current_reply_keyboard(service, user_id))
    return True


def _sync_mcc_payload(data: dict[str, Any]) -> None:
    """Rebuild an MCC proposal after an editable preview field changes."""

    note = str(data.get("note", ""))
    if data.get("kind") == "edit_mcc_note":
        payload = {
            "merchant_id": data["merchant_id"],
            "mcc": data["mcc"],
            "note": note,
        }
    elif data.get("kind") == "replace_mcc":
        payload = {
            "merchant_id": data["merchant_id"],
            "old_mcc": data["old_mcc"],
            "mcc": data["mcc"],
            "note": note,
        }
    elif data.get("channel") == "both":
        data["kind"] = "add_mcc_both"
        payload = {"mcc": data["mcc"], "note": note}
        if data.get("brand_id"):
            payload["brand_id"] = data["brand_id"]
        else:
            payload["name"] = data["name"]
            if data.get("aliases"):
                payload["aliases"] = list(data["aliases"])
    elif data.get("merchant_id"):
        data["kind"] = "add_mcc"
        payload = {"merchant_id": data["merchant_id"], "mcc": data["mcc"], "note": note}
    else:
        data["kind"] = "add_merchant"
        payload = {
            "name": data["name"],
            "channel": data["channel"],
            "mcc": data["mcc"],
            "note": note,
        }
        if data.get("aliases"):
            payload["aliases"] = list(data["aliases"])
        if data.get("brand_id"):
            payload["brand_id"] = data["brand_id"]
    if data.get("kind") in {"replace_mcc", "edit_mcc_note"} and data.get("merchant_ids"):
        payload["merchant_ids"] = list(data["merchant_ids"])
        if len(data.get("channels", [])) > 1:
            payload["channels"] = list(data["channels"])
    if data.get("source_url"):
        payload["source_url"] = data["source_url"]
    data["payload"] = payload


def _sync_partner_payload(data: dict[str, Any]) -> None:
    """Build the JSON-safe fixed-tier payload accepted by ``PartnerRepository``."""

    data["kind"] = "card_partnership"
    data["payload"] = {
        "brand_id": data["brand_id"],
        "card_id": data["card_id"],
        "channel": data["channel"],
        "mode": data["mode"],
        "reward_kind": data["reward_kind"],
        "starts_on": None,
        "ends_on": None,
        "conditions": data["conditions"],
        "source_url": data.get("source_url", ""),
        "tiers": [
            {
                "value": data["value"],
                "min_purchase": None,
                "max_purchase": None,
                "per_transaction_cap": None,
            }
        ],
        "exclusions": [
            {
                "brand_id": data["brand_id"],
                "card_id": data["card_id"],
                "reward_kind": data["reward_kind"],
                "channel": data["channel"],
                "mcc": mcc,
                "starts_on": None,
                "ends_on": None,
                "reason": "Партнёрство не действует для MCC",
                "source_url": data.get("source_url", ""),
            }
            for mcc in data.get("excluded_mccs", [])
        ],
    }


def _consume_text(
    service: CommunityService, draft: Draft, text: str, update_id: int | None
) -> Draft:
    stage, data = draft.stage, dict(draft.data)
    if stage == "name":
        data = {key: value for key, value in data.items() if key in {"draft_mode", "flow_kind"}}
    if stage in {
        "name",
        "choose",
        "store_name",
        "store_choose",
        "partner_store_name",
        "partner_store_choose",
        "edit_name",
        "edit_choose",
        "target_name",
        "target_choose",
        "preview_brand",
        "preview_choose",
    }:
        name = clean_text(text, maximum=MAX_NAME)
        data["dirty"] = True
        results = service.stores.search(name, limit=10)
        matches = (*results.matches, *results.suggestions)
        data["matches"] = list(dict.fromkeys(brand.id for brand in matches))[:10]
        if stage in {"name", "choose"}:
            data["name"] = name
            data.pop("brand_id", None)
            data.pop("selected_brand_id", None)
            stage = "choose" if matches else "channel"
        elif stage in {"store_name", "store_choose"}:
            if not matches:
                raise CommunityError(
                    "Магазин не найден. Проверьте название или сначала добавьте новый магазин."
                )
            stage = "store_choose"
        elif stage in {"partner_store_name", "partner_store_choose"}:
            if not matches:
                raise CommunityError("Магазин не найден. Сначала добавьте его в базу.")
            stage = "partner_store_choose"
        elif stage in {"edit_name", "edit_choose"}:
            stage = "edit_choose"
        elif stage in {"preview_brand", "preview_choose"}:
            data["candidate_name"] = name
            stage = "preview_choose" if matches else "preview"
            if not matches:
                data["name"] = name
                data.pop("brand_id", None)
                data.pop("selected_brand_id", None)
                data.pop("merchant_id", None)
                _sync_mcc_payload(data)
        else:
            data["matches"] = [value for value in data["matches"] if value != data["brand_id"]]
            stage = "target_choose"
    elif stage == "mcc":
        if not re.fullmatch(r"[0-9]{4}", text):
            raise CommunityError("Введите MCC: ровно четыре цифры.")
        if not service.is_known_mcc(text):
            raise CommunityError(f"MCC {text} не найден в справочнике. Проверьте код.")
        data["mcc"] = text
        data["dirty"] = True
        data.setdefault("note", "")
        _sync_mcc_payload(data)
        stage = "note_choice"
    elif stage == "note":
        data["note"] = clean_text(text, maximum=MAX_NOTE)
        data["dirty"] = True
        _sync_mcc_payload(data)
        stage = "preview"
    elif stage in {"source_url", "partner_source"}:
        if len(text) > 2048:
            raise CommunityError("Официальная ссылка слишком длинная.")
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CommunityError("Укажите полную официальную ссылку с http:// или https://.")
        data["source_url"] = text
        data["dirty"] = True
        if data.get("flow_kind") == "partner":
            _sync_partner_payload(data)
        else:
            _sync_mcc_payload(data)
        stage = "preview"
    elif stage == "partner_value":
        normalized = text.replace(",", ".")
        if not re.fullmatch(r"(?:[0-9]{1,2}(?:\.[0-9]{1,4})?|100(?:\.0{1,4})?)", normalized):
            raise CommunityError("Введите процент числом от 0 до 100.")
        value = Decimal(normalized)
        if value <= 0:
            raise CommunityError("Процент должен быть больше 0.")
        data["value"] = format(value.normalize(), "f")
        data["dirty"] = True
        stage = "partner_conditions"
    elif stage == "partner_conditions":
        data["conditions"] = clean_text(text)
        data["dirty"] = True
        stage = "partner_exclusions"
    elif stage == "partner_exclusions":
        if text.casefold() in {"нет", "-", "без исключений"}:
            excluded = []
        else:
            excluded = [value.strip() for value in text.split(",")]
            if len(excluded) > 50 or any(
                not re.fullmatch(r"[0-9]{4}", value) for value in excluded
            ):
                raise CommunityError("Перечислите не больше 50 MCC из четырёх цифр через запятую.")
            excluded = list(dict.fromkeys(excluded))
        data["excluded_mccs"] = excluded
        data["dirty"] = True
        _sync_partner_payload(data)
        stage = "partner_source"
    elif stage == "new_aliases":
        aliases = (
            []
            if text == "-"
            else [clean_text(value, maximum=MAX_NAME) for value in text.split(",")]
        )
        if len(aliases) > 20:
            raise CommunityError("Можно сохранить не больше 20 названий.")
        data["aliases"] = list(dict.fromkeys(aliases))
        data["dirty"] = True
        _sync_mcc_payload(data)
        stage = "preview"
    elif stage in {"private_comment", "comment", "response"}:
        data["comment"] = clean_text(text)
        data["dirty"] = True
        stage = "response_evidence" if stage == "response" else "preview"
    elif stage == "edit_value":
        payload = (
            {"brand_id": data["brand_id"]}
            if data["kind"] in {"rename_brand", "brand_aliases"}
            else {"merchant_id": data["merchant_id"]}
        )
        if data["kind"] in {"rename_merchant", "rename_brand"}:
            payload["name"] = clean_text(text, maximum=MAX_NAME)
        else:
            aliases = (
                []
                if text == "-"
                else [clean_text(value, maximum=MAX_NAME) for value in text.split(",")]
            )
            if len(aliases) > 20:
                raise CommunityError("Можно сохранить не больше 20 названий.")
            payload["aliases"] = aliases
        if data["kind"] in {"rename_brand", "brand_aliases"}:
            payload.pop("merchant_id", None)
        data["payload"] = payload
        data["dirty"] = True
        stage = "preview" if data.get("editing") else "comment"
    elif stage == "name_value":
        brand = _brand(service, data["brand_id"], include_archived=True)
        if brand is None:
            raise StaleAction("Магазин больше недоступен.")
        aliases = list(brand.aliases)
        value = clean_text(text, maximum=MAX_NAME)
        mode = data.get("name_mode")
        if mode == "primary":
            name = value
        elif mode == "add":
            if len(aliases) >= 20:
                raise CommunityError("Можно сохранить не больше 20 других названий.")
            aliases.append(value)
            name = brand.name
        elif mode == "alias":
            index = data.get("name_index")
            if not isinstance(index, int) or not 0 <= index < len(aliases):
                raise StaleAction("Название уже изменилось. Откройте список заново.")
            aliases[index] = value
            name = brand.name
        else:
            raise StaleAction("Действие с названием устарело.")
        data["kind"] = "edit_brand_names"
        data["payload"] = {"brand_id": brand.id, "name": name, "aliases": aliases}
        data["dirty"] = True
        stage = "preview"
    elif stage == "reason":
        data["reason"] = clean_text(text)
        data["dirty"] = True
        stage = "review_preview"
    elif stage in {"evidence", "response_evidence"}:
        raise _ButtonOnlyInput(_button_only_guidance(service, draft))
    else:
        raise _ButtonOnlyInput(_button_only_guidance(service, draft))
    return _advance(service, draft, stage, data, update_id=update_id)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Accept bounded screenshot references only during a private evidence step."""

    user_id = _identity(update)
    if user_id is None:
        return False
    service = _service(context)
    try:
        draft = service.draft(user_id)
        if draft is None:
            return False
        form_screenshot = (
            draft.stage == "form_editor" and draft.data.get("active_field") == "screenshot"
        )
        if not form_screenshot and draft.stage not in {
            "evidence",
            "response_evidence",
            "source_url",
            "partner_source",
        }:
            raise CommunityError("Сейчас вложение не требуется. Продолжите текущий шаг.")
        message = update.effective_message
        photos = getattr(message, "photo", None)
        photo = photos[-1] if photos else None
        if photo is None:
            raise CommunityError("Пришлите скриншот как фотографию Telegram, а не как файл.")
        size = getattr(photo, "file_size", None)
        if size is None or size > MAX_MEDIA_BYTES or size <= 0:
            raise CommunityError("Изображение должно быть не больше 10 МБ.")
        data = dict(draft.data)
        caption = getattr(message, "caption", None)
        if caption:
            data["comment"] = clean_text(caption)
        if form_screenshot:
            data["active_field"] = None
            data["dirty"] = True
            draft = service.advance(
                draft.user_id,
                draft.id,
                draft.version,
                "form_editor",
                data,
                update_id=getattr(update, "update_id", None),
                media=(photo.file_id, photo.file_unique_id),
            )
            await _render_form_editor(update, context, service, draft)
        else:
            draft = _advance(
                service,
                draft,
                "preview",
                data,
                update_id=getattr(update, "update_id", None),
                media=(photo.file_id, photo.file_unique_id),
            )
            await _render_draft(update, service, draft)
    except (CommunityError, ValueError) as exc:
        await _say(update, str(exc))
    return True


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Authorize every callback using effective_user and reject stale private actions."""

    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except TelegramError:
        return
    if getattr(query, "message", None) is None or not getattr(query.message, "is_accessible", True):
        return
    user_id = _identity(update)
    if user_id is None:
        await _say(update, "Это действие доступно только в личном чате с ботом.")
        return
    if (
        not isinstance(query.data, str)
        or len(query.data) > 64
        or not query.data.startswith("community:")
    ):
        return
    service = _service(context)
    parts = query.data.split(":")[1:]
    try:
        for part in parts:
            if re.fullmatch(r"[+-]?[0-9]+", part) and not 0 <= int(part) <= 2**63 - 1:
                raise CommunityError("Кнопка устарела. Откройте меню заново.")
        await _dispatch_callback(update, context, service, user_id, parts)
    except (CommunityError, ValueError, KeyError, IndexError) as exc:
        text = str(exc) if isinstance(exc, ValueError) else "Кнопка устарела. Откройте меню заново."
        await _say(update, text, _current_reply_keyboard(service, user_id))


async def _finish_form_submission(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    draft: Draft,
) -> None:
    proposal = service.submit(draft.user_id, draft.id, draft.version)
    if proposal.status != "approved":
        await _say_with_restored_menu(
            update,
            "Спасибо! Предложение отправлено на проверку.",
            keyboard_for(service, draft.user_id),
        )
        return
    brand_id = proposal.payload.get("brand_id")
    if brand_id is None and proposal.payload.get("name"):
        result = service.stores.search(proposal.payload["name"], limit=10)
        exact = [
            brand
            for brand in result.matches
            if brand.name.casefold() == proposal.payload["name"].casefold()
        ]
        if len(exact) == 1:
            brand_id = exact[0].id
    rows = [[("🏪 Открыть магазин", f"open_brand:{brand_id}")]] if brand_id else []
    await _say_with_restored_menu(
        update,
        "Изменение сохранено.",
        keyboard_for(service, draft.user_id),
        _keyboard(rows) if rows else None,
    )


async def _form_editor_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    draft: Draft,
    parts: list[str],
) -> None:
    action = parts[0]
    data = dict(draft.data)
    values = dict(data.get("values", {}))
    if action == "form_field":
        field = parts[1]
        if field not in {item[0] for item in _form_fields(data["form"])}:
            raise StaleAction("Поле больше недоступно.")
        data["active_field"] = field
        data.pop("matches", None)
        data.pop("new_store_name", None)
    elif action == "form_more":
        data["optional_open"] = not bool(data.get("optional_open"))
        data["active_field"] = None
    elif action == "form_select" and data.get("active_field") == "brand_id":
        brand_id = int(parts[1])
        if brand_id not in data.get("matches", []):
            raise StaleAction("Выберите магазин из текущих результатов.")
        if _brand(service, brand_id) is None:
            raise StaleAction("Магазин больше недоступен.")
        if (
            data.get("form") == "partner_save"
            and not service.is_admin(draft.user_id)
            and not service.brand_has_confirmed_mcc(brand_id)
        ):
            raise CommunityError(
                "Партнёрство можно предложить только для магазина с подтверждённым MCC."
            )
        values["brand_id"] = brand_id
        values.pop("name", None)
        data.update(values=values, active_field=None, dirty=True)
    elif action == "form_new_store" and data.get("active_field") == "brand_id":
        if data.get("form") != "partner_save" or not service.is_admin(draft.user_id):
            raise CommunityError("Новый магазин для партнёрства может создать только помощник.")
        name = data.get("new_store_name")
        if not isinstance(name, str) or not name:
            raise StaleAction("Сначала отправьте название магазина.")
        values["name"] = name
        values.pop("brand_id", None)
        data.update(values=values, active_field=None, dirty=True)
    elif action == "form_card" and data.get("active_field") == "card_id":
        cards = getattr(context.application.bot_data.get("catalog"), "cards", ())
        index = int(parts[1])
        if not 0 <= index < len(cards):
            raise StaleAction("Карта больше недоступна.")
        card = cards[index]
        if not _partner_policy_is_explicit(card):
            raise StaleAction("Карта больше недоступна.")
        values["card_id"] = card.id
        data.update(values=values, active_field=None, dirty=True)
    elif action == "form_channel" and data.get("active_field") == "channel":
        channel = parts[1]
        allowed = (
            {"offline", "online", "any"}
            if data.get("form") == "partner_save"
            else {"offline", "online", "both"}
        )
        if channel not in allowed:
            raise CommunityError("Выберите допустимый способ оплаты.")
        values["channel"] = channel
        data.update(values=values, active_field=None, dirty=True)
    elif action == "form_cancel":
        if data.get("review_edit"):
            proposal_id = data["review_edit"]["proposal_id"]
            service.cancel_draft(draft.user_id, draft.id, draft.version)
            await _review_view(update, service, service.proposal(draft.user_id, proposal_id))
        else:
            await _close_draft(update, context, service, draft)
        return
    elif action == "form_save":
        if not _form_complete(data, service):
            if _location_required(service, data):
                raise CommunityError(
                    "У магазина уже есть такое название. Укажите, где он находится."
                )
            raise CommunityError("Заполните все поля со знаком *.")
        kind, payload = _form_payload(context, service, data)
        if data.get("review_edit"):
            review = data["review_edit"]
            proposal = service.edit_review_payload(
                draft.user_id,
                review["proposal_id"],
                review["proposal_version"],
                payload,
            )
            service.cancel_draft(draft.user_id, draft.id, draft.version)
            await _review_view(update, service, proposal)
            return
        preview = {
            "draft_mode": True,
            "dirty": True,
            "kind": kind,
            "payload": payload,
            "brand_id": payload.get("brand_id"),
            "name": payload.get("name"),
        }
        draft = service.advance(
            draft.user_id, draft.id, draft.version, "preview", preview
        )
        await _finish_form_submission(update, context, service, draft)
        return
    else:
        raise StaleAction("Кнопка не относится к редактору.")
    draft = service.advance(
        draft.user_id, draft.id, draft.version, "form_editor", data
    )
    await _render_form_editor(update, context, service, draft)


async def _render_form_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    draft: Draft,
) -> None:
    brand = _brand(service, draft.data["brand_id"])
    if brand is None:
        raise CommunityError("Магазин больше недоступен.")
    view = draft.data.get("menu_view", "root")
    if view == "mcc":
        text = f"MCC магазина «{brand.name}». Выберите строку:"
        rows = []
        for index, item in enumerate(draft.data.get("facts", [])):
            label = f"MCC {item['mcc']} · {_channel_title(item['channel'])}"
            rows.append(
                [
                    (f"✏️ {label}", f"menu_mcc_edit:{index}"),
                    ("🗑", f"menu_mcc_delete:{index}"),
                ]
            )
        rows.append([("⬅️ Назад", "menu_root")])
    elif view == "more":
        text = f"Дополнительные данные магазина «{brand.name}»:"
        rows = [[("✏️ Название и другие названия", "menu_metadata")]]
        rows.append([("➕ Добавить партнёрство", "menu_partner_new")])
        for index, item in enumerate(draft.data.get("offers", [])):
            card = _form_card(context, item["card_id"])
            label = card.name if card else item["card_id"]
            rows.append(
                [
                    (f"✏️ {label[:38]}", f"menu_partner_edit:{index}"),
                    ("🗑", f"menu_partner_delete:{index}"),
                ]
            )
        rows.append([("⬅️ Назад", "menu_root")])
    else:
        channels: dict[str, list[str]] = {"offline": [], "online": []}
        for item in draft.data.get("facts", []):
            item_channels = (
                ("offline", "online")
                if item["channel"] == "both"
                else (item["channel"],)
            )
            for channel in item_channels:
                if item["mcc"] not in channels[channel]:
                    channels[channel].append(item["mcc"])
        text = "\n".join(
            (
                f"Редактирование: {brand.name}",
                f"🏬 Офлайн: {', '.join(channels['offline']) or 'MCC не указан'}",
                f"🌐 Онлайн: {', '.join(channels['online']) or 'MCC не указан'}",
            )
        )
        rows = [
            [("➕ Добавить MCC", "menu_mcc_new")],
            [("✏️ Изменить MCC", "menu_mcc_list")],
            [("⋯ Ещё", "menu_more")],
            [("⬅️ К магазину", "form_cancel")],
        ]
    await _say_inline(
        update,
        text,
        _draft_buttons(draft, rows),
    )


def _menu_data(service: CommunityService, brand_id: int) -> dict[str, Any]:
    facts = []
    for item in _brand_fact_groups(service, brand_id):
        channel_notes = tuple(getattr(item, "channel_notes", ()))
        if len({note for _channel, note in channel_notes}) > 1:
            for channel, note in channel_notes:
                concrete = next(
                    fact
                    for fact in _brand_facts(service, brand_id, channel)
                    if fact.mcc == item.mcc
                )
                facts.append(
                    {
                        "mcc": item.mcc,
                        "channel": channel,
                        "channels": [channel],
                        "merchant_id": concrete.merchant_ids[0],
                        "merchant_ids": list(concrete.merchant_ids),
                        "note": note,
                    }
                )
            continue
        facts.append(
            {
                "mcc": item.mcc,
                "channel": item.channel,
                "channels": list(item.channels),
                "merchant_id": item.merchant_ids[0],
                "merchant_ids": list(item.merchant_ids),
                "note": item.note,
            }
        )
    offers = []
    partners = service.partners
    if partners is not None:
        for offer in partners.list_offers(brand_id):
            if offer.archived:
                continue
            tier = offer.tiers[0]
            offers.append(
                {
                    "offer_id": offer.id,
                    "brand_id": offer.brand_id,
                    "card_id": offer.card_id,
                    "channel": offer.channel,
                    "value": format(tier.value.normalize(), "f"),
                    "conditions": offer.conditions,
                    "starts_on": offer.starts_on.isoformat() if offer.starts_on else None,
                    "ends_on": offer.ends_on.isoformat() if offer.ends_on else None,
                    "min_purchase": (
                        format(tier.min_purchase.normalize(), "f")
                        if tier.min_purchase is not None
                        else None
                    ),
                    "max_purchase": (
                        format(tier.max_purchase.normalize(), "f")
                        if tier.max_purchase is not None
                        else None
                    ),
                    "per_transaction_cap": (
                        format(tier.per_transaction_cap.normalize(), "f")
                        if tier.per_transaction_cap is not None
                        else None
                    ),
                    "excluded_mccs": [
                        item.mcc
                        for item in partners.list_offer_exclusions(offer.id)
                        if item.mcc
                    ],
                    "source_url": offer.source_url,
                }
            )
    return {
        "draft_mode": True,
        "dirty": False,
        "brand_id": brand_id,
        "facts": facts,
        "offers": offers,
    }


async def _begin_form_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, service: CommunityService, brand_id: int
) -> None:
    if _brand(service, brand_id) is None:
        raise CommunityError("Магазин больше недоступен.")
    draft = service.begin(
        _identity(update),
        stage="form_menu",
        data=_menu_data(service, brand_id),
        privileged=False,
    )
    await _render_form_menu(update, context, service, draft)


async def _render_delete_confirm(
    update: Update, service: CommunityService, draft: Draft
) -> None:
    warning = draft.data.get("warning")
    text = draft.data["title"] + "\n\nУдалить?"
    if warning:
        text += "\n\n⚠️ " + warning
    rows = [[("🗑 Удалить", "delete_yes"), ("Отмена", "delete_no")]]
    await _say_inline(update, text, _draft_buttons(draft, rows))


async def _form_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    draft: Draft,
    parts: list[str],
) -> None:
    action = parts[0]
    data = draft.data
    brand = _brand(service, data["brand_id"])
    if brand is None:
        raise StaleAction("Магазин больше недоступен.")
    if action == "form_cancel":
        await _close_draft(update, context, service, draft)
        return
    if action in {"menu_root", "menu_mcc_list", "menu_more"}:
        next_data = dict(data)
        next_data["menu_view"] = {
            "menu_root": "root",
            "menu_mcc_list": "mcc",
            "menu_more": "more",
        }[action]
        next_draft = service.advance(
            draft.user_id, draft.id, draft.version, "form_menu", next_data
        )
        await _render_form_menu(update, context, service, next_draft)
        return
    if action == "menu_metadata":
        form_data = _new_form_data(
            context,
            service,
            "store_metadata",
            values={
                "brand_id": brand.id,
                "name": brand.name,
                "aliases": list(brand.aliases),
                "location": brand.location,
            },
        )
    elif action == "menu_mcc_new":
        form_data = _new_form_data(
            context, service, "mcc_save", values={"brand_id": brand.id}
        )
    elif action in {"menu_mcc_edit", "menu_mcc_delete"}:
        item = data["facts"][int(parts[1])]
        if action == "menu_mcc_delete":
            delete_data = {
                "draft_mode": True,
                "dirty": True,
                "brand_id": brand.id,
                "kind": "mcc_delete",
                "payload": {
                    "merchant_id": item["merchant_id"],
                    "merchant_ids": item["merchant_ids"],
                    "channels": item["channels"],
                    "mcc": item["mcc"],
                },
                "title": f"MCC {item['mcc']} у магазина «{brand.name}»",
                "warning": (
                    "Это последний MCC. Магазин останется в поиске без MCC."
                    if len(data["facts"]) == 1
                    else ""
                ),
            }
            draft = service.advance(
                draft.user_id, draft.id, draft.version, "form_delete", delete_data
            )
            await _render_delete_confirm(update, service, draft)
            return
        form_data = _new_form_data(
            context,
            service,
            "mcc_save",
            values={
                "brand_id": brand.id,
                "merchant_id": item["merchant_id"],
                "merchant_ids": item["merchant_ids"],
                "channels": item["channels"],
                "old_mcc": item["mcc"],
                "mcc": item["mcc"],
                "channel": item["channel"],
                "note": item["note"],
            },
        )
    elif action == "menu_partner_new":
        form_data = _new_form_data(
            context, service, "partner_save", values={"brand_id": brand.id}
        )
    elif action in {"menu_partner_edit", "menu_partner_delete"}:
        item = data["offers"][int(parts[1])]
        if action == "menu_partner_delete":
            delete_data = {
                "draft_mode": True,
                "dirty": True,
                "brand_id": brand.id,
                "kind": "partner_delete",
                "payload": {"offer_id": item["offer_id"]},
                "title": f"Партнёрство у магазина «{brand.name}»",
            }
            draft = service.advance(
                draft.user_id, draft.id, draft.version, "form_delete", delete_data
            )
            await _render_delete_confirm(update, service, draft)
            return
        form_data = _new_form_data(context, service, "partner_save", values=item)
    else:
        raise StaleAction("Действие больше недоступно.")
    next_draft = service.advance(
        draft.user_id, draft.id, draft.version, "form_editor", form_data
    )
    await _render_form_editor(update, context, service, next_draft)


async def _form_delete_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    draft: Draft,
    parts: list[str],
) -> None:
    if parts[0] == "delete_no":
        brand_id = draft.data.get("brand_id")
        service.cancel_draft(draft.user_id, draft.id, draft.version)
        await _begin_form_menu(update, context, service, brand_id)
        return
    if parts[0] != "delete_yes":
        raise StaleAction("Выберите подтверждение или отмену.")
    preview = {
        "draft_mode": True,
        "dirty": True,
        "kind": draft.data["kind"],
        "payload": draft.data["payload"],
        "brand_id": draft.data.get("brand_id"),
    }
    draft = service.advance(draft.user_id, draft.id, draft.version, "preview", preview)
    await _finish_form_submission(update, context, service, draft)


async def _dispatch_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    user_id: int,
    parts: list[str],
) -> None:
    action = parts[0]
    if action in {"pending", "pending_new"}:
        from .store_handlers import pending_new_overlay, pending_overlay

        if action == "pending":
            brand_id = int(parts[1])
            text, _count, proposal_ids = pending_overlay(context, brand_id)
            rows = [[("⬅️ К магазину", f"open_brand:{brand_id}")]]
        else:
            text, proposal_ids = pending_new_overlay(context, int(parts[1]))
            rows = []
        if service.is_admin(user_id):
            identities = []
            for proposal_id in proposal_ids:
                author = service.proposal_author(user_id, proposal_id)
                username = f"@{author['username']}" if author.get("username") else "без username"
                identities.append(f"№{proposal_id}: {username} · ID {author['user_id']}")
            if identities:
                text += "\n\nАвторы (видно только помощникам):\n" + "\n".join(identities)
        await _say_inline(
            update,
            text,
            _keyboard(rows) if rows else InlineKeyboardMarkup([]),
            parse_mode=ParseMode.HTML,
        )
    elif action == "start":
        brand_id = int(parts[1]) or None if len(parts) >= 2 else None
        channel = parts[2] if len(parts) == 3 and parts[2] in {"offline", "online"} else None
        name = None
        if len(parts) == 3 and brand_id is None and channel is None:
            name = context.user_data.get("store_searches", {}).get(parts[2])
            if name is None:
                raise StaleAction("Поиск устарел. Отправьте название магазина ещё раз.")
        await begin_contribution(
            update,
            context,
            brand_id=brand_id,
            channel=channel,
            name=name,
            flow_kind="store_mcc",
        )
    elif action == "add":
        if len(parts) != 2:
            raise CommunityError("Выберите вид данных заново.")
        if parts[1] in {"new_store", "store_mcc"}:
            await begin_contribution(update, context, flow_kind=parts[1])
        elif parts[1] == "partner":
            await begin_partner_contribution(update, context)
        else:
            raise CommunityError("Выберите вид данных заново.")
    elif action == "open_brand":
        from .store_handlers import _brand_view

        brand = _brand(service, int(parts[1]))
        if brand is None:
            raise CommunityError("Магазин больше недоступен.")
        text, markup = _brand_view(service.stores, brand, 0, context, user_id, private=True)
        await _say_inline(update, text, markup, parse_mode=ParseMode.HTML)
    elif action == "again":
        await begin_contribution(update, context, brand_id=int(parts[1]), channel=parts[2])
    elif action == "again_new":
        await begin_contribution(update, context)
    elif action == "report":
        brand_id = int(parts[1])
        brand = _brand(service, brand_id)
        if not brand:
            raise CommunityError("Магазин не найден.")
        draft = service.begin(
            user_id,
            stage="report",
            data={
                "brand_id": brand_id,
                "selected_brand_id": brand_id,
                "name": brand.name,
                "dirty": False,
            },
            privileged=service.is_admin(user_id),
        )
        await _render_draft(update, service, draft)
    elif action == "d":
        draft = service.draft(user_id)
        if draft is None or draft.id != parts[1] or draft.version != int(parts[2]):
            raise StaleAction("Кнопка устарела. Продолжите текущий шаг или откройте меню.")
        await _draft_callback(update, context, service, draft, parts[3:])
    elif action in {"mine", "own", "cancel"}:
        raise StaleAction("Кнопка устарела. Откройте /start.")
    elif action == "respond":
        draft = service.respond(user_id, int(parts[1]), int(parts[2]))
        await _render_draft(update, service, draft)
    elif action == "media":
        reference = service.media_for(
            user_id, int(parts[1]), review_version=int(parts[2]) if len(parts) == 3 else None
        )
        if not reference:
            await _say(update, "Скриншот отсутствует или срок хранения истёк.")
        else:
            try:
                await update.effective_message.reply_photo(reference, protect_content=True)
            except TelegramError:
                await _say(
                    update,
                    "Скриншот больше недоступен. При необходимости запросите уточнение у автора.",
                )
    elif action == "queue":
        await _queue(update, service, user_id, min(1000000, max(0, int(parts[1]))))
    elif action == "q":
        await _review_callback(update, context, service, user_id, parts[1:])
    elif action == "manage":
        await _management(update, service, user_id)
    elif action == "recent":
        offset = _history_offset(parts[1]) if len(parts) == 2 else 0
        await _recent_history(update, service, user_id, offset)
    elif action == "history_entry":
        if not service.is_admin(user_id):
            raise CommunityError("История доступна только действующим помощникам.")
        audit_id = int(parts[1])
        offset = _history_offset(parts[2]) if len(parts) == 3 else 0
        entry = service.stores.audit_entry(audit_id)
        if entry is None:
            raise CommunityError("Запись истории больше недоступна.")
        brand = _audit_brand(service, entry)
        if brand is None:
            raise CommunityError("Магазин для этой записи больше недоступен.")
        undo_ids = (
            [entry.id] if not entry.reverted_by and entry.kind not in {"import", "revert"} else []
        )
        draft = service.begin(
            user_id,
            stage="history_entry",
            privileged=True,
            data={
                "editing": True,
                "brand_id": brand.id,
                "audit_id": entry.id,
                "history_ids": [entry.id],
                "history_undo_ids": undo_ids,
                "recent_offset": offset,
            },
        )
        await _render_draft(update, service, draft)
    elif action == "history":
        brand_id = int(parts[1])
        if not _brand(service, brand_id, include_archived=True):
            raise CommunityError("Магазин не найден.")
        draft = service.begin(
            user_id,
            stage="history",
            privileged=True,
            data={"editing": True, "brand_id": brand_id},
        )
        await _render_draft(update, service, draft)
    elif action == "digest":
        if len(parts) != 3 or parts[1] not in {"0", "1"}:
            raise CommunityError("Некорректная настройка.")
        service.set_digest(user_id, parts[1] == "1", expected_epoch=int(parts[2]))
        await _management(update, service, user_id)
    elif action == "volunteer":
        await _request_helper_role(update, service, user_id)
    elif action == "roles":
        offset = max(0, min(1000000, int(parts[1]))) if len(parts) > 1 else 0
        await _role_list(update, service, user_id, offset)
    elif action == "role_view":
        target_id, epoch = int(parts[1]), int(parts[2])
        candidate = next(
            (
                item
                for item in service.role_candidates(user_id)
                if item["user_id"] == target_id and item["epoch"] == epoch
            ),
            None,
        )
        if candidate is None:
            raise StaleAction("Заявка или роль уже изменилась.")
        state = "Действующий помощник" if candidate["active"] else "Заявка в помощники"
        rows = [
            [
                (
                    "Отозвать доступ" if candidate["active"] else "Назначить помощником",
                    f"role:{target_id}:{epoch}:{int(not candidate['active'])}",
                )
            ]
        ]
        if not candidate["active"]:
            rows.append([("Отклонить заявку", f"decline:{target_id}:{epoch}")])
        rows.append([("⬅️ К списку", "roles:0")])
        await _say_inline(update, f"{state}\n\n{_role_identity(candidate)}", _keyboard(rows))
    elif action == "role":
        if parts[3] not in {"0", "1"}:
            raise CommunityError("Некорректная роль.")
        service.set_role(
            user_id,
            int(parts[1]),
            parts[3] == "1",
            expected_epoch=int(parts[2]),
            require_pending=parts[3] == "1",
        )
        delivered = await _notify_role(
            context,
            service,
            int(parts[1]),
            "✅ Вы назначены помощником. Теперь вам доступны очередь предложений "
            "и управление данными.\nЕсли меню не обновилось, вызовите /start."
            if parts[3] == "1"
            else "Доступ помощника отозван. Предложения доступны как обычно.",
        )
        await _say_inline(
            update,
            (
                "Роль обновлена. Пользователь уведомлён."
                if delivered
                else "Роль обновлена, но уведомление пользователю не доставлено."
            )
            + " Вечерняя сводка выключена до нового согласия помощника.",
            _keyboard([[("⬅️ К списку", "roles:0")]]),
        )
    elif action == "decline":
        target_id = int(parts[1])
        service.decline_role(user_id, target_id, int(parts[2]))
        await _notify_role(
            context,
            service,
            target_id,
            "Ваша заявка в помощники пока не принята. Вы можете предлагать данные как обычно.",
        )
        await _say_inline(
            update,
            "Заявка отклонена.",
            _keyboard([[("⬅️ К списку", "roles:0")]]),
        )
    elif action == "edit":
        if len(parts) != 2:
            raise CommunityError(
                "Найдите магазин по названию и нажмите «Редактировать магазин» в его карточке."
            )
        brand_id = int(parts[1])
        if not _brand(service, brand_id):
            raise CommunityError("Магазин не найден.")
        await _begin_form_menu(update, context, service, brand_id)
    else:
        raise CommunityError("Кнопка устарела. Откройте меню заново.")


async def _draft_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    draft: Draft,
    parts: list[str],
) -> None:
    if draft.stage == "form_editor":
        await _form_editor_callback(update, context, service, draft, parts)
        return
    if draft.stage == "form_menu":
        await _form_menu_callback(update, context, service, draft, parts)
        return
    if draft.stage == "form_delete":
        await _form_delete_callback(update, context, service, draft, parts)
        return
    action, stage, data = parts[0], draft.stage, dict(draft.data)
    if action in {"cancel", "close"}:
        if isinstance(data.get("proposal_id"), int):
            released = service.cancel_review_draft(draft.user_id, draft.id, draft.version)
            if draft.data.get("draft_mode"):
                await _say(
                    update,
                    "Разбор отменён." if released is not None else "Разбор уже изменился.",
                    keyboard_for(service, draft.user_id),
                )
            await _queue(
                update,
                service,
                draft.user_id,
                notice=(
                    "Разбор отменён. Заявка возвращена в очередь."
                    if released is not None
                    else "Разбор уже изменился. Черновик закрыт."
                ),
            )
            return
        meaningful = bool(data.get("dirty")) or service.draft_has_media(draft.user_id, draft.id)
        if meaningful and stage != "cancel_confirm":
            data["cancel_stage"] = stage
            draft = service.advance(draft.user_id, draft.id, draft.version, "cancel_confirm", data)
            await _render_draft(update, service, draft)
            return
        await _close_draft(update, context, service, draft)
        return
    if action == "cancel_yes" and stage == "cancel_confirm":
        await _close_draft(update, context, service, draft)
        return
    if action == "cancel_no" and stage == "cancel_confirm":
        previous = data.pop("cancel_stage", None)
        if not isinstance(previous, str) or previous == "cancel_confirm":
            raise StaleAction("Не удалось восстановить предыдущий шаг.")
        draft = service.advance(draft.user_id, draft.id, draft.version, previous, data)
        await _render_draft(update, service, draft)
        return
    if action == "resume":
        await _render_draft(update, service, draft)
        return
    if action == "submit" and stage == "preview":
        proposal = service.submit(draft.user_id, draft.id, draft.version)
        if proposal.status != "approved":
            await _say(
                update,
                "Спасибо! Отправлено на проверку. Если понадобится уточнение, бот напишет вам.",
                keyboard_for(service, draft.user_id),
            )
            return
        brand_id = data.get("brand_id")
        if brand_id is None:
            result = service.stores.search(data.get("name", ""), limit=10)
            exact = [brand for brand in result.matches if brand.name == data.get("name")]
            if len(exact) == 1:
                brand_id = exact[0].id
        rows: list[list[tuple[str, str]]] = []
        if brand_id:
            rows.append([("🏪 Открыть магазин", f"open_brand:{brand_id}")])
        await _say_with_restored_menu(
            update,
            "Спасибо за добавление!",
            keyboard_for(service, draft.user_id),
            _keyboard(rows) if rows else None,
        )
        return
    if action == "decision" and stage == "review_preview":
        proposal = service.review(
            draft.user_id,
            data["proposal_id"],
            data["proposal_version"],
            data["decision"],
            reason=data["reason"],
        )
        service.cancel_draft(draft.user_id, draft.id, draft.version)
        await _notify(context, proposal)
        await _say(update, "Решение сохранено.", keyboard_for(service, draft.user_id))
        return
    if action == "back":
        history = data.get("back", [])
        if not history:
            raise StaleAction("Назад с этого шага недоступно.")
        stage = history[-1]
        data["back"] = history[:-1]
        draft = service.advance(draft.user_id, draft.id, draft.version, stage, data)
        await _render_draft(update, service, draft)
        return
    if action == "select" and stage in {
        "choose",
        "store_choose",
        "partner_store_choose",
        "edit_choose",
        "target_choose",
        "preview_choose",
    }:
        brand_id = int(parts[1])
        if brand_id not in data.get("matches", []):
            raise StaleAction("Выберите магазин из текущих результатов.")
        brand = _brand(service, brand_id)
        if not brand:
            raise CommunityError("Магазин больше недоступен.")
        if stage == "target_choose":
            data.pop("expected", None)
            data["payload"] = {"brand_id": data["brand_id"], "target_id": brand_id}
            data["dirty"] = True
            stage = "preview" if data.get("editing") else "comment"
        elif stage == "preview_choose":
            data.update(brand_id=brand.id, selected_brand_id=brand.id, name=brand.name)
            member = (
                _member_for_channel(service, brand.id, data["channel"])
                if data.get("channel") in {"offline", "online"}
                else None
            )
            if member:
                data["merchant_id"] = member.id
            else:
                data.pop("merchant_id", None)
            _sync_mcc_payload(data)
            stage = "preview"
        else:
            data.update(brand_id=brand.id, selected_brand_id=brand.id, name=brand.name)
            if stage == "edit_choose":
                stage = "editor"
            elif stage == "partner_store_choose":
                stage = "partner_card"
            else:
                stage = "channel"
    elif action == "new" and stage in {"choose", "preview_choose"}:
        data.pop("brand_id", None)
        data.pop("selected_brand_id", None)
        data.pop("merchant_id", None)
        if stage == "preview_choose":
            data["name"] = data.pop("candidate_name")
            _sync_mcc_payload(data)
            stage = "preview"
        else:
            stage = "channel"
    elif action == "channel" and stage == "channel":
        channel = parts[1]
        if channel not in {"offline", "online", "both"}:
            raise CommunityError("Выберите способ оплаты.")
        data["channel"] = channel
        if data.get("brand_id") and channel in {"offline", "online"}:
            member = _member_for_channel(service, data["brand_id"], channel)
            if member:
                data["merchant_id"] = member.id
            else:
                data.pop("merchant_id", None)
        if data.pop("editing_payment", False):
            stage = "mcc"
        elif data.get("mcc"):
            _sync_mcc_payload(data)
            stage = "preview"
        else:
            stage = "mcc"
    elif action == "partner_card_page" and stage == "partner_card":
        offset = int(parts[1])
        cards = data.get("cards", [])
        if offset < 0 or offset % 10 or offset >= len(cards):
            raise StaleAction("Страница карт недоступна.")
        data["card_offset"] = offset
    elif action == "partner_card" and stage == "partner_card":
        index = int(parts[1])
        cards = data.get("cards", [])
        if not 0 <= index < len(cards):
            raise StaleAction("Карта больше недоступна в текущем списке.")
        data["card_id"] = cards[index]["id"]
        data["card_name"] = cards[index]["name"]
        data["dirty"] = True
        stage = "partner_channel"
    elif action == "partner_channel" and stage == "partner_channel":
        if parts[1] not in {"offline", "online", "any"}:
            raise CommunityError("Выберите способ оплаты.")
        data["channel"] = parts[1]
        stage = "partner_mode"
    elif action == "partner_mode" and stage == "partner_mode":
        if parts[1] not in {"additional", "total"}:
            raise CommunityError("Выберите способ учёта выгоды.")
        data["mode"] = parts[1]
        stage = "partner_reward_kind"
    elif action == "partner_reward" and stage == "partner_reward_kind":
        if parts[1] not in {"cash", "points"}:
            raise CommunityError("Выберите денежный возврат или баллы.")
        data["reward_kind"] = parts[1]
        stage = "partner_value"
    elif action == "skip" and stage in {
        "comment",
        "private_comment",
        "note",
        "evidence",
        "response_evidence",
    }:
        if stage in {"comment", "private_comment"}:
            data["comment"] = ""
        elif stage == "note":
            if data.get("note"):
                data["dirty"] = True
            data["note"] = ""
            _sync_mcc_payload(data)
        stage = "preview"
    elif action == "source" and stage in {"evidence", "preview"}:
        stage = "source_url"
    elif action == "source_screenshot" and stage == "source_url":
        stage = "evidence"
    elif action == "preview" and stage == "preview":
        field = parts[1]
        if data.get("editing"):
            allowed_fields = (
                {"payment", "channel", "mcc", "more", "note", "evidence", "source"}
                if data.get("kind") in {"add_mcc", "add_mcc_both"} and not data.get("selected_mcc")
                else {"note"}
            )
            if field not in allowed_fields:
                raise StaleAction("Поле недоступно.")
        if field == "brand":
            stage = "preview_brand"
        elif field in {"payment", "channel", "mcc"}:
            data["editing_payment"] = True
            stage = "channel"
        elif field == "more":
            stage = "preview_more"
        elif field in {"note", "evidence", "comment", "source"}:
            # Keep buttons rendered by the previous release usable.
            stage = {
                "note": "note",
                "evidence": "evidence",
                "comment": "private_comment",
                "source": "source_url",
            }[field]
        else:
            raise StaleAction("Поле недоступно.")
    elif action == "more" and stage == "preview_more":
        field = parts[1]
        if field == "aliases" and not data.get("brand_id"):
            stage = "new_aliases"
        elif field == "note":
            stage = "note"
        elif field == "evidence":
            stage = "evidence"
        elif field == "source":
            stage = "source_url"
        elif field == "comment" and not service.is_admin(draft.user_id):
            stage = "private_comment"
        else:
            raise StaleAction("Поле недоступно.")
    elif action == "mcc_actions" and stage == "editor" and service.is_admin(draft.user_id):
        data["fact_offset"] = 0
        stage = "mcc_facts"
    elif action == "brand_actions" and stage == "editor" and service.is_admin(draft.user_id):
        stage = "brand_editor"
    elif action == "operation" and stage in {"report", "editor", "brand_editor"}:
        kind = parts[1]
        allowed = {"add_mcc"} if stage in {"report", "editor"} else {"names", "merge_brand"}
        if kind not in allowed or (
            stage in {"editor", "brand_editor"} and not service.is_admin(draft.user_id)
        ):
            raise CommunityError("Изменение недоступно.")
        data["kind"] = kind
        data.pop("expected", None)
        data.pop("payload", None)
        data.pop("comment", None)
        if kind == "names":
            stage = "brand_names"
        elif kind == "merge_brand":
            stage = "target_name"
        elif kind == "add_mcc":
            for key in (
                "channel",
                "merchant_id",
                "mcc",
                "note",
                "old_mcc",
                "selected_mcc",
                "original_note",
                "merchant_ids",
                "channels",
                "combined_channels",
                "combined_merchant_ids",
            ):
                data.pop(key, None)
            stage = "channel"
        else:
            raise CommunityError("Изменение недоступно.")
    elif action in {"name_primary", "name_add", "name_alias"} and stage == "brand_names":
        if action == "name_primary":
            data["name_mode"] = "primary"
            stage = "name_value"
        elif action == "name_add":
            data["name_mode"] = "add"
            stage = "name_value"
        else:
            index = int(parts[1])
            brand = _brand(service, data["brand_id"], include_archived=True)
            if brand is None or not 0 <= index < len(brand.aliases):
                raise StaleAction("Название уже изменилось. Откройте список заново.")
            data["name_index"] = index
            stage = "alias_actions"
    elif action in {"name_promote", "name_edit", "name_delete"} and stage == "alias_actions":
        brand = _brand(service, data["brand_id"], include_archived=True)
        index = data.get("name_index")
        if brand is None or not isinstance(index, int) or not 0 <= index < len(brand.aliases):
            raise StaleAction("Название уже изменилось. Откройте список заново.")
        if action == "name_edit":
            data["name_mode"] = "alias"
            stage = "name_value"
        else:
            aliases = list(brand.aliases)
            selected = aliases.pop(index)
            name = brand.name
            if action == "name_promote":
                aliases.insert(0, name)
                name = selected
            data["kind"] = "edit_brand_names"
            data["payload"] = {"brand_id": brand.id, "name": name, "aliases": aliases}
            data["dirty"] = True
            stage = "preview"
    elif action == "fact_page" and stage == "mcc_facts":
        offset = int(parts[1])
        items = _brand_fact_items(service, data["brand_id"])
        if (
            offset < 0
            or offset % FACT_PAGE_SIZE
            or offset >= max(1, len(items))
            or abs(offset - int(data.get("fact_offset", 0))) > FACT_PAGE_SIZE
        ):
            raise StaleAction("Страница MCC недоступна.")
        data["fact_offset"] = offset
    elif action == "fact" and stage == "mcc_facts":
        selector, mcc = parts[1], parts[2]
        if selector in {"offline", "online", "both"}:
            channel = selector
            facts = (
                _brand_fact_groups(service, data["brand_id"])
                if channel == "both"
                else _brand_facts(service, data["brand_id"], channel)
            )
            fact = next(
                (fact for fact in facts if fact.mcc == mcc and fact.channel == channel), None
            )
        else:
            # Keep already-rendered buttons from the previous release usable.
            legacy_id = int(selector)
            member = next(
                (
                    item
                    for item in _brand_members(service, data["brand_id"])
                    if item.id == legacy_id
                ),
                None,
            )
            channel = member.channel if member else ""
            fact = next(
                (
                    item
                    for item in _brand_facts(service, data["brand_id"], channel)
                    if item.mcc == mcc and legacy_id in item.merchant_ids
                ),
                None,
            )
        if fact is None:
            raise StaleAction("MCC уже изменился. Откройте список заново.")
        merchant_id = fact.merchant_ids[0]
        data.pop("expected", None)
        data.update(
            merchant_id=merchant_id,
            merchant_ids=list(fact.merchant_ids),
            channels=list(getattr(fact, "channels", (channel,))),
            channel=channel,
            selected_mcc=mcc,
            original_note=getattr(fact, "note", ""),
            note=getattr(fact, "note", ""),
        )
        for key in ("kind", "payload", "old_mcc", "mcc"):
            data.pop(key, None)
        stage = "mcc_fact"
    elif action == "fact_channel" and stage == "mcc_fact":
        channel = parts[1]
        selected = _selected_fact(service, data)
        if (
            data.get("channel") != "both"
            or selected is None
            or channel not in getattr(selected, "channels", ())
            or list(selected.merchant_ids) != data.get("merchant_ids")
            or getattr(selected, "note", "") != data.get("original_note", "")
        ):
            raise StaleAction("MCC или его подпись уже изменились. Откройте список заново.")
        fact = next(
            (
                fact
                for fact in _brand_facts(service, data["brand_id"], channel)
                if fact.mcc == data["selected_mcc"]
                and getattr(fact, "note", "") == data.get("original_note", "")
            ),
            None,
        )
        if fact is None:
            raise StaleAction("MCC уже изменился. Откройте список заново.")
        data.update(
            combined_channels=list(selected.channels),
            combined_merchant_ids=list(selected.merchant_ids),
            merchant_id=fact.merchant_ids[0],
            merchant_ids=list(fact.merchant_ids),
            channels=[channel],
            channel=channel,
        )
    elif action == "fact_all" and stage == "mcc_fact":
        combined_channels = data.get("combined_channels")
        combined_ids = data.get("combined_merchant_ids")
        fact = next(
            (
                fact
                for fact in _brand_fact_groups(service, data["brand_id"])
                if fact.channel == "both"
                and fact.mcc == data.get("selected_mcc")
                and getattr(fact, "note", "") == data.get("original_note", "")
            ),
            None,
        )
        if (
            combined_channels != ["offline", "online"]
            or fact is None
            or list(fact.channels) != combined_channels
            or list(fact.merchant_ids) != combined_ids
        ):
            raise StaleAction("MCC или его подпись уже изменились. Откройте список заново.")
        data.update(
            merchant_id=fact.merchant_ids[0],
            merchant_ids=list(fact.merchant_ids),
            channels=list(fact.channels),
            channel="both",
        )
        data.pop("combined_channels", None)
        data.pop("combined_merchant_ids", None)
    elif action in {"fact_replace", "fact_note", "fact_remove"} and stage == "mcc_fact":
        fact = _selected_fact(service, data)
        if (
            fact is None
            or list(fact.merchant_ids) != data.get("merchant_ids")
            or list(getattr(fact, "channels", (data.get("channel"),))) != data.get("channels")
            or getattr(fact, "note", "") != data.get("original_note", "")
        ):
            raise StaleAction("MCC или его подпись уже изменились. Откройте список заново.")
        if action == "fact_replace":
            data.update(
                kind="replace_mcc",
                old_mcc=data["selected_mcc"],
                note=data.get("original_note", ""),
            )
            data.pop("mcc", None)
            data.pop("payload", None)
            stage = "mcc"
        elif action == "fact_note":
            data.update(
                kind="edit_mcc_note",
                mcc=data["selected_mcc"],
                note=data.get("original_note", ""),
            )
            _sync_mcc_payload(data)
            stage = "note"
        else:
            data.update(kind="archive_mcc", dirty=True)
            data["payload"] = {
                "merchant_id": data["merchant_id"],
                "merchant_ids": list(data["merchant_ids"]),
                "mcc": data["selected_mcc"],
            }
            if len(data.get("channels", [])) > 1:
                data["payload"]["channels"] = list(data["channels"])
            stage = "preview"
    elif action in {"note_keep", "note_edit", "note_remove"} and stage == "note_choice":
        if action == "note_edit":
            stage = "note"
        else:
            if action == "note_remove" and data.get("note"):
                data["note"] = ""
                data["dirty"] = True
            _sync_mcc_payload(data)
            stage = "preview"
    elif action == "history" and stage == "brand_editor" and service.is_admin(draft.user_id):
        data["history_offset"] = 0
        stage = "history"
    elif action == "history_page" and stage == "history" and service.is_admin(draft.user_id):
        offset = _history_offset(parts[1])
        current = data.get("history_offset", 0)
        previous = current > 0 and offset == current - HISTORY_PAGE_SIZE
        following = data.get("history_has_next") and offset == current + HISTORY_PAGE_SIZE
        if not previous and not following:
            raise StaleAction("Выберите соседнюю страницу кнопкой из текущей истории.")
        data["history_offset"] = offset
    elif action == "entry" and stage == "history" and service.is_admin(draft.user_id):
        audit_id = int(parts[1])
        if audit_id not in data.get("history_ids", []):
            raise StaleAction("Запись не показана на текущей странице истории.")
        data["audit_id"] = audit_id
        stage = "history_entry"
    elif action == "recent_back" and stage == "history_entry" and service.is_admin(draft.user_id):
        offset = _history_offset(str(data.get("recent_offset", 0)))
        service.cancel_draft(draft.user_id, draft.id, draft.version)
        await _recent_history(update, service, draft.user_id, offset)
        return
    elif (
        action == "undo"
        and stage in {"history", "history_entry"}
        and service.is_admin(draft.user_id)
    ):
        audit_id = int(parts[1])
        if audit_id not in data.get("history_undo_ids", []):
            raise StaleAction("Это изменение не показано в текущей истории.")
        entry = service.stores.audit_entry(audit_id)
        if entry is None or entry.reverted_by or entry.kind in {"import", "revert"}:
            raise StaleAction("Изменение недоступно для отмены.")
        data["kind"] = "revert"
        data.pop("expected", None)
        data["payload"] = {"audit_id": audit_id}
        data["dirty"] = True
        stage = "preview"
    else:
        raise StaleAction("Кнопка не относится к текущему шагу.")
    draft = _advance(service, draft, stage, data)
    await _render_draft(update, service, draft)


def _history_offset(value: str) -> int:
    """Validate a bounded page offset rather than accepting arbitrary SQL offsets."""

    offset = int(value)
    if not 0 <= offset <= MAX_HISTORY_OFFSET or offset % HISTORY_PAGE_SIZE:
        raise StaleAction("Страница истории недоступна. Откройте журнал заново.")
    return offset


async def _review_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    user_id: int,
    parts: list[str],
) -> None:
    proposal_id, version, action = int(parts[0]), int(parts[1]), parts[2]
    if not service.is_admin(user_id):
        raise CommunityError("Доступ к разбору отозван.")
    if action == "claim":
        proposal = service.claim(user_id, proposal_id, version)
        await _review_view(update, service, proposal)
        return
    if action == "renew":
        raise StaleAction("Продление резерва больше недоступно. Завершите разбор за 15 минут.")
    if action == "release":
        service.release_review(user_id, proposal_id, version)
        await _queue(
            update,
            service,
            user_id,
            notice="Разбор отменён. Заявка возвращена в очередь.",
        )
        return
    proposal = service.proposal(user_id, proposal_id)
    if (
        proposal.version != version
        or proposal.status != "pending"
        or proposal.reviewer_id != user_id
    ):
        raise StaleAction("Предложение уже изменилось. Откройте очередь.")
    if action == "view":
        await _review_view(update, service, proposal)
    elif action == "edit" and proposal.kind in {"store_metadata", "mcc_save", "partner_save"}:
        if proposal.kind == "store_metadata":
            form = "store_metadata"
        elif proposal.kind == "partner_save":
            form = "partner_save"
        else:
            form = "store_create" if "name" in proposal.payload else "mcc_save"
        values = dict(proposal.payload)
        data = _new_form_data(context, service, form, values=values)
        data["review_edit"] = {
            "proposal_id": proposal.id,
            "proposal_version": proposal.version,
        }
        draft = service.begin(user_id, stage="form_editor", data=data, privileged=True)
        await _render_form_editor(update, context, service, draft)
    elif action in {"approve", "replace_confirm"}:
        proposal = service.review(
            user_id,
            proposal_id,
            version,
            "approved",
            replace_old=parts[3] if action == "replace_confirm" else None,
        )
        await _notify(context, proposal)
        await _queue(
            update,
            service,
            user_id,
            notice="Предложение принято и сохранено в базе.",
        )
    elif action == "replace" and proposal.kind == "add_mcc":
        service.validate_review(user_id, proposal_id, version)
        rows = [
            [
                (
                    f"Заменить {fact.mcc} на {proposal.payload['mcc']}",
                    f"q:{proposal.id}:{proposal.version}:replace_confirm:{fact.mcc}",
                )
            ]
            for fact in service.stores.list_mcc(proposal.payload["merchant_id"])
            if fact.mcc != proposal.payload["mcc"]
        ]
        rows.append(
            [
                (
                    "⬅️ Назад к заявке",
                    f"q:{proposal.id}:{proposal.version}:view",
                )
            ]
        )
        await _say_inline(
            update,
            "Какой старый MCC подтверждён как ошибочный? Кнопка сохранит замену."
            if rows
            else "Нет другого MCC для замены.",
            _keyboard(rows),
        )
    elif action == "reject":
        service.review(user_id, proposal_id, version, "rejected")
        await _queue(
            update,
            service,
            user_id,
            notice="Предложение отклонено.",
        )
    elif action == "reject_reason":
        service.validate_review(user_id, proposal_id, version)
        draft = service.begin(
            user_id,
            stage="reason",
            privileged=True,
            data={
                "proposal_id": proposal_id,
                "proposal_version": version,
                "decision": "rejected",
                "draft_mode": True,
            },
        )
        await _render_draft(update, service, draft)
    elif action == "clarify":
        service.validate_review(user_id, proposal_id, version)
        draft = service.begin(
            user_id,
            stage="reason",
            privileged=True,
            data={
                "proposal_id": proposal_id,
                "proposal_version": version,
                "decision": "clarification",
                "draft_mode": True,
            },
        )
        await _render_draft(update, service, draft)
    else:
        raise CommunityError("Действие недоступно.")
