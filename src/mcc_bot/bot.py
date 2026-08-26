"""Telegram application entry point and handlers."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .catalog import CardCatalog, CatalogError, InvalidMccError, normalize_mcc
from .config import BotSettings, SettingsError
from .descriptions import DescriptionCatalog
from .formatting import format_limits, format_match_pages, split_message
from .users import UserRegistry

LOGGER = logging.getLogger(__name__)
DETAILS_CALLBACK = re.compile(r"mcc_details:([0-9]{4}):(0|[1-9][0-9]{0,5}):([01])")
RESULT_TOO_LONG = (
    "Не удалось показать результат: данные одной карты или описание MCC слишком длинные. "  # noqa: RUF001
    "Лимиты по картам: /limits"
)


def load_environment() -> None:
    """Load a local ``.env`` from the current working directory.

    Existing process environment variables win, which keeps platform-managed
    secrets authoritative in deployment environments.
    """

    load_dotenv(dotenv_path=Path(".env"), override=False)


async def _configure_bot_commands(application: Application) -> None:
    """Expose the supported commands in Telegram's command menu."""

    await application.bot.set_my_commands(
        [
            BotCommand(command="start", description="Инструкция по MCC"),
            BotCommand(command="limits", description="Лимиты по картам"),
        ]
    )


async def _reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message is None:
        return
    for chunk in split_message(text):
        await message.reply_text(chunk)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/start`` with a short usage guide."""

    await _reply(
        update,
        "Отправьте четырёхзначный MCC, чтобы увидеть карты с манибэком.\n"  # noqa: RUF001
        "Пример: 5411 или /mcc 5411\n"
        "Лимиты по картам: /limits",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/help``."""

    await start(update, context)


async def _lookup_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_mcc: str,
) -> None:
    catalog: CardCatalog = context.application.bot_data["catalog"]
    descriptions: DescriptionCatalog = context.application.bot_data["descriptions"]
    try:
        normalized_mcc = normalize_mcc(raw_mcc)
        matches = catalog.lookup(normalized_mcc)
    except InvalidMccError as exc:
        await _reply(update, str(exc))
        return
    try:
        pages = format_match_pages(normalized_mcc, matches, descriptions, html=True)
    except ValueError:
        await _reply(update, RESULT_TOO_LONG)
        return
    message = update.effective_message
    if message is None:
        return
    for page_index, page in enumerate(pages):
        if matches:
            await message.reply_text(
                page.compact,
                parse_mode=ParseMode.HTML,
                reply_markup=_details_keyboard(normalized_mcc, page_index, details=False),
            )
        else:
            await message.reply_text(page.compact, parse_mode=ParseMode.HTML)


def _details_keyboard(mcc: str, page: int, *, details: bool) -> InlineKeyboardMarkup:
    label = "Скрыть подробности" if details else "🏦 Банки и минимальный платёж"
    button = InlineKeyboardButton(
        label, callback_data=f"mcc_details:{mcc}:{page}:{int(not details)}"
    )
    return InlineKeyboardMarkup([[button]])


async def toggle_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Acknowledge a stateless MCC button and edit only its own result message.

    Callback data carries the normalized MCC, stable card page and target view.
    Invalid, expired and inaccessible callbacks never create new messages.
    """

    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except TelegramError:
        LOGGER.info("Could not acknowledge an MCC details callback")
        return
    payload = DETAILS_CALLBACK.fullmatch(query.data) if isinstance(query.data, str) else None
    if payload is None or query.message is None or not query.message.is_accessible:
        return
    mcc, raw_page, raw_details = payload.groups()
    page_index, details = int(raw_page), raw_details == "1"
    catalog: CardCatalog = context.application.bot_data["catalog"]
    # There cannot be more non-empty pages than cards. Reject forged large indices
    # before catalog lookup and formatting, then check the actual page count below.
    if page_index >= len(catalog.cards):
        return
    descriptions: DescriptionCatalog = context.application.bot_data["descriptions"]
    matches = catalog.lookup(mcc)
    if not matches:
        return
    try:
        pages = format_match_pages(mcc, matches, descriptions, html=True)
    except ValueError:
        text, keyboard = RESULT_TOO_LONG, None
    else:
        if page_index >= len(pages):
            return
        page = pages[page_index]
        text = page.expanded if details else page.compact
        keyboard = _details_keyboard(mcc, page_index, details=details)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            LOGGER.info("Could not edit an MCC details message")
    except (TelegramError, TypeError):
        LOGGER.info("MCC details message is no longer accessible")


async def lookup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/mcc 5411``."""

    raw_mcc = " ".join(context.args).strip()
    if not raw_mcc:
        await _reply(update, "Укажите четырёхзначный MCC после /mcc, например /mcc 5411.")
        return
    await _lookup_and_reply(update, context, raw_mcc)


async def lookup_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a bare MCC sent as a normal text message."""

    message = update.effective_message
    if message is None or not isinstance(message.text, str):
        return
    await _lookup_and_reply(update, context, message.text)


async def limits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle ``/limits`` by returning all payment thresholds and monthly caps."""

    catalog: CardCatalog = context.application.bot_data["catalog"]
    await _reply(update, format_limits(catalog.cards))


async def remember_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persist the effective chat ID before processing a supported update."""

    chat = update.effective_chat
    if chat is None:
        return
    registry: UserRegistry = context.application.bot_data["user_registry"]
    registry.remember(chat.id)


def build_application(settings: BotSettings) -> Application:
    """Build a configured Telegram application without starting polling."""

    try:
        catalog = CardCatalog.from_file(settings.catalog_path)
        descriptions = DescriptionCatalog.from_file(settings.descriptions_path)
    except CatalogError as exc:
        raise SettingsError(str(exc)) from exc
    user_registry = UserRegistry(settings.user_registry_path)
    user_registry.initialize()
    application = (
        ApplicationBuilder().token(settings.token).post_init(_configure_bot_commands).build()
    )
    application.bot_data["catalog"] = catalog
    application.bot_data["descriptions"] = descriptions
    application.bot_data["user_registry"] = user_registry
    application.add_handler(TypeHandler(Update, remember_chat), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mcc", lookup_command))
    application.add_handler(CommandHandler("limits", limits_command))
    application.add_handler(CallbackQueryHandler(toggle_details))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lookup_text))
    return application


def main() -> None:
    """Load configuration and run the bot with Telegram long polling."""

    load_environment()
    try:
        settings = BotSettings.from_environment()
        application = build_application(settings)
    except SettingsError as exc:
        raise SystemExit(str(exc)) from exc
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, settings.log_level, logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    LOGGER.info("Starting MCC bot with catalog %s", settings.catalog_path)
    application.run_polling(allowed_updates=Update.ALL_TYPES)
