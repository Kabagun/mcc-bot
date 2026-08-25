from __future__ import annotations

from pathlib import Path

import pytest

from mcc_bot.bot import load_environment
from mcc_bot.config import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_DESCRIPTIONS_PATH,
    BotSettings,
    SettingsError,
)


def _set_required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "false")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123,456")


def test_from_environment_reads_catalog_and_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("MCC_CATALOG_PATH", "~/catalog.json")
    monkeypatch.setenv("MCC_DESCRIPTIONS_PATH", "~/descriptions.json")

    settings = BotSettings.from_environment()

    assert settings.allowed_user_ids == frozenset({123, 456})
    assert settings.catalog_path == Path("~/catalog.json").expanduser()
    assert settings.descriptions_path == Path("~/descriptions.json").expanduser()


def test_open_access_does_not_require_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "true")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)

    assert BotSettings.from_environment().open_access


def test_restricted_mode_requires_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "false")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)

    with pytest.raises(SettingsError, match="TELEGRAM_ALLOWED_USER_IDS"):
        BotSettings.from_environment()


def test_invalid_environment_values_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "maybe")
    with pytest.raises(SettingsError, match="true или false"):
        BotSettings.from_environment()

    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "false")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "not-a-number")
    with pytest.raises(SettingsError, match="числовые ID"):
        BotSettings.from_environment()


def test_default_catalog_paths_are_bundled_resources() -> None:
    assert DEFAULT_CATALOG_PATH.name == "cards.json"
    assert DEFAULT_DESCRIPTIONS_PATH.name == "mcc_descriptions.json"
    assert DEFAULT_CATALOG_PATH.is_file()
    assert DEFAULT_DESCRIPTIONS_PATH.is_file()


def test_load_environment_reads_dotenv_from_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=dotenv-token\nTELEGRAM_OPEN_ACCESS=true\n", encoding="utf-8"
    )
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OPEN_ACCESS", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)

    load_environment()

    settings = BotSettings.from_environment()
    assert settings.token == "dotenv-token"
    assert settings.open_access
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("TELEGRAM_OPEN_ACCESS")


def test_load_environment_does_not_override_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=dotenv-token\nTELEGRAM_OPEN_ACCESS=true\n", encoding="utf-8"
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "process-token")
    monkeypatch.setenv("TELEGRAM_OPEN_ACCESS", "false")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")

    load_environment()

    settings = BotSettings.from_environment()
    assert settings.token == "process-token"
    assert not settings.open_access
