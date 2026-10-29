from __future__ import annotations

from pathlib import Path

import pytest

from mcc_bot.bot import load_environment
from mcc_bot.config import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_DESCRIPTIONS_PATH,
    DEFAULT_STORES_PATH,
    DEFAULT_USER_REGISTRY_PATH,
    BotSettings,
    SettingsError,
)


def _set_required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("BOT_OWNER_TELEGRAM_ID", "12345")
    monkeypatch.delenv("MCC_FORM_FLOW_V2_USER_IDS", raising=False)
    monkeypatch.delenv("MCC_FORM_FLOW_V2_ALL_USERS", raising=False)


def test_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    with pytest.raises(SettingsError, match="TELEGRAM_BOT_TOKEN"):
        BotSettings.from_environment()


def test_from_environment_reads_catalog_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("MCC_CATALOG_PATH", "~/catalog.json")
    monkeypatch.setenv("MCC_DESCRIPTIONS_PATH", "~/descriptions.json")
    monkeypatch.setenv("MCC_USER_REGISTRY_PATH", "~/users.sqlite3")
    monkeypatch.setenv("MCC_STORES_PATH", "~/stores.sqlite3")

    settings = BotSettings.from_environment()

    assert settings.catalog_path == Path("~/catalog.json").expanduser()
    assert settings.descriptions_path == Path("~/descriptions.json").expanduser()
    assert settings.user_registry_path == Path("~/users.sqlite3").expanduser()
    assert settings.stores_path == Path("~/stores.sqlite3").expanduser()
    assert settings.owner_telegram_id == 12345


def test_default_catalog_paths_are_bundled_resources() -> None:
    assert DEFAULT_CATALOG_PATH.name == "cards.json"
    assert DEFAULT_DESCRIPTIONS_PATH.name == "mcc_descriptions.json"
    assert DEFAULT_CATALOG_PATH.is_file()
    assert DEFAULT_DESCRIPTIONS_PATH.is_file()
    assert Path("var/users.sqlite3") == DEFAULT_USER_REGISTRY_PATH
    assert Path("var/stores.sqlite3") == DEFAULT_STORES_PATH


@pytest.mark.parametrize(
    "raw",
    ["", "0", "-1", "1.5", "someone", "１２３", str(2**63), "9" * 5000],  # noqa: RUF001
)
def test_owner_must_be_an_explicit_positive_user_id(monkeypatch, raw):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("BOT_OWNER_TELEGRAM_ID", raw)
    with pytest.raises(SettingsError, match="BOT_OWNER_TELEGRAM_ID"):
        BotSettings.from_environment()


def test_form_rollout_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_environment(monkeypatch)

    settings = BotSettings.from_environment()

    assert settings.form_flow_v2_user_ids == frozenset()
    assert settings.form_flow_v2_all_users is False
    assert BotSettings(token="token").form_flow_v2_user_ids == frozenset()
    assert BotSettings(token="token").form_flow_v2_all_users is False


@pytest.mark.parametrize("raw", ["", " ", "\t"])
def test_empty_form_rollout_allowlist_stays_disabled(monkeypatch, raw):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("MCC_FORM_FLOW_V2_USER_IDS", raw)

    assert BotSettings.from_environment().form_flow_v2_user_ids == frozenset()


def test_form_rollout_reads_explicit_user_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("MCC_FORM_FLOW_V2_USER_IDS", f"12345, 23456,12345,{2**63 - 1}")

    settings = BotSettings.from_environment()

    assert settings.form_flow_v2_user_ids == frozenset({12345, 23456, 2**63 - 1})
    assert settings.form_flow_v2_all_users is False


@pytest.mark.parametrize(
    "raw",
    [
        ",12345",
        "12345,",
        "12345,,23456",
        "12345, ,23456",
        "0",
        "-12345",
        "+12345",
        "1.5",
        "true",
        "false",
        "someone",
        "１２３",  # noqa: RUF001
        "\u200312345",
        str(2**63),
        "9" * 5000,
    ],
)
def test_invalid_form_rollout_user_ids_are_rejected_without_echoing_values(monkeypatch, raw):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("MCC_FORM_FLOW_V2_USER_IDS", raw)

    with pytest.raises(SettingsError, match="MCC_FORM_FLOW_V2_USER_IDS") as error:
        BotSettings.from_environment()

    assert str(error.value) == (
        "MCC_FORM_FLOW_V2_USER_IDS должен содержать положительные ID через запятую"
    )


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", " Yes "])
def test_form_rollout_all_users_requires_explicit_true(monkeypatch, raw):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("MCC_FORM_FLOW_V2_ALL_USERS", raw)

    assert BotSettings.from_environment().form_flow_v2_all_users is True


@pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", " No "])
def test_form_rollout_all_users_accepts_explicit_false(monkeypatch, raw):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("MCC_FORM_FLOW_V2_ALL_USERS", raw)

    assert BotSettings.from_environment().form_flow_v2_all_users is False


@pytest.mark.parametrize("raw", ["", " ", "2", "on", "off", "truthy", "\u2003true", "да"])
def test_invalid_form_rollout_all_users_flag_is_rejected_without_echoing_value(monkeypatch, raw):
    _set_required_environment(monkeypatch)
    monkeypatch.setenv("MCC_FORM_FLOW_V2_ALL_USERS", raw)

    with pytest.raises(SettingsError, match="MCC_FORM_FLOW_V2_ALL_USERS") as error:
        BotSettings.from_environment()

    assert (
        str(error.value) == "MCC_FORM_FLOW_V2_ALL_USERS должен содержать true/false, 1/0 или yes/no"
    )


def test_load_environment_reads_dotenv_from_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_OWNER_TELEGRAM_ID", "12345")
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
    monkeypatch.setenv("BOT_OWNER_TELEGRAM_ID", "12345")
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=dotenv-token\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "process-token")

    load_environment()

    settings = BotSettings.from_environment()
    assert settings.token == "process-token"
