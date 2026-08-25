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


def test_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    with pytest.raises(SettingsError, match="TELEGRAM_BOT_TOKEN"):
        BotSettings.from_environment()


def test_from_environment_reads_catalog_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("MCC_CATALOG_PATH", "~/catalog.json")
    monkeypatch.setenv("MCC_DESCRIPTIONS_PATH", "~/descriptions.json")

    settings = BotSettings.from_environment()

    assert settings.catalog_path == Path("~/catalog.json").expanduser()
    assert settings.descriptions_path == Path("~/descriptions.json").expanduser()


def test_default_catalog_paths_are_bundled_resources() -> None:
    assert DEFAULT_CATALOG_PATH.name == "cards.json"
    assert DEFAULT_DESCRIPTIONS_PATH.name == "mcc_descriptions.json"
    assert DEFAULT_CATALOG_PATH.is_file()
    assert DEFAULT_DESCRIPTIONS_PATH.is_file()


def test_load_environment_reads_dotenv_from_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=dotenv-token\n", encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    load_environment()

    settings = BotSettings.from_environment()
    assert settings.token == "dotenv-token"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")


def test_load_environment_does_not_override_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=dotenv-token\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "process-token")

    load_environment()

    settings = BotSettings.from_environment()
    assert settings.token == "process-token"
