from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from telegram.error import NetworkError

from mcc_bot.users import UserRegistry, broadcast_message


def test_user_registry_remembers_unique_chat_ids(tmp_path) -> None:
    path = tmp_path / "users.sqlite3"
    registry = UserRegistry(path)
    registry.initialize()

    registry.remember(20)
    registry.remember(10)
    registry.remember(20)

    assert registry.chat_ids() == (10, 20)
    path.unlink()
    assert not path.exists()


def test_user_registry_requests_owner_only_permissions(tmp_path) -> None:
    path = tmp_path / "users.sqlite3"
    registry = UserRegistry(path)

    with patch("mcc_bot.users.os.chmod") as chmod:
        registry.initialize()

    chmod.assert_called_once_with(path, 0o600)


def test_broadcast_message_reports_successes_and_failures(tmp_path) -> None:
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10)
    registry.remember(20)
    bot = AsyncMock()
    bot.send_message.side_effect = [object(), NetworkError("offline")]

    result = asyncio.run(broadcast_message(bot, registry, "Бот обновлён"))

    assert result.attempted == 2
    assert result.sent == 1
    assert result.failed == 1
    assert [call.kwargs["chat_id"] for call in bot.send_message.await_args_list] == [10, 20]
