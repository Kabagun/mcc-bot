"""Operator CLI for inspecting durable broadcast failures without Telegram IDs."""

from __future__ import annotations

import argparse
import os
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from .bot import load_environment
from .config import DEFAULT_USER_REGISTRY_PATH
from .users import BroadcastFailure, UserRegistry, redact_telegram_ids


def build_parser() -> argparse.ArgumentParser:
    """Build the broadcast failure-report parser."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latest", action="store_true", required=True, help="Show the latest run")
    parser.add_argument("--database", type=Path, help=argparse.SUPPRESS)
    return parser


def _registry_path(override: Path | None) -> Path:
    if override is not None:
        return override
    raw_value = os.getenv("MCC_USER_REGISTRY_PATH", "").strip()
    return Path(raw_value).expanduser() if raw_value else DEFAULT_USER_REGISTRY_PATH


def _identity(failure: BroadcastFailure) -> str:
    username = f"@{failure.username}" if failure.username else None
    display_name = " ".join(value for value in (failure.first_name, failure.last_name) if value)
    if username and display_name:
        return f"{username} ({display_name})"
    return username or display_name or "profile unavailable"


def _reason(failure: BroadcastFailure) -> str:
    reason = f"{failure.error_type}: {failure.error_text}"
    return redact_telegram_ids(reason, failure.chat_id)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the latest run, counters and exact per-recipient failure reasons."""

    args = build_parser().parse_args(argv)
    load_environment()
    registry = UserRegistry(_registry_path(args.database))
    if not registry.path.is_file():
        raise SystemExit(f"Broadcast registry not found: {registry.path}")
    try:
        run = registry.latest_broadcast_run()
    except sqlite3.Error as exc:
        raise SystemExit(f"Could not read broadcast log: {exc}") from exc
    if run is None:
        print("BROADCAST_RUN=none")
        return 0
    completed_at = run.completed_at or "-"
    print(
        f"BROADCAST_RUN={run.run_id} STATUS={run.status} "
        f"STARTED_AT={run.started_at} COMPLETED_AT={completed_at} "
        f"BROADCAST_RECIPIENTS={run.recipient_count}"
    )
    print(
        f"BROADCAST_ATTEMPTED={run.attempted} "
        f"BROADCAST_SENT={run.sent} BROADCAST_FAILED={run.failed}"
    )
    for failure in run.failures:
        print(f"FAILURE {_identity(failure)} — {_reason(failure)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
