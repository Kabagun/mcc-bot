"""Telegram application entry point and handlers."""

from __future__ import annotations

import logging
import re
import secrets
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
from .community import CommunityService
from .community_handlers import INFO, keyboard_for, show_menu
from .community_handlers import callback as community_callback
from .community_handlers import handle_media as handle_community_media
from .community_handlers import handle_text as handle_community_text
from .config import BotSettings, SettingsError
from .descriptions import DescriptionCatalog
from .formatting import format_match_pages, split_message
from .notifications import install_jobs
from .partner_rewards import PartnerRepository
from .store_handlers import handle_store_callback, search_stores
from .stores import Brand, StoreRepository
from .users import UserRegistry

LOGGER = logging.getLogger(__name__)
DETAILS_CALLBACK = re.compile(r"mcc_details:([0-9]{4}):(0|[1-9][0-9]{0,5}):([01])")
MCC_LOOKUP_CALLBACK = re.compile(r"mcc_lookup:([0-9]{4})")
RESULT_TOO_LONG = (
    "Не удалось показать результат: данные одной карты или описание MCC слишком длинные. "  # noqa: RUF001
    f"Откройте /start и нажмите «{INFO}»."
)


def load_environment() -> None:
    """Load a local ``.env`` from the current working directory.

    Existing process environment variables win, which keeps platform-managed
    secrets authoritative in deployment environments.
    """

    load_dotenv(dotenv_path=Path(".env"), override=False)


async def _configure_bot_commands(application: Application) -> None:
    """Synchronize the public Telegram profile and supported command menu."""

    await application.bot.set_my_commands(
        [BotCommand(command="start", description="Начало и меню")]
    )
    await application.bot.set_my_name("Какой картой?")
    await application.bot.set_my_short_description("Подскажет лучшую карту по магазину или MCC.")


async def _reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message is None:
        return
    for chunk in split_message(text):
        await message.reply_text(chunk)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current role-aware menu and short lookup instructions."""

    await show_menu(update, context)


async def _lookup_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_mcc: str,
) -> None:
    catalog: CardCatalog = context.application.bot_data["catalog"]
    descriptions: DescriptionCatalog = context.application.bot_data["descriptions"]
    try:
        normalized_mcc = normalize_mcc(raw_mcc)
    except InvalidMccError as exc:
        await _reply(update, str(exc))
        return
    if normalized_mcc not in descriptions:
        await _reply(
            update,
            f"MCC {normalized_mcc} не найден в справочнике. Проверьте код.",
        )
        return
    matches = catalog.lookup(normalized_mcc)
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
    if mcc not in descriptions:
        return
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


async def lookup_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route menu/form input first, then bare four-digit MCCs or merchant names."""

    message = update.effective_message
    if message is None or not isinstance(message.text, str):
        return
    if await handle_community_text(update, context):
        return
    value = message.text.strip()
    if not value:
        await unknown_command(update, context)
    elif value.isascii() and value.isdecimal() and len(value) == 4:
        await _route_four_digit_text(update, context, value)
    else:
        await search_stores(update, context, value)


def _exact_brand_matches(context: ContextTypes.DEFAULT_TYPE, query: str) -> tuple[Brand, ...]:
    """Return store matches before deciding whether a numeric query is also an MCC."""

    repository: StoreRepository | None = context.application.bot_data.get("stores")
    if repository is None:
        return ()
    result = repository.search(query, limit=100)
    return tuple(result.matches)


def _remember_store_search(context: ContextTypes.DEFAULT_TYPE, query: str) -> str:
    """Keep a bounded callback-safe query for brand selection or creation."""

    token = secrets.token_hex(4)
    searches = context.user_data.setdefault("store_searches", {})
    if len(searches) >= 20:
        searches.pop(next(iter(searches)))
    searches[token] = query
    return token


async def _route_four_digit_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    value: str,
) -> None:
    """Disambiguate an exact numeric brand from a known MCC without fuzzy promotion."""

    descriptions: DescriptionCatalog = context.application.bot_data["descriptions"]
    brands = _exact_brand_matches(context, value)
    known_mcc = value in descriptions
    if brands and not known_mcc:
        await search_stores(update, context, value)
        return
    if known_mcc and not brands:
        await _lookup_and_reply(update, context, value)
        return

    message = update.effective_message
    if message is None:
        return
    if brands:
        if len(brands) == 1:
            brand_callback = f"store:show:{brands[0].id}:0"
        else:
            token = _remember_store_search(context, value)
            brand_callback = f"store:search:{token}:0"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🏪 Магазин «{value}»",
                        callback_data=brand_callback,
                    )
                ],
                [InlineKeyboardButton(f"🧾 MCC {value}", callback_data=f"mcc_lookup:{value}")],
            ]
        )
        await message.reply_text(
            f"«{value}» есть и среди магазинов, и в справочнике MCC. Что открыть?",
            reply_markup=keyboard,
        )
        return

    await search_stores(update, context, value)


async def lookup_mcc_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the explicitly selected MCC branch of a numeric brand/MCC conflict."""

    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except TelegramError:
        LOGGER.info("Could not acknowledge an MCC lookup callback")
        return
    payload = MCC_LOOKUP_CALLBACK.fullmatch(query.data) if isinstance(query.data, str) else None
    if payload is None or query.message is None or not query.message.is_accessible:
        return
    await _lookup_and_reply(update, context, payload.group(1))


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Direct obsolete and unknown commands to the current menu."""

    text = (
        "Отправьте MCC из четырёх цифр (например, 5411) или название магазина.\n"
        f"Откройте /start и нажмите «{INFO}», чтобы посмотреть лимиты и условия карт."
    )
    message = update.effective_message
    service = context.application.bot_data.get("community")
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    if (
        message is not None
        and service is not None
        and user is not None
        and chat is not None
        and chat.type == "private"
    ):
        await message.reply_text(text, reply_markup=keyboard_for(service, user.id))
    else:
        await _reply(update, text)


async def lookup_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pass screenshots only to an active contribution or clarification form."""

    if not await handle_community_media(update, context):
        await _reply(update, "Для отправки скриншота сначала нажмите «Предложить данные».")


async def expired_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Acknowledge an unknown old button without applying another handler's action."""

    if update.callback_query is not None:
        try:
            await update.callback_query.answer("Кнопка устарела. Откройте /start.")
        except TelegramError:
            LOGGER.info("Could not acknowledge an expired callback")


async def report_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report unexpected failures without logging Telegram identities or evidence."""

    LOGGER.error("Unexpected bot error: %s", type(context.error).__name__)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "Не удалось завершить действие. Проверьте его результат в меню /start "  # noqa: RUF001
                "и при необходимости повторите."
            )
        except TelegramError:
            LOGGER.info("Could not deliver an error notice")


async def remember_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persist the chat and refresh known role identities before handling an update."""

    chat = update.effective_chat
    if chat is None:
        return
    registry: UserRegistry = context.application.bot_data["user_registry"]
    registry.remember(chat.id)
    user = getattr(update, "effective_user", None)
    community: CommunityService | None = context.application.bot_data.get("community")
    if user is not None and community is not None:
        community.refresh_role_profile(
            user.id,
            getattr(user, "username", None),
            getattr(user, "first_name", None),
            getattr(user, "last_name", None),
        )


def build_application(settings: BotSettings) -> Application:
    """Build a configured Telegram application without starting polling."""

    try:
        catalog = CardCatalog.from_file(settings.catalog_path)
        descriptions = DescriptionCatalog.from_file(settings.descriptions_path)
    except CatalogError as exc:
        raise SettingsError(str(exc)) from exc
    user_registry = UserRegistry(settings.user_registry_path)
    user_registry.initialize()
    stores = StoreRepository(settings.stores_path)
    stores.initialize()
    partners = PartnerRepository(stores)
    partners.initialize()
    community = CommunityService(
        stores,
        owner_id=settings.owner_telegram_id,
        allowed_mccs=descriptions.labels,
        partners=partners,
        catalog=catalog,
    )
    community.initialize()
    application = (
        ApplicationBuilder().token(settings.token).post_init(_configure_bot_commands).build()
    )
    application.bot_data["catalog"] = catalog
    application.bot_data["descriptions"] = descriptions
    application.bot_data["user_registry"] = user_registry
    application.bot_data["stores"] = stores
    application.bot_data["partners"] = partners
    application.bot_data["community"] = community
    application.add_handler(TypeHandler(Update, remember_chat), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_handler(CallbackQueryHandler(toggle_details, pattern=r"^mcc_details:"))
    application.add_handler(CallbackQueryHandler(lookup_mcc_callback, pattern=r"^mcc_lookup:"))
    application.add_handler(CallbackQueryHandler(handle_store_callback, pattern=r"^store:"))
    application.add_handler(CallbackQueryHandler(community_callback, pattern=r"^community:"))
    application.add_handler(CallbackQueryHandler(expired_callback))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, lookup_media))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lookup_text))
    application.add_error_handler(report_error)
    install_jobs(application)
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
