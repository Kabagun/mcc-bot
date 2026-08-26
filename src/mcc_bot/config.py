"""Environment-based application settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .resources import DEFAULT_CATALOG_PATH, DEFAULT_DESCRIPTIONS_PATH

DEFAULT_USER_REGISTRY_PATH = Path("var/users.sqlite3")
DEFAULT_STORES_PATH = Path("var/stores.sqlite3")


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


def _owner_id() -> int:
    """Require an explicit positive Telegram user ID; never bootstrap from a chat."""

    raw_value = _required("BOT_OWNER_TELEGRAM_ID")
    if (
        not raw_value.isascii()
        or not raw_value.isdecimal()
        or len(raw_value) > 19
        or not 0 < int(raw_value) < 2**63
    ):
        raise SettingsError("BOT_OWNER_TELEGRAM_ID должен быть положительным ID пользователя")
    return int(raw_value)


@dataclass(frozen=True, slots=True)
class BotSettings:
    """Complete Telegram bot configuration."""

    token: str
    catalog_path: Path = DEFAULT_CATALOG_PATH
    descriptions_path: Path = DEFAULT_DESCRIPTIONS_PATH
    user_registry_path: Path = DEFAULT_USER_REGISTRY_PATH
    stores_path: Path = DEFAULT_STORES_PATH
    owner_telegram_id: int | None = None
    log_level: str = "INFO"

    @classmethod
    def from_environment(cls) -> BotSettings:
        """Load and validate all bot settings from process environment variables."""

        catalog_path = _path("MCC_CATALOG_PATH", DEFAULT_CATALOG_PATH)
        descriptions_path = _path("MCC_DESCRIPTIONS_PATH", DEFAULT_DESCRIPTIONS_PATH)
        user_registry_path = _path("MCC_USER_REGISTRY_PATH", DEFAULT_USER_REGISTRY_PATH)
        return cls(
            token=_required("TELEGRAM_BOT_TOKEN"),
            catalog_path=catalog_path,
            descriptions_path=descriptions_path,
            user_registry_path=user_registry_path,
            stores_path=_path("MCC_STORES_PATH", DEFAULT_STORES_PATH),
            owner_telegram_id=_owner_id(),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )
