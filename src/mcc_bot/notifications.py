"""Silent reviewer invitations, daily reminders and screenshot cleanup."""

# Russian digest text deliberately contains Cyrillic look-alike letters.
# ruff: noqa: RUF001

from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, ContextTypes

from .community import CommunityService
from .users import UserRegistry

LOGGER = logging.getLogger(__name__)
MINSK = ZoneInfo("Europe/Minsk")


def reminder_choice_keyboard(epoch: int) -> InlineKeyboardMarkup:
    """Offer an explicit choice without changing the user's main keyboard."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Да, напоминать", callback_data=f"community:reminder:1:{epoch}")],
            [InlineKeyboardButton("Нет, спасибо", callback_data=f"community:reminder:0:{epoch}")],
        ]
    )


def reminder_actions_keyboard(epoch: int, counts: dict[str, int]) -> InlineKeyboardMarkup:
    rows = []
    if counts["data"]:
        rows.append(
            [InlineKeyboardButton("📋 Разобрать очередь", callback_data="community:queue:0")]
        )
    if counts["helpers"]:
        rows.append(
            [InlineKeyboardButton("👥 Заявки в помощники", callback_data="community:roles:0")]
        )
    rows.append(
        [InlineKeyboardButton("🔕 Не напоминать", callback_data=f"community:reminder:0:{epoch}")]
    )
    return InlineKeyboardMarkup(rows)


def _work_lines(counts: dict[str, int]) -> str:
    lines = []
    if counts["data"]:
        lines.append(f"Заявок на проверку данных: {counts['data']}.")
    if counts["helpers"]:
        lines.append(f"Заявок в помощники: {counts['helpers']}.")
    return "\n".join(lines)


def reminder_message(counts: dict[str, int], *, invitation: bool = False) -> str:
    text = (
        "📋 Есть заявки — поможете?\n\n"
        + _work_lines(counts)
        + "\n\nПомогите с разбором: так бот быстрее пополнится проверенными данными. "
        "Есть пара минут — проверьте хотя бы одну заявку."
        "\n\nБез вашей помощи заявки остаются без проверки, данные устаревают, "
        "а бот теряет пользу. Давайте развивать его вместе!"
    )
    if invitation:
        text += (
            "\n\nХотите получать такие напоминания? Они приходят без звука, "
            "раз в день в 20:00 по Минску, только когда есть что разобрать."
            "\n\nОткажетесь — вероятность бана вырастет в разы 😈"
        )
    return text


def reminder_preference_message(enabled: bool) -> str:
    if enabled:
        return (
            "Спасибо, что помогаете развивать бота!\n\n"
            "Напоминания включены: без звука, в 20:00 по Минску, "
            "только если есть заявки для разбора."
            "\nОтключить их можно здесь или в «⚙️ Управление»."
        )
    return (
        "Жаль! Вероятность бана выросла в разы 😈"
        "\n\nНапоминания отключены. Вы остаётесь помощником "
        "и по-прежнему можете разбирать заявки."
        "\n\nСпасибо за ответ! Если передумаете, напоминания можно включить обратно, "
        "если вас, конечно, еще не забанят."
    )


async def send_reminder_invitations(
    service: CommunityService,
    bot,
    *,
    now: datetime | None = None,
    registry: UserRegistry | None = None,
) -> dict[str, int]:
    """Ask never-subscribed reviewers once, only while work is available."""
    local = (now or datetime.now(MINSK)).astimezone(MINSK)
    day = local.date().isoformat()
    result = {"sent": 0, "uncertain": 0, "skipped": 0}
    eligible = {r.chat_id for r in registry.recipients()} if registry is not None else None
    for user_id in service.reminder_invitation_candidates():
        if eligible is not None and user_id not in eligible:
            result["skipped"] += 1
            continue
        reservation = service.reserve_reminder_invitation(user_id, day)
        if reservation is None:
            continue
        _count, epoch = reservation
        current = service.refresh_reminder_invitation(user_id, day, epoch)
        counts = service.reminder_work_counts(user_id) if current is not None else None
        if counts is None or not sum(counts.values()):
            service.finish_reminder_invitation(user_id, day, epoch, "skipped")
            result["skipped"] += 1
            continue
        try:
            await bot.send_message(
                chat_id=user_id,
                text=reminder_message(counts, invitation=True),
                reply_markup=reminder_choice_keyboard(epoch),
                disable_notification=True,
            )
        except TelegramError as error:
            if isinstance(error, Forbidden) and registry is not None:
                registry.mark_forbidden(user_id, error)
            service.finish_reminder_invitation(user_id, day, epoch, "uncertain")
            result["uncertain"] += 1
            LOGGER.info("Reminder invitation delivery uncertain; no automatic retry")
        else:
            service.finish_reminder_invitation(user_id, day, epoch, "sent")
            result["sent"] += 1
    return result


async def send_daily_digest(
    service: CommunityService,
    bot,
    *,
    now: datetime | None = None,
    registry: UserRegistry | None = None,
) -> dict[str, int]:
    """Send today's count once per subscribed reviewer, never replaying missed dates.

    An intent is committed before the Telegram call. A timeout or process crash
    leaves the date consumed: accepting a rare missed message prevents duplicates.
    """

    local = (now or datetime.now(MINSK)).astimezone(MINSK)
    result = {"sent": 0, "uncertain": 0, "skipped": 0}
    if local.hour < 20:
        return result
    day = local.date().isoformat()
    service.expire_media()
    for user_id in service.digest_candidates():
        if registry is not None and registry.blocked_chat(user_id) is not None:
            result["skipped"] += 1
            continue
        count = service.reserve_digest(user_id, day)
        if count is None:
            continue
        # Recheck immediately before sending, including changes since candidate
        # selection/reservation. No screenshots, author IDs or proposal text here.
        count = service.refresh_digest_count(user_id, day)
        counts = service.reminder_work_counts(user_id) if count is not None else None
        if counts is None or not sum(counts.values()):
            service.finish_digest(user_id, day, "skipped")
            result["skipped"] += 1
            continue
        try:
            await bot.send_message(
                chat_id=user_id,
                text=reminder_message(counts),
                reply_markup=reminder_actions_keyboard(service.role_epoch(user_id), counts),
                disable_notification=True,
            )
        except TelegramError as error:
            if isinstance(error, Forbidden) and registry is not None:
                registry.mark_forbidden(user_id, error)
            service.finish_digest(user_id, day, "uncertain")
            result["uncertain"] += 1
            LOGGER.info("Daily digest delivery uncertain; no automatic retry")
        else:
            service.finish_digest(user_id, day, "sent")
            result["sent"] += 1
    return result


async def _digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_daily_digest(
        context.application.bot_data["community"],
        context.bot,
        registry=context.application.bot_data["user_registry"],
    )


async def _invitation_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_reminder_invitations(
        context.application.bot_data["community"],
        context.bot,
        registry=context.application.bot_data["user_registry"],
    )


async def _cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    service = context.application.bot_data["community"]
    service.requeue_expired_clarifications()
    service.expire_media()


def install_jobs(application: Application) -> None:
    """Install initial invitations, Minsk 20:00 reminders and hourly cleanup."""

    if application.job_queue is None:
        raise RuntimeError("Install python-telegram-bot[job-queue] to enable daily digests")
    application.job_queue.run_daily(
        _digest_job,
        time=time(20, 0, tzinfo=MINSK),
        name="community-daily-digest",
        job_kwargs={"misfire_grace_time": 60, "coalesce": True, "max_instances": 1},
    )
    application.job_queue.run_repeating(
        _invitation_job,
        interval=60,
        first=30,
        name="community-reminder-invitations",
        job_kwargs={"coalesce": True, "max_instances": 1},
    )
    application.job_queue.run_repeating(
        _cleanup_job,
        interval=3600,
        first=60,
        name="community-media-cleanup",
        job_kwargs={"coalesce": True, "max_instances": 1},
    )
