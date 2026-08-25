from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mcc_bot.bot import _configure_bot_commands, _is_authorized, lookup_command, lookup_text, start
from mcc_bot.catalog import CardCatalog
from mcc_bot.config import BotSettings
from mcc_bot.descriptions import DescriptionCatalog


def _settings(*, open_access: bool, allowed_user_ids: frozenset[int]) -> BotSettings:
    return BotSettings(token="token", open_access=open_access, allowed_user_ids=allowed_user_ids)


def _context(settings: BotSettings, catalog: CardCatalog) -> SimpleNamespace:
    return SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "settings": settings,
                "catalog": catalog,
                "descriptions": DescriptionCatalog(labels={"5411": "Продуктовые магазины"}),
            }
        ),
        args=[],
    )


def test_authorization_supports_open_and_restricted_modes() -> None:
    user = SimpleNamespace(effective_user=SimpleNamespace(id=123))

    assert _is_authorized(user, _settings(open_access=True, allowed_user_ids=frozenset()))
    assert _is_authorized(user, _settings(open_access=False, allowed_user_ids=frozenset({123})))
    assert not _is_authorized(
        SimpleNamespace(effective_user=SimpleNamespace(id=999)),
        _settings(open_access=False, allowed_user_ids=frozenset({123})),
    )


def test_start_sends_russian_usage_instructions(catalog_path) -> None:
    settings = _settings(open_access=True, allowed_user_ids=frozenset())
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_message=message)

    asyncio.run(start(update, _context(settings, CardCatalog.from_file(catalog_path))))

    message.reply_text.assert_awaited_once()
    assert "четырёхзначный MCC" in message.reply_text.await_args.args[0]


def test_lookup_command_replies_with_sorted_russian_results(catalog_path) -> None:
    settings = _settings(open_access=True, allowed_user_ids=frozenset())
    message = SimpleNamespace(reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_message=message)
    context = _context(settings, CardCatalog.from_file(catalog_path))
    context.args = ["5411"]

    asyncio.run(lookup_command(update, context))

    result = message.reply_text.await_args.args[0]
    assert result.index("Beta Card") < result.index("Alpha Card")
    assert "Продуктовые магазины" in result


def test_text_lookup_accepts_mcc_prefix(catalog_path) -> None:
    settings = _settings(open_access=True, allowed_user_ids=frozenset())
    message = SimpleNamespace(text="MCC 5812", reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_message=message)

    asyncio.run(lookup_text(update, _context(settings, CardCatalog.from_file(catalog_path))))

    assert "Alpha Card" in message.reply_text.await_args.args[0]


def test_unauthorized_lookup_does_not_query_catalog() -> None:
    settings = _settings(open_access=False, allowed_user_ids=frozenset({123}))
    message = SimpleNamespace(text="5411", reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=999), effective_message=message)
    catalog = SimpleNamespace(lookup=pytest.fail)

    asyncio.run(lookup_text(update, _context(settings, catalog)))

    message.reply_text.assert_awaited_once_with("Доступ запрещён.")


def test_command_menu_lists_supported_commands() -> None:
    set_commands = AsyncMock()
    application = SimpleNamespace(bot=SimpleNamespace(set_my_commands=set_commands))

    asyncio.run(_configure_bot_commands(application))

    commands = set_commands.await_args.args[0]
    assert [command.command for command in commands] == ["start", "help", "mcc"]
