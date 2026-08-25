"""Telegram application entry point and handlers."""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .catalog import CardCatalog, CatalogError, InvalidMccError, normalize_mcc
from .config import BotSettings, SettingsError
from .descriptions import DescriptionCatalog
from .formatting import format_matches, split_message

LOGGER = logging.getLogger(__name__)


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
        "Пример: 5411 или /mcc 5411",
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
    await _reply(update, format_matches(normalized_mcc, matches, descriptions))


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


def build_application(settings: BotSettings) -> Application:
    """Build a configured Telegram application without starting polling."""

    try:
        catalog = CardCatalog.from_file(settings.catalog_path)
        descriptions = DescriptionCatalog.from_file(settings.descriptions_path)
    except CatalogError as exc:
        raise SettingsError(str(exc)) from exc
    application = (
        ApplicationBuilder().token(settings.token).post_init(_configure_bot_commands).build()
    )
    application.bot_data["catalog"] = catalog
    application.bot_data["descriptions"] = descriptions
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mcc", lookup_command))
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
