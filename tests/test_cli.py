from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from telegram.error import ChatMigrated, NetworkError

from mcc_bot.broadcast_failures import main as broadcast_failures_main
from mcc_bot.cli import main
from mcc_bot.config import DEFAULT_CATALOG_PATH, DEFAULT_DESCRIPTIONS_PATH
from mcc_bot.users import UserRegistry, broadcast_message

DATA_PATH = DEFAULT_CATALOG_PATH
DESCRIPTION_PATH = DEFAULT_DESCRIPTIONS_PATH


def test_cli_prints_russian_unicode_card_names(capsys) -> None:
    assert (
        main(
            [
                "--catalog",
                str(DATA_PATH),
                "--descriptions",
                str(DESCRIPTION_PATH),
                "--mcc",
                "5411",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "🛒 MCC 5411 — Продуктовые магазины" in output
    assert "💳 Витамин Д" in output
    assert "2,5% (2,435%)" in output


def test_cli_json_contains_all_components(capsys) -> None:
    assert (
        main(
            [
                "--catalog",
                str(DATA_PATH),
                "--descriptions",
                str(DESCRIPTION_PATH),
                "--mcc",
                "5411",
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"gross_percent": "1"' in output
    assert '"tax_exempt": true' in output


def test_cli_uses_bundled_resources_outside_checkout(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["--mcc", "5411"]) == 0
    assert "🛒 MCC 5411 — Продуктовые магазины" in capsys.readouterr().out


def test_broadcast_failures_cli_shows_identity_and_reason_without_secrets_or_id(
    tmp_path,
    capsys,
) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(998877, "known_user", "Known", "User")
    token = "123456:ABC-secret_token"
    bot = AsyncMock()
    bot.token = token
    bot.send_message.side_effect = NetworkError(f"offline {token} for chat 998877")
    asyncio.run(broadcast_message(bot, registry, "News"))

    assert (
        broadcast_failures_main(
            ["--latest", "--database", str(registry.path)],
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "STATUS=completed" in output
    assert "BROADCAST_ATTEMPTED=1 BROADCAST_SENT=0 BROADCAST_FAILED=1" in output
    assert "@known_user (Known User)" in output
    assert "NetworkError: offline [REDACTED] for chat [telegram-id]" in output
    assert token not in output
    assert "998877" not in output


def test_broadcast_failures_cli_redacts_chat_migration_destination_id(
    tmp_path,
    capsys,
) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    old_chat_id = -1001234567890
    new_chat_id = -1009876543210
    registry.remember(old_chat_id, "known_group", None, None)
    bot = AsyncMock()
    bot.send_message.side_effect = ChatMigrated(new_chat_id)
    asyncio.run(broadcast_message(bot, registry, "News"))

    assert (
        broadcast_failures_main(
            ["--latest", "--database", str(registry.path)],
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "@known_group" in output
    assert "ChatMigrated: Group migrated to supergroup. New chat id: [telegram-id]" in output
    assert str(old_chat_id) not in output
    assert str(new_chat_id) not in output
    assert str(new_chat_id) not in registry.latest_broadcast_run().failures[0].error_text
