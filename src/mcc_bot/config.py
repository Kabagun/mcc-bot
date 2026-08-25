"""Environment-based application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .resources import DEFAULT_CATALOG_PATH, DEFAULT_DESCRIPTIONS_PATH


class SettingsError(ValueError):
    """Raised when required application configuration is missing or invalid."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SettingsError(
            f"Не задана обязательная переменная окружения: {name}"  # noqa: RUF001
        )
    return value


def _path(name: str, default: Path) -> Path:
    raw_value = os.getenv(name, "").strip()
    return Path(raw_value).expanduser() if raw_value else default


@dataclass(frozen=True, slots=True)
class BotSettings:
    """Complete Telegram bot configuration."""

    token: str
    catalog_path: Path = DEFAULT_CATALOG_PATH
    descriptions_path: Path = DEFAULT_DESCRIPTIONS_PATH
    log_level: str = "INFO"

    @classmethod
    def from_environment(cls) -> BotSettings:
        """Load and validate all bot settings from process environment variables."""

        catalog_path = _path("MCC_CATALOG_PATH", DEFAULT_CATALOG_PATH)
        descriptions_path = _path("MCC_DESCRIPTIONS_PATH", DEFAULT_DESCRIPTIONS_PATH)
        return cls(
            token=_required("TELEGRAM_BOT_TOKEN"),
            catalog_path=catalog_path,
            descriptions_path=descriptions_path,
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )
