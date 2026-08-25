from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcc_bot.bot import _configure_bot_commands, lookup_command, lookup_text, start
from mcc_bot.catalog import CardCatalog
from mcc_bot.descriptions import DescriptionCatalog


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


def test_command_menu_lists_only_start() -> None:
    set_commands = AsyncMock()
    application = SimpleNamespace(bot=SimpleNamespace(set_my_commands=set_commands))

    asyncio.run(_configure_bot_commands(application))

    commands = set_commands.await_args.args[0]
    assert [command.command for command in commands] == ["start"]
