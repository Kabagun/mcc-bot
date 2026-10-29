from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mcc_bot.bot import build_application
from mcc_bot.config import BotSettings
from mcc_bot.form_rollout import FormFlowRollout, is_form_flow_v2_enabled


def _context(rollout=None):
    bot_data = {} if rollout is None else {"form_flow_v2": rollout}
    return SimpleNamespace(application=SimpleNamespace(bot_data=bot_data))


def test_missing_rollout_and_default_configuration_are_disabled():
    assert not is_form_flow_v2_enabled(_context(), 12345)
    assert not is_form_flow_v2_enabled(_context(FormFlowRollout()), 12345)


def test_rollout_configuration_is_immutable():
    rollout = FormFlowRollout(user_ids=frozenset({12345}))

    with pytest.raises(FrozenInstanceError):
        rollout.all_users = True


@pytest.mark.parametrize("role", ["owner", "superadmin", "helper", "user"])
def test_only_explicit_pilot_user_is_enabled_regardless_of_role(role):
    context = _context(FormFlowRollout(user_ids=frozenset({12345})))
    role_lookup = Mock(return_value=role)
    context.application.bot_data["community"] = SimpleNamespace(role=role_lookup)

    assert is_form_flow_v2_enabled(context, 12345)
    assert not is_form_flow_v2_enabled(context, 23456)
    role_lookup.assert_not_called()


def test_only_explicit_all_users_flag_enables_users_outside_allowlist():
    assert not FormFlowRollout(user_ids=frozenset({12345})).enabled_for(23456)
    assert FormFlowRollout(all_users=True).enabled_for(23456)
    assert not FormFlowRollout(all_users="true").enabled_for(23456)
    assert not FormFlowRollout(all_users=1).enabled_for(23456)


@pytest.mark.parametrize("user_id", [True, False, None, "12345", 12345.0, 0, -1, 2**63])
def test_invalid_user_id_fails_closed_even_with_all_users_enabled(user_id):
    assert not FormFlowRollout(all_users=True).enabled_for(user_id)
    assert not is_form_flow_v2_enabled(_context(FormFlowRollout(all_users=True)), user_id)


def test_boolean_in_allowlist_does_not_enable_user_with_numeric_identity():
    assert not FormFlowRollout(user_ids=frozenset({True})).enabled_for(1)


@pytest.mark.parametrize("rollout", [True, "true", {"all_users": True}, [12345]])
def test_unrecognized_rollout_objects_fail_closed(rollout):
    assert not is_form_flow_v2_enabled(_context(rollout), 12345)


@pytest.mark.parametrize(
    ("user_ids", "all_users"),
    [(frozenset(), False), (frozenset({12345}), False), (frozenset(), True)],
)
def test_build_application_installs_explicit_rollout_settings(
    tmp_path, monkeypatch, user_ids, all_users
):
    settings = BotSettings(
        token="123456:synthetic-token",
        user_registry_path=tmp_path / "users.sqlite3",
        stores_path=tmp_path / "stores.sqlite3",
        owner_telegram_id=23456,
        form_flow_v2_user_ids=user_ids,
        form_flow_v2_all_users=all_users,
    )
    monkeypatch.setattr("mcc_bot.bot.install_jobs", lambda application: None)

    application = build_application(settings)

    assert application.bot_data["form_flow_v2"] == FormFlowRollout(user_ids, all_users)
    assert application.bot_data["community"].role(23456) == "owner"
    assert application.bot_data["form_flow_v2"].enabled_for(23456) is all_users
