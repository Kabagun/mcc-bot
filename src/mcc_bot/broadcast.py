"""Operator CLI for notifying every remembered Telegram chat."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from telegram import Bot

from .bot import load_environment
from .config import BotSettings, SettingsError
from .users import BroadcastResult, BroadcastSafetyError, UserRegistry, broadcast_message

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the broadcast command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    messages = parser.add_mutually_exclusive_group(required=True)
    messages.add_argument("--message", help="Telegram message to broadcast")
    messages.add_argument(
        "--message-file", type=Path, help="UTF-8 file containing the exact Telegram message"
    )
    parser.add_argument(
        "--release-id",
        help="Stable release identifier: skip successful deliveries when retrying the same text",
    )
    return parser


async def _broadcast(
    settings: BotSettings, message: str, *, release_id: str | None = None
) -> BroadcastResult:
    registry = UserRegistry(settings.user_registry_path)
    registry.initialize()
    try:
        registry.backfill_profiles(settings.stores_path)
    except sqlite3.Error:
        LOGGER.warning("Could not backfill broadcast recipient profiles")
    async with Bot(settings.token) as bot:
        return await broadcast_message(bot, registry, message, release_id=release_id)


def main(argv: Sequence[str] | None = None) -> int:
    """Load settings, broadcast the requested text, and print delivery totals."""

    args = build_parser().parse_args(argv)
    try:
        # newline="" preserves approved line endings as well as leading/trailing spacing.
        if args.message_file is not None:
            with args.message_file.open(encoding="utf-8", newline="") as message_file:
                message = message_file.read()
        else:
            message = args.message
    except (OSError, UnicodeError):
        raise SystemExit("Cannot read message file as UTF-8") from None
    if not message or not message.strip():
        raise SystemExit("Broadcast message must not be empty")
    load_environment()
    try:
        settings = BotSettings.from_environment()
    except SettingsError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        result = asyncio.run(_broadcast(settings, message, release_id=args.release_id))
    except BroadcastSafetyError as exc:
        # These errors contain static operator instructions, never recipient or message data.
        raise SystemExit(str(exc)) from None
    except Exception:
        # Telegram/client exceptions can embed tokens, recipient IDs or request payloads.
        raise SystemExit(
            "Broadcast interrupted; inspect its durable delivery state before retrying"
        ) from None
    print(f"BROADCAST_SENT={result.sent} BROADCAST_FAILED={result.failed}")
    return 0 if result.sent or result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
