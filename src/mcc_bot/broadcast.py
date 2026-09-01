"""Operator CLI for notifying every remembered Telegram chat."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
from collections.abc import Sequence

from telegram import Bot

from .bot import load_environment
from .config import BotSettings, SettingsError
from .users import BroadcastResult, UserRegistry, broadcast_message

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the broadcast command-line parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", required=True, help="Telegram message to broadcast")
    return parser


async def _broadcast(settings: BotSettings, message: str) -> BroadcastResult:
    registry = UserRegistry(settings.user_registry_path)
    registry.initialize()
    try:
        registry.backfill_profiles(settings.stores_path)
    except sqlite3.Error:
        LOGGER.warning("Could not backfill broadcast recipient profiles")
    async with Bot(settings.token) as bot:
        return await broadcast_message(bot, registry, message)


def main(argv: Sequence[str] | None = None) -> int:
    """Load settings, broadcast the requested text, and print delivery totals."""

    args = build_parser().parse_args(argv)
    load_environment()
    try:
        settings = BotSettings.from_environment()
    except SettingsError as exc:
        raise SystemExit(str(exc)) from exc
    result = asyncio.run(_broadcast(settings, args.message))
    print(
        f"BROADCAST_ATTEMPTED={result.attempted} "
        f"BROADCAST_SENT={result.sent} BROADCAST_FAILED={result.failed}"
    )
    return 0 if result.sent or result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
