"""Private Telegram contribution, moderation and management conversations."""

# Russian UI text and ordinary Unicode menu emojis are intentional.
# ruff: noqa: RUF001

from __future__ import annotations

import logging
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .community import (
    FINAL_STATES,
    MAX_MEDIA_BYTES,
    MAX_NAME,
    CommunityError,
    CommunityService,
    Draft,
    Proposal,
    StaleAction,
    clean_text,
)
from .formatting import format_limits, split_message

LOGGER = logging.getLogger(__name__)
INFO = "ℹ️ Информация по картам"
SUGGEST = "➕ Предложить MCC магазина"
ADD = "➕ Добавить MCC магазина"
MINE = "👤 Мои предложения"
QUEUE = "📋 Разобрать очередь"
MANAGE = "⚙️ Управление"
HISTORY_PAGE_SIZE = 10
MAX_HISTORY_OFFSET = 1000000
STATUS = {
    "pending": "ожидает проверки",
    "approved": "принято",
    "rejected": "отклонено",
    "clarification": "нужно уточнение",
    "cancelled": "отменено",
}
KINDS = {
    "add_merchant": "Добавить магазин и MCC",
    "add_mcc": "Добавить MCC",
    "replace_mcc": "Заменить ошибочный MCC",
    "rename_merchant": "Исправить название",
    "merge_merchant": "Объединить дубль",
    "aliases": "Изменить другие названия",
    "archive_merchant": "Убрать магазин из поиска",
    "archive_mcc": "Убрать ошибочный MCC",
    "revert": "Отменить изменение",
}
HISTORY_KINDS = {**KINDS, "import": "Импорт данных из tannei.by"}


def _service(context: ContextTypes.DEFAULT_TYPE) -> CommunityService:
    return context.application.bot_data["community"]


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

    rows = (
        [[INFO], [ADD], [QUEUE], [MANAGE]]
        if service.is_admin(user_id)
        else [[INFO], [SUGGEST], [MINE]]
    )
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


async def _say(update: Update, text: str, markup: Any = None) -> None:
    if update.effective_message is not None:
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            await update.effective_message.reply_text(
                chunk, reply_markup=markup if index == len(chunks) - 1 else None
            )


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show a private role-aware start menu without clearing a durable draft."""

    user_id = _identity(update)
    if user_id is None:
        await _say(update, "Предложения и управление доступны в личном чате с ботом.")
        return
    service = _service(context)
    await _say(
        update,
        "Отправьте четырёхзначный MCC или название магазина.\n"
        "Например: 5411 или Евроопт.\n"
        "Данные магазинов пополняют пользователи; проверяйте MCC перед покупкой.",
        keyboard_for(service, user_id),
    )
    draft = service.draft(user_id)
    if draft:
        await _say(
            update,
            "У вас есть незавершённое действие.",
            _keyboard(
                [
                    [
                        ("Продолжить", f"d:{draft.id}:{draft.version}:resume"),
                        ("Отменить", f"d:{draft.id}:{draft.version}:cancel"),
                    ]
                ]
            ),
        )


def _draft_buttons(
    draft: Draft, rows: list[list[tuple[str, str]]] | None = None
) -> InlineKeyboardMarkup:
    prefix = f"d:{draft.id}:{draft.version}:"
    result = [[(label, prefix + action) for label, action in row] for row in (rows or [])]
    navigation = []
    if draft.stage != "name":
        navigation.append(("⬅️ Назад", prefix + "back"))
    navigation.append(("Отмена", prefix + "cancel"))
    result.append(navigation)
    return _keyboard(result)


def _display_payload(service: CommunityService, kind: str, payload: dict[str, Any]) -> str:
    parts = [KINDS.get(kind, "Предложение")]
    merchant_id = payload.get("merchant_id")
    if merchant_id:
        merchant = service.stores.get(merchant_id, include_archived=True)
        parts.append(f"Магазин: {merchant.name if merchant else 'не найден'} (№{merchant_id})")
        if merchant:
            parts.append("Канал: " + ("онлайн" if merchant.channel == "online" else "офлайн"))
    if "name" in payload:
        parts.append("Название: " + payload["name"])
    if "channel" in payload:
        parts.append("Канал: " + ("онлайн" if payload["channel"] == "online" else "офлайн"))
    if "old_mcc" in payload:
        parts.append("Старый MCC: " + payload["old_mcc"])
    if "mcc" in payload:
        parts.append("MCC: " + payload["mcc"])
    if "target_id" in payload:
        target = service.stores.get(payload["target_id"], include_archived=True)
        parts.append(
            f"Оставить: {target.name if target else 'не найден'} (№{payload['target_id']})"
        )
    if "aliases" in payload:
        parts.append("Другие названия: " + (", ".join(payload["aliases"]) or "нет"))
    if "audit_id" in payload:
        parts.append(f"Изменение №{payload['audit_id']}. Независимые подтверждения сохранятся.")
    return "\n".join(parts)


async def _render_draft(update: Update, service: CommunityService, draft: Draft) -> None:
    stage, data = draft.stage, draft.data
    if stage in {"reason", "review_preview"}:
        service.touch_review(draft.user_id, data["proposal_id"], data["proposal_version"])
    rows: list[list[tuple[str, str]]] = []
    if stage == "name":
        text = "Как называется магазин? Введите название (до 160 символов)."
    elif stage in {"choose", "edit_choose", "target_choose"}:
        text = "Выберите магазин. Если нужного нет, уточните название сообщением."
        for merchant_id in data.get("matches", [])[:10]:
            merchant = service.stores.get(merchant_id)
            if merchant:
                channel = "онлайн" if merchant.channel == "online" else "офлайн"
                rows.append(
                    [
                        (
                            f"{merchant.name[:48]} · {channel} · №{merchant.id}",
                            f"select:{merchant.id}",
                        )
                    ]
                )
        if stage == "choose":
            rows.append([("Создать новый магазин", "new")])
    elif stage == "channel":
        text = f"Где оплачена покупка в «{data['name']}»?"
        rows = [[("🏬 Офлайн", "channel:offline"), ("🌐 Онлайн", "channel:online")]]
    elif stage == "mcc":
        text = "Введите MCC покупки: ровно четыре цифры."
    elif stage in {"evidence", "response_evidence"}:
        text = (
            "Пришлите скриншот операции из банка, где видны магазин и MCC.\n"
            "Заранее закройте имя, номер карты, баланс и другие личные данные. "
            "Можно добавить подпись до 1000 символов. Изображение — до 10 МБ.\n"
            "Скриншот увидят только вы и действующие помощники; ссылка удалится "
            "через 5 дней после завершения проверки."
        )
        if service.is_admin(draft.user_id) or stage == "response_evidence":
            rows = [[("Без нового скриншота", "skip")]]
    elif stage == "comment":
        text = (
            "Добавьте комментарий: например, дата покупки или адрес. Не указывайте личные данные."
        )
        rows = [[("Без комментария", "skip")]]
    elif stage == "response":
        text = "Напишите уточнение для помощника (до 1000 символов)."
    elif stage == "preview":
        text = "Проверьте перед отправкой:\n\n" + _display_payload(
            service, data["kind"], data["payload"]
        )
        if data.get("comment"):
            text += "\n\nКомментарий: " + data["comment"]
        direct = service.is_admin(draft.user_id) and not data.get("response_id")
        rows = [[("Сохранить в базу" if direct else "Отправить на проверку", "submit")]]
    elif stage == "report":
        text = "Что нужно исправить? Для названия или дубля скриншот не нужен."
        rows = [
            [("Добавить или подтвердить MCC", "operation:add_mcc")],
            [("Заменить ошибочный MCC", "operation:replace_mcc")],
            [("Название магазина", "operation:rename_merchant")],
            [("Это дубль другого магазина", "operation:merge_merchant")],
        ]
    elif stage == "edit_name":
        text = "Введите название магазина для редактирования."
    elif stage == "editor":
        merchant = service.stores.get(data["merchant_id"], include_archived=True)
        text = f"Редактирование: {merchant.name if merchant else 'магазин недоступен'}"
        rows = [
            [("Название", "operation:rename_merchant"), ("Другие названия", "operation:aliases")],
            [("Добавить MCC", "operation:add_mcc"), ("Заменить MCC", "operation:replace_mcc")],
            [
                ("Убрать MCC", "operation:archive_mcc"),
                ("Объединить дубль", "operation:merge_merchant"),
            ],
            [("Убрать магазин", "operation:archive_merchant"), ("История / отмена", "history")],
        ]
    elif stage == "edit_value":
        text = {
            "rename_merchant": "Введите правильное название магазина.",
            "aliases": "Введите другие названия через запятую (до 20). "
            "Для удаления всех отправьте «-».",
        }[data["kind"]]
    elif stage == "old_mcc":
        text = "Выберите ошибочный MCC, который нужно заменить или убрать."
        rows = [
            [(fact.mcc, "old:" + fact.mcc)] for fact in service.stores.list_mcc(data["merchant_id"])
        ]
        if not rows:
            text = "У магазина пока нет MCC. Вернитесь назад и добавьте его."
    elif stage == "target_name":
        text = "Введите название магазина, который нужно оставить после объединения."
    elif stage == "history":
        offset = data.get("history_offset", 0)
        merchant = service.stores.get(data["merchant_id"], include_archived=True)
        entries = service.stores.history(
            data["merchant_id"], limit=HISTORY_PAGE_SIZE + 1, offset=offset
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
        channel = "онлайн/приложение" if merchant and merchant.channel == "online" else "обычный"
        name = merchant.name if merchant else f"магазин №{data['merchant_id']}"
        text = (
            f"История «{name}» · {channel} · страница {offset // HISTORY_PAGE_SIZE + 1}.\n"
            "Отмена сохранит более поздние независимые подтверждения."
        )
        for entry in page:
            text += f"\n\n№{entry.id}: {HISTORY_KINDS.get(entry.kind, entry.kind)}"
            for detail in entry.details:
                text += f"\n• {detail}"
            text += (
                f"\nИзменил: {_audit_identity(service.audit_actor(draft.user_id, entry.actor_id))}"
            )
            if entry.id in undo_ids:
                rows.append([(f"Отменить изменение №{entry.id}", f"undo:{entry.id}")])
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
    elif stage == "reason":
        text = (
            "Напишите причину отказа."
            if data["decision"] == "rejected"
            else "Что нужно уточнить у автора?"
        )
    elif stage == "review_preview":
        text = "Проверьте сообщение автору:\n\n" + data["reason"]
        rows = [[("Отправить решение", "decision")]]
    else:
        raise CommunityError("Неизвестный шаг. Отмените действие и начните заново.")
    await _say(update, text, _draft_buttons(draft, rows))


async def _mine(update: Update, service: CommunityService, user_id: int, offset: int = 0) -> None:
    proposals = service.own_proposals(user_id, offset=offset)
    text = "Ваши предложения" if proposals else "Пока нет предложений."
    rows = []
    for proposal in proposals:
        rows.append([(f"№{proposal.id} · {STATUS[proposal.status]}", f"own:{proposal.id}")])
    if offset:
        rows.append([("⬅️ Назад", f"mine:{max(0, offset - 10)}")])
    if len(proposals) == 10:
        rows.append([("Дальше ➡️", f"mine:{offset + 10}")])
    if not service.is_admin(user_id):
        rows.append([("🙋 Хочу помогать", "volunteer")])
    await _say(update, text, _keyboard(rows))


async def _own(update: Update, service: CommunityService, user_id: int, proposal_id: int) -> None:
    proposal = service.proposal(user_id, proposal_id)
    if proposal.user_id != user_id:
        raise CommunityError("Откройте предложение через очередь.")
    text = f"Предложение №{proposal.id}: {STATUS[proposal.status]}\n\n" + _display_payload(
        service, proposal.kind, proposal.payload
    )
    if proposal.comment:
        text += "\nКомментарий: " + proposal.comment
    if proposal.reason:
        text += "\nОтвет помощника: " + proposal.reason
    rows = []
    if proposal.status == "clarification":
        rows.append([("Ответить на уточнение", f"respond:{proposal.id}:{proposal.version}")])
    if proposal.status not in FINAL_STATES:
        rows.append([("Отменить предложение", f"cancel:{proposal.id}:{proposal.version}")])
    rows.extend([[("Скриншот", f"media:{proposal.id}")], [("Мои предложения", "mine:0")]])
    await _say(update, text, _keyboard(rows))


async def _queue(update: Update, service: CommunityService, user_id: int, offset: int = 0) -> None:
    proposals = service.queue(user_id, offset=offset)
    rows = [
        [(f"№{proposal.id} · {KINDS[proposal.kind]}", f"q:{proposal.id}:{proposal.version}:claim")]
        for proposal in proposals
    ]
    if offset:
        rows.append([("⬅️ Назад", f"queue:{max(0, offset - 10)}")])
    if len(proposals) == 10:
        rows.append([("Дальше ➡️", f"queue:{offset + 10}")])
    await _say(
        update,
        "Выберите предложение для разбора." if proposals else "Очередь пуста.",
        _keyboard(rows),
    )


async def _review_view(update: Update, service: CommunityService, proposal: Proposal) -> None:
    prefix = f"q:{proposal.id}:{proposal.version}:"
    text = f"Разбор №{proposal.id} · резерв на 15 минут\n\n" + _display_payload(
        service, proposal.kind, proposal.payload
    )
    if proposal.comment:
        text += "\nКомментарий: " + proposal.comment
    if proposal.reason:
        text += "\nПредыдущее уточнение: " + proposal.reason
    approve = "Добавить как ещё один MCC" if proposal.kind == "add_mcc" else "Принять"
    rows = [[(approve, prefix + "approve")]]
    if proposal.kind == "add_mcc":
        rows.append([("Заменить ошибочный MCC", prefix + "replace")])
    rows.extend(
        [
            [("Отклонить", prefix + "reject"), ("Уточнить", prefix + "clarify")],
            [
                ("Скриншот", f"media:{proposal.id}:{proposal.version}"),
                ("Продлить резерв", prefix + "renew"),
            ],
            [("Очередь", "queue:0")],
        ]
    )
    await _say(update, text, _keyboard(rows))


async def _management(update: Update, service: CommunityService, user_id: int) -> None:
    if not service.is_admin(user_id):
        raise CommunityError("Управление доступно только действующим помощникам.")
    enabled = service.digest_enabled(user_id)
    rows = [
        [("Редактировать магазин", "edit")],
        [("Журнал изменений и восстановление", "recent")],
        [(MINE, "mine:0")],
        [
            (
                "🔔 Отключить сводку" if enabled else "🔕 Включить сводку в 20:00",
                f"digest:{int(not enabled)}:{service.role_epoch(user_id)}",
            )
        ],
    ]
    if service.role(user_id) == "owner":
        rows.append([("Помощники и заявки", "roles")])
    await _say(
        update,
        "Управление магазинами. Сводка сообщает только число предложений и не содержит скриншотов.",
        _keyboard(rows),
    )


async def _notify(context: ContextTypes.DEFAULT_TYPE, proposal: Proposal) -> None:
    rows = (
        [[("Ответить на уточнение", f"respond:{proposal.id}:{proposal.version}")]]
        if proposal.status == "clarification"
        else [[("Мои предложения", "mine:0")]]
    )
    text = f"Ваше предложение №{proposal.id}: {STATUS[proposal.status]}."
    if proposal.reason:
        text += "\n" + proposal.reason
    try:
        await context.bot.send_message(
            chat_id=proposal.user_id, text=text, reply_markup=_keyboard(rows)
        )
    except TelegramError:
        LOGGER.info("Could not deliver a contribution decision; it remains in own proposals")


async def _notify_role(
    context: ContextTypes.DEFAULT_TYPE, service: CommunityService, user_id: int, text: str
) -> None:
    try:
        await context.bot.send_message(
            chat_id=user_id, text=text, reply_markup=keyboard_for(service, user_id)
        )
    except TelegramError:
        LOGGER.info("Could not deliver role notification; role change remains committed")


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


async def _begin_contribution(
    update: Update, service: CommunityService, user_id: int, merchant_id: int | None = None
) -> None:
    data: dict[str, Any] = {}
    stage = "name"
    if merchant_id is not None:
        merchant = service.stores.get(merchant_id)
        if merchant is None:
            raise CommunityError("Магазин больше недоступен. Найдите его заново.")
        data = {
            "merchant_id": merchant.id,
            "selected_merchant_id": merchant.id,
            "name": merchant.name,
        }
        stage = "channel"
    draft = service.begin(user_id, stage=stage, data=data, privileged=service.is_admin(user_id))
    await _render_draft(update, service, draft)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume role menu actions and active draft text, returning whether handled."""

    message = update.effective_message
    if message is None or not isinstance(message.text, str):
        return False
    text = message.text.strip()
    user_id = _identity(update)
    if user_id is None:
        if text in {INFO, ADD, SUGGEST, MINE, QUEUE, MANAGE}:
            await _say(update, "Откройте личный чат с ботом.")
            return True
        return False
    service = _service(context)
    try:
        if text == INFO:
            catalog = context.application.bot_data["catalog"]
            await _say(update, format_limits(catalog.cards), keyboard_for(service, user_id))
        elif text in {ADD, SUGGEST}:
            if text == ADD and not service.is_admin(user_id):
                raise CommunityError("Роль изменилась. Используйте «Предложить MCC магазина».")
            await _begin_contribution(update, service, user_id)
        elif text == MINE:
            await _mine(update, service, user_id)
        elif text == QUEUE:
            await _queue(update, service, user_id)
        elif text == MANAGE:
            await _management(update, service, user_id)
        else:
            draft = service.draft(user_id)
            if draft is None:
                return False
            draft = _consume_text(service, draft, text, getattr(update, "update_id", None))
            await _render_draft(update, service, draft)
    except (CommunityError, ValueError) as exc:
        await _say(update, str(exc), keyboard_for(service, user_id))
    return True


def _consume_text(
    service: CommunityService, draft: Draft, text: str, update_id: int | None
) -> Draft:
    stage, data = draft.stage, dict(draft.data)
    if stage == "name":
        data = {}
    if stage in {"name", "choose", "edit_name", "edit_choose", "target_name", "target_choose"}:
        name = clean_text(text, maximum=MAX_NAME)
        results = service.stores.search(name, limit=10)
        matches = (*results.matches, *results.suggestions)
        data["matches"] = list(dict.fromkeys(merchant.id for merchant in matches))[:10]
        if stage in {"name", "choose"}:
            data["name"] = name
            data.pop("merchant_id", None)
            data.pop("selected_merchant_id", None)
            stage = "choose"
        elif stage in {"edit_name", "edit_choose"}:
            stage = "edit_choose"
        else:
            data["matches"] = [value for value in data["matches"] if value != data["merchant_id"]]
            stage = "target_choose"
    elif stage == "mcc":
        if not re.fullmatch(r"[0-9]{4}", text):
            raise CommunityError("Введите MCC: ровно четыре цифры.")
        if data.get("editing") or data.get("kind") == "replace_mcc":
            payload = {"merchant_id": data["merchant_id"], "mcc": text}
            if data["kind"] == "replace_mcc":
                payload["old_mcc"] = data["old_mcc"]
        elif "merchant_id" in data:
            data["kind"] = "add_mcc"
            payload = {"merchant_id": data["merchant_id"], "mcc": text}
        else:
            data["kind"] = "add_merchant"
            payload = {"name": data["name"], "channel": data["channel"], "mcc": text}
        data["payload"] = payload
        stage = "evidence"
    elif stage in {"comment", "response"}:
        data["comment"] = clean_text(text)
        stage = "response_evidence" if stage == "response" else "preview"
    elif stage == "edit_value":
        payload = {"merchant_id": data["merchant_id"]}
        if data["kind"] == "rename_merchant":
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
        data["payload"] = payload
        stage = "comment"
    elif stage == "reason":
        data["reason"] = clean_text(text)
        stage = "review_preview"
    elif stage in {"evidence", "response_evidence"}:
        raise CommunityError(
            "На этом шаге нужен скриншот. Комментарий можно добавить подписью к нему."
        )
    else:
        raise CommunityError("Выберите действие кнопкой под текущим шагом.")
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
        if draft.stage not in {"evidence", "response_evidence"}:
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
        stage = "preview" if caption or draft.stage == "response_evidence" else "comment"
        draft = _advance(
            service,
            draft,
            stage,
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
        await _say(update, text, keyboard_for(service, user_id))


async def _dispatch_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    user_id: int,
    parts: list[str],
) -> None:
    action = parts[0]
    if action == "start":
        await _begin_contribution(
            update, service, user_id, (int(parts[1]) or None) if len(parts) == 2 else None
        )
    elif action == "report":
        merchant_id = int(parts[1])
        merchant = service.stores.get(merchant_id)
        if not merchant:
            raise CommunityError("Магазин не найден.")
        draft = service.begin(
            user_id,
            stage="report",
            data={
                "merchant_id": merchant_id,
                "selected_merchant_id": merchant_id,
                "name": merchant.name,
            },
            privileged=service.is_admin(user_id),
        )
        await _render_draft(update, service, draft)
    elif action == "d":
        draft = service.draft(user_id)
        if draft is None or draft.id != parts[1] or draft.version != int(parts[2]):
            raise StaleAction("Кнопка устарела. Продолжите текущий шаг или откройте меню.")
        await _draft_callback(update, context, service, draft, parts[3:])
    elif action == "mine":
        await _mine(update, service, user_id, min(1000000, max(0, int(parts[1]))))
    elif action == "own":
        await _own(update, service, user_id, int(parts[1]))
    elif action == "cancel":
        proposal = service.cancel(user_id, int(parts[1]), int(parts[2]))
        await _say(update, f"Предложение №{proposal.id} отменено.")
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
        if not service.is_admin(user_id):
            raise CommunityError("История доступна только действующим помощникам.")
        offset = _history_offset(parts[1]) if len(parts) == 2 else 0
        entries = service.stores.history(limit=HISTORY_PAGE_SIZE + 1, offset=offset)
        rows = []
        for entry in entries[:HISTORY_PAGE_SIZE]:
            merchant = service.stores.get(entry.merchant_id, include_archived=True)
            name = merchant.name if merchant else f"магазин №{entry.merchant_id}"
            rows.append(
                [
                    (
                        f"№{entry.id} · {name[:32]} · {HISTORY_KINDS.get(entry.kind, entry.kind)}",
                        f"history:{entry.merchant_id}",
                    )
                ]
            )
        navigation = []
        if offset:
            navigation.append(("⬅️ Предыдущая страница", f"recent:{offset - HISTORY_PAGE_SIZE}"))
        if len(entries) > HISTORY_PAGE_SIZE and offset < MAX_HISTORY_OFFSET:
            navigation.append(("Следующая страница ➡️", f"recent:{offset + HISTORY_PAGE_SIZE}"))
        if navigation:
            rows.append(navigation)
        await _say(
            update,
            f"Изменения, включая скрытые магазины · страница {offset // HISTORY_PAGE_SIZE + 1}."
            if entries
            else "На этой странице изменений нет.",
            _keyboard(rows),
        )
    elif action == "history":
        merchant_id = int(parts[1])
        if not service.stores.get(merchant_id, include_archived=True):
            raise CommunityError("Магазин не найден.")
        draft = service.begin(
            user_id,
            stage="history",
            privileged=True,
            data={"editing": True, "merchant_id": merchant_id},
        )
        await _render_draft(update, service, draft)
    elif action == "digest":
        if len(parts) != 3 or parts[1] not in {"0", "1"}:
            raise CommunityError("Некорректная настройка.")
        service.set_digest(user_id, parts[1] == "1", expected_epoch=int(parts[2]))
        await _management(update, service, user_id)
    elif action == "volunteer":
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
            f"Заявка от {identity} отправлена владельцу. "
            "Доступ появится только после подтверждения.",
        )
    elif action == "roles":
        offset = max(0, min(1000000, int(parts[1]))) if len(parts) > 1 else 0
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
        await _say(
            update,
            "Помощники и заявки" if rows else "Заявок и помощников пока нет.",
            _keyboard(rows),
        )
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
        await _say(update, f"{state}\n\n{_role_identity(candidate)}", _keyboard(rows))
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
        await _notify_role(
            context,
            service,
            int(parts[1]),
            "Вы назначены помощником. Очередь и управление доступны в меню."
            if parts[3] == "1"
            else "Доступ помощника отозван. Предложения доступны как обычно.",
        )
        await _say(
            update, "Роль обновлена. Вечерняя сводка выключена до нового согласия помощника."
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
        await _say(update, "Заявка отклонена.")
    elif action == "edit":
        data = {"editing": True}
        stage = "edit_name"
        if len(parts) == 2:
            merchant_id = int(parts[1])
            if not service.stores.get(merchant_id):
                raise CommunityError("Магазин не найден.")
            data["merchant_id"] = merchant_id
            stage = "editor"
        await _render_draft(
            update, service, service.begin(user_id, stage=stage, data=data, privileged=True)
        )
    else:
        raise CommunityError("Кнопка устарела. Откройте меню заново.")


async def _draft_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    service: CommunityService,
    draft: Draft,
    parts: list[str],
) -> None:
    action, stage, data = parts[0], draft.stage, dict(draft.data)
    if action == "cancel":
        service.cancel_draft(draft.user_id, draft.id, draft.version)
        await _say(update, "Действие отменено.", keyboard_for(service, draft.user_id))
        return
    if action == "resume":
        await _render_draft(update, service, draft)
        return
    if action == "submit" and stage == "preview":
        proposal = service.submit(draft.user_id, draft.id, draft.version)
        text = (
            "Изменение сохранено в базе."
            if proposal.status == "approved"
            else "Предложение отправлено на проверку. Статус — в «Мои предложения»."
        )
        await _say(update, text, keyboard_for(service, draft.user_id))
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
        await _say(update, "Решение сохранено.")
        return
    if action == "back":
        history = data.get("back", [])
        if history:
            stage = history[-1]
            data["back"] = history[:-1]
        else:
            stage = "editor" if data.get("editing") and data.get("merchant_id") else "name"
        if stage == "name":
            data = {}
        draft = service.advance(draft.user_id, draft.id, draft.version, stage, data)
        await _render_draft(update, service, draft)
        return
    if action == "select" and stage in {"choose", "edit_choose", "target_choose"}:
        merchant_id = int(parts[1])
        if merchant_id not in data.get("matches", []):
            raise StaleAction("Выберите магазин из текущих результатов.")
        merchant = service.stores.get(merchant_id)
        if not merchant:
            raise CommunityError("Магазин больше недоступен.")
        if stage == "target_choose":
            data["payload"] = {"merchant_id": data["merchant_id"], "target_id": merchant_id}
            stage = "comment"
        else:
            data.update(
                merchant_id=merchant.id, selected_merchant_id=merchant.id, name=merchant.name
            )
            stage = "editor" if stage == "edit_choose" else "channel"
    elif action == "new" and stage == "choose":
        data.pop("merchant_id", None)
        data.pop("selected_merchant_id", None)
        stage = "channel"
    elif action == "channel" and stage == "channel":
        channel = parts[1]
        if channel not in {"offline", "online"}:
            raise CommunityError("Выберите канал оплаты.")
        data["channel"] = channel
        if "selected_merchant_id" in data:
            merchant = service.stores.get(data["selected_merchant_id"])
            if merchant and merchant.channel == channel:
                data["merchant_id"] = merchant.id
            if not merchant or merchant.channel != channel:
                data.pop("merchant_id", None)
                matches = service.stores.find_exact(data["name"], channel)
                if len(matches) == 1:
                    data["merchant_id"] = matches[0].id
                elif len(matches) > 1:
                    data["matches"] = [item.id for item in matches]
                    stage = "choose"
                    draft = _advance(service, draft, stage, data)
                    await _render_draft(update, service, draft)
                    return
        stage = "mcc"
    elif action == "skip" and stage in {"comment", "evidence", "response_evidence"}:
        if stage == "evidence" and not service.is_admin(draft.user_id):
            raise CommunityError("Для MCC нужен скриншот.")
        if stage == "comment":
            data["comment"] = ""
        stage = "comment" if stage == "evidence" else "preview"
    elif action == "operation" and stage in {"report", "editor"}:
        kind = parts[1]
        allowed = (
            {"rename_merchant", "merge_merchant", "add_mcc", "replace_mcc"}
            if stage == "report"
            else set(KINDS) - {"add_merchant", "revert"}
        )
        if kind not in allowed or (stage == "editor" and not service.is_admin(draft.user_id)):
            raise CommunityError("Изменение недоступно.")
        data["kind"] = kind
        data.pop("payload", None)
        data.pop("comment", None)
        if kind in {"rename_merchant", "aliases"}:
            stage = "edit_value"
        elif kind == "merge_merchant":
            stage = "target_name"
        elif kind == "add_mcc":
            stage = "mcc" if data.get("editing") else "channel"
        elif kind in {"replace_mcc", "archive_mcc"}:
            stage = "old_mcc"
        else:
            data["payload"] = {"merchant_id": data["merchant_id"]}
            stage = "preview"
    elif action == "old" and stage == "old_mcc":
        mcc = parts[1]
        if mcc not in {fact.mcc for fact in service.stores.list_mcc(data["merchant_id"])}:
            raise StaleAction("MCC уже изменился.")
        if data["kind"] == "archive_mcc":
            data["payload"] = {"merchant_id": data["merchant_id"], "mcc": mcc}
            stage = "preview"
        else:
            data["old_mcc"] = mcc
            stage = "mcc"
    elif action == "history" and stage == "editor" and service.is_admin(draft.user_id):
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
    elif action == "undo" and stage == "history" and service.is_admin(draft.user_id):
        audit_id = int(parts[1])
        if audit_id not in data.get("history_undo_ids", []):
            raise StaleAction("Это изменение не показано на текущей странице истории.")
        entries = service.stores.history(
            data["merchant_id"], limit=HISTORY_PAGE_SIZE, offset=data.get("history_offset", 0)
        )
        if not any(
            item.id == audit_id and not item.reverted_by and item.kind != "revert"
            for item in entries
        ):
            raise StaleAction("Изменение недоступно для отмены.")
        data["kind"] = "revert"
        data["payload"] = {"audit_id": audit_id}
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
    if action in {"claim", "renew"}:
        proposal = (
            service.claim(user_id, proposal_id, version)
            if action == "claim"
            else service.touch_review(user_id, proposal_id, version)
        )
        await _review_view(update, service, proposal)
        return
    proposal = service.proposal(user_id, proposal_id)
    if (
        proposal.version != version
        or proposal.status != "pending"
        or proposal.reviewer_id != user_id
    ):
        raise StaleAction("Предложение уже изменилось. Откройте очередь.")
    if action in {"approve", "replace_confirm"}:
        proposal = service.review(
            user_id,
            proposal_id,
            version,
            "approved",
            replace_old=parts[3] if action == "replace_confirm" else None,
        )
        await _notify(context, proposal)
        await _say(update, "Предложение принято и сохранено в базе.")
    elif action == "replace" and proposal.kind == "add_mcc":
        service.touch_review(user_id, proposal_id, version)
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
        await _say(
            update,
            "Какой старый MCC подтверждён как ошибочный? Кнопка сохранит замену."
            if rows
            else "Нет другого MCC для замены.",
            _keyboard(rows),
        )
    elif action in {"reject", "clarify"}:
        service.touch_review(user_id, proposal_id, version)
        draft = service.begin(
            user_id,
            stage="reason",
            privileged=True,
            data={
                "proposal_id": proposal_id,
                "proposal_version": version,
                "decision": "rejected" if action == "reject" else "clarification",
            },
        )
        await _render_draft(update, service, draft)
    else:
        raise CommunityError("Действие недоступно.")
