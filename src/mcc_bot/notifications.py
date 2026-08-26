"""At-most-once daily reviewer digests and screenshot reference cleanup."""

# Russian digest text deliberately contains Cyrillic look-alike letters.
# ruff: noqa: RUF001

from __future__ import annotations

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

from .community import CommunityService

LOGGER = logging.getLogger(__name__)
MINSK = ZoneInfo("Europe/Minsk")


async def send_daily_digest(
    service: CommunityService, bot, *, now: datetime | None = None
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
        count = service.reserve_digest(user_id, day)
        if count is None:
            continue
        # Recheck immediately before sending, including changes since candidate
        # selection/reservation. No screenshots, author IDs or proposal text here.
        if not service.digest_enabled(user_id):
            service.finish_digest(user_id, day, "skipped")
            result["skipped"] += 1
            continue
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"В очереди предложений для проверки: {count}.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "📋 Разобрать очередь", callback_data="community:queue:0"
                            )
                        ]
                    ]
                ),
            )
        except TelegramError:
            service.finish_digest(user_id, day, "uncertain")
            result["uncertain"] += 1
            LOGGER.info("Daily digest delivery uncertain; no automatic retry")
        else:
            service.finish_digest(user_id, day, "sent")
            result["sent"] += 1
    return result


async def _digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_daily_digest(context.application.bot_data["community"], context.bot)


async def _cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.application.bot_data["community"].expire_media()


def install_jobs(application: Application) -> None:
    """Install Minsk 20:00 digests and hourly retention cleanup with no catch-up."""

    if application.job_queue is None:
        raise RuntimeError("Install python-telegram-bot[job-queue] to enable daily digests")
    application.job_queue.run_daily(
        _digest_job,
        time=time(20, 0, tzinfo=MINSK),
        name="community-daily-digest",
        job_kwargs={"misfire_grace_time": 60, "coalesce": True, "max_instances": 1},
    )
    application.job_queue.run_repeating(
        _cleanup_job,
        interval=3600,
        first=60,
        name="community-media-cleanup",
        job_kwargs={"coalesce": True, "max_instances": 1},
    )
