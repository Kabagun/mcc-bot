from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mcc_bot.broadcast import _broadcast, build_parser, main
from mcc_bot.config import BotSettings
from mcc_bot.users import BroadcastResult, BroadcastSafetyError


@pytest.mark.parametrize(
    "args",
    [[], ["--message", "text", "--message-file", "file.txt"]],
)
def test_broadcast_parser_requires_one_message_source(args) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(args)
    assert error.value.code == 2


def test_broadcast_cli_reads_exact_utf8_message_and_forwards_release(tmp_path, capsys) -> None:
    message = "  Обновление\r\n\r\n• Новое меню\r\n"
    message_path = tmp_path / "approved.txt"
    message_path.write_bytes(message.encode("utf-8"))
    settings = BotSettings(token="synthetic")
    send = AsyncMock(return_value=BroadcastResult(attempted=3, sent=2, failed=1))
    with (
        patch("mcc_bot.broadcast.load_environment"),
        patch("mcc_bot.broadcast.BotSettings.from_environment", return_value=settings),
        patch("mcc_bot.broadcast._broadcast", send),
    ):
        assert main(["--message-file", str(message_path), "--release-id", "test-release"]) == 0

    send.assert_awaited_once_with(settings, message, release_id="test-release")
    assert capsys.readouterr().out == "BROADCAST_SENT=2 BROADCAST_FAILED=1\n"


def test_broadcast_cli_preserves_existing_message_option(capsys) -> None:
    settings = BotSettings(token="synthetic")
    send = AsyncMock(return_value=BroadcastResult(attempted=1, sent=1, failed=0))
    with (
        patch("mcc_bot.broadcast.load_environment"),
        patch("mcc_bot.broadcast.BotSettings.from_environment", return_value=settings),
        patch("mcc_bot.broadcast._broadcast", send),
    ):
        assert main(["--message", "News"]) == 0

    send.assert_awaited_once_with(settings, "News", release_id=None)
    assert capsys.readouterr().out == "BROADCAST_SENT=1 BROADCAST_FAILED=0\n"


@pytest.mark.parametrize("contents", [b"\xff", b"", b"  \n"])
def test_broadcast_cli_rejects_unreadable_or_empty_message_before_loading_settings(
    tmp_path, contents
) -> None:
    message_path = tmp_path / "message.txt"
    message_path.write_bytes(contents)
    with patch("mcc_bot.broadcast.load_environment") as load, pytest.raises(SystemExit):
        main(["--message-file", str(message_path)])
    load.assert_not_called()


def test_broadcast_cli_file_error_does_not_expose_path(tmp_path) -> None:
    message_path = tmp_path / "private-name.txt"
    with pytest.raises(SystemExit) as error:
        main(["--message-file", str(message_path)])
    assert str(error.value) == "Cannot read message file as UTF-8"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            BroadcastSafetyError("Release has uncertain deliveries"),
            "Release has uncertain deliveries",
        ),
        (
            RuntimeError("synthetic token and chat id: 10"),
            "Broadcast interrupted; inspect its durable delivery state before retrying",
        ),
        (
            ValueError("synthetic token and chat id: 10"),
            "Broadcast interrupted; inspect its durable delivery state before retrying",
        ),
    ],
)
def test_broadcast_cli_only_exposes_static_safe_errors(failure, expected, capsys) -> None:
    with (
        patch("mcc_bot.broadcast.load_environment"),
        patch(
            "mcc_bot.broadcast.BotSettings.from_environment", return_value=BotSettings("synthetic")
        ),
        patch("mcc_bot.broadcast._broadcast", AsyncMock(side_effect=failure)),
        pytest.raises(SystemExit) as error,
    ):
        main(["--message", "News", "--release-id", "test"])

    assert str(error.value) == expected
    assert capsys.readouterr().out == ""


def test_broadcast_forwards_release_id_through_existing_registry_mechanism(tmp_path) -> None:
    settings = BotSettings(
        token="synthetic",
        user_registry_path=tmp_path / "users.sqlite3",
        stores_path=Path("missing"),
    )
    client = AsyncMock()
    expected = BroadcastResult(attempted=0, sent=0, failed=0)
    send = AsyncMock(return_value=expected)
    with (
        patch("mcc_bot.broadcast.Bot", return_value=client),
        patch("mcc_bot.broadcast.broadcast_message", send),
    ):
        result = asyncio.run(_broadcast(settings, "Exact text\n", release_id="release"))

    assert result == expected
    assert send.await_args.args[0] is client.__aenter__.return_value
    assert send.await_args.args[2] == "Exact text\n"
    assert send.await_args.kwargs == {"release_id": "release"}
