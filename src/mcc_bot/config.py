"""Environment-based application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CATALOG_PATH = Path("data/cards.json")


class SettingsError(ValueError):
    """Raised when required application configuration is missing or invalid."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SettingsError(f"Required environment variable is missing: {name}")
    return value


def _boolean(name: str, *, default: bool = False) -> bool:
    raw_value = os.getenv(name, str(default)).strip().lower()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} must be true or false")


def _allowed_user_ids(raw_value: str) -> frozenset[int]:
    values = [part.strip() for part in raw_value.replace(";", ",").split(",")]
    try:
        result = frozenset(int(value) for value in values if value)
    except ValueError as exc:
        raise SettingsError("TELEGRAM_ALLOWED_USER_IDS must contain numeric IDs") from exc
    if any(value < 0 for value in result):
        raise SettingsError("TELEGRAM_ALLOWED_USER_IDS must contain non-negative IDs")
    return result


@dataclass(frozen=True, slots=True)
class BotSettings:
    """Complete Telegram bot configuration."""

    token: str
    open_access: bool
    allowed_user_ids: frozenset[int]
    catalog_path: Path = DEFAULT_CATALOG_PATH
    log_level: str = "INFO"

    @classmethod
    def from_environment(cls) -> BotSettings:
        """Load and validate all bot settings from process environment variables."""

        open_access = _boolean("TELEGRAM_OPEN_ACCESS")
        allowed_user_ids = _allowed_user_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))
        if not open_access and not allowed_user_ids:
            raise SettingsError(
                "Set TELEGRAM_OPEN_ACCESS=true or provide TELEGRAM_ALLOWED_USER_IDS"
            )
        catalog_path = Path(os.getenv("MCC_CATALOG_PATH", str(DEFAULT_CATALOG_PATH))).expanduser()
        return cls(
            token=_required("TELEGRAM_BOT_TOKEN"),
            open_access=open_access,
            allowed_user_ids=allowed_user_ids,
            catalog_path=catalog_path,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )
