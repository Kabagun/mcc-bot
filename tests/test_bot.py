from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import BotCommand

from mcc_bot.bot import (
    _configure_bot_commands,
    limits_command,
    lookup_command,
    lookup_text,
    remember_chat,
    start,
)
from mcc_bot.catalog import CardCatalog
from mcc_bot.descriptions import DescriptionCatalog
from mcc_bot.users import UserRegistry


def _context(catalog: CardCatalog) -> SimpleNamespace:
    return SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "catalog": catalog,
                "descriptions": DescriptionCatalog(labels={"5411": "Продуктовые магазины"}),
            }
        ),
        args=[],
    )


def test_start_sends_russian_usage_instructions(catalog_path) -> None:
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message)

    asyncio.run(start(update, _context(CardCatalog.from_file(catalog_path))))

    message.reply_text.assert_awaited_once()
    assert "четырёхзначный MCC" in message.reply_text.await_args.args[0]


def test_lookup_command_replies_with_sorted_russian_results(catalog_path) -> None:
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message)
    context = _context(CardCatalog.from_file(catalog_path))
    context.args = ["5411"]

    asyncio.run(lookup_command(update, context))

    result = message.reply_text.await_args.args[0]
    assert result.index("Beta Card") < result.index("Alpha Card")
    assert "Продуктовые магазины" in result


def test_text_lookup_accepts_mcc_prefix(catalog_path) -> None:
    message = SimpleNamespace(text="MCC 5812", reply_text=AsyncMock())
    update = SimpleNamespace(effective_message=message)

    asyncio.run(lookup_text(update, _context(CardCatalog.from_file(catalog_path))))

    assert "Alpha Card" in message.reply_text.await_args.args[0]


def test_limits_replies_immediately_and_does_not_change_number_lookup(catalog_path) -> None:
    catalog = CardCatalog.from_file(catalog_path)
    context = _context(catalog)
    limits_message = SimpleNamespace(reply_text=AsyncMock())

    asyncio.run(limits_command(SimpleNamespace(effective_message=limits_message), context))

    limits_result = limits_message.reply_text.await_args.args[0]
    assert "📊 Лимиты по картам" in limits_result
    assert "Alpha Card" in limits_result

    lookup_message = SimpleNamespace(text="5411", reply_text=AsyncMock())
    asyncio.run(lookup_text(SimpleNamespace(effective_message=lookup_message), context))

    lookup_result = lookup_message.reply_text.await_args.args[0]
    assert "MCC 5411" in lookup_result
    assert "Лимиты по картам" not in lookup_result


def test_command_menu_lists_start_and_limits_with_russian_descriptions() -> None:
    set_commands = AsyncMock()
    application = SimpleNamespace(bot=SimpleNamespace(set_my_commands=set_commands))

    asyncio.run(_configure_bot_commands(application))

    set_commands.assert_awaited_once_with(
        [
            BotCommand(command="start", description="Инструкция по MCC"),
            BotCommand(command="limits", description="Лимиты по картам"),
        ]
    )


def test_remember_chat_persists_effective_chat_id(catalog_path, tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    context = _context(CardCatalog.from_file(catalog_path))
    context.application.bot_data["user_registry"] = registry
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=12345))

    asyncio.run(remember_chat(update, context))

    assert registry.chat_ids() == (12345,)
