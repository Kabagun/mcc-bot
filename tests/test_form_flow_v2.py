"""Pilot form workflows with real services and synthetic Telegram updates."""

# Russian product copy is intentional.
# ruff: noqa: RUF001

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_community_handlers import (
    all_buttons,
    click,
    draft_click,
    merchant,
    proposal_flow,
    send,
    start_data,
    update,
)
from test_community_handlers import (
    flow as flow,
)

from mcc_bot.community_handlers import callback
from mcc_bot.form_rollout import FormFlowRollout
from mcc_bot.partner_rewards import PartnerRepository


def _pilot(flow, *user_ids):
    flow.application.bot_data["form_flow_v2"] = FormFlowRollout(user_ids=frozenset(user_ids))
    return flow.application.bot_data["community"]


def _text(event):
    return "\n".join(call.args[0] for call in event.effective_message.reply_text.await_args_list)


def _labels(event):
    return [button.text for button in all_buttons(event)]


def _partner(flow):
    service = flow.application.bot_data["community"]
    partners = PartnerRepository(service.stores)
    partners.initialize()
    card = SimpleNamespace(
        id="pilot-card",
        name="Тестовая карта",
        partner_policy=SimpleNamespace(mode="additional", reward_kind="points"),
        partner_policy_explicit=True,
    )
    catalog = SimpleNamespace(cards=(card,))
    service.partners = partners
    service.catalog = catalog
    flow.application.bot_data.update(partners=partners, catalog=catalog)
    merchant(flow, "Тестовый партнёр")
    return service


def test_pilot_is_not_automatically_enabled_for_other_privileged_users(flow):
    service = _pilot(flow, 2)
    service.set_role(1, 2, True, role="superadmin")
    service.set_role(1, 3, True)
    service.set_role(1, 3, True, role="superadmin")
    start_data(flow, user=2)
    assert service.draft(2).data.get("form_flow_v2") is True
    for user_id in (1, 3, 10):
        event = start_data(flow, user=user_id)
        assert not service.draft(user_id).data.get("form_flow_v2")
        assert service.draft(user_id).data.get("active_field") is None
        assert "Поля можно заполнять в любом порядке." in _text(event)


def test_existing_legacy_draft_does_not_switch_when_user_joins_pilot(flow):
    start_data(flow, user=2)
    service = _pilot(flow, 2)
    existing = service.draft(2)
    draft_click(flow, "form_field:mcc", 2)
    send(flow, "5411", 2)
    current = service.draft(2)
    assert current.id == existing.id
    assert not current.data.get("form_flow_v2")
    assert current.data.get("active_field") is None


def test_guided_new_store_reaches_review_without_opening_each_field(flow):
    service = _pilot(flow, 10)
    started = start_data(flow)
    assert service.draft(10).data.get("active_field") == "brand_id"
    assert "Источник: не заполнено" not in _text(started)
    send(flow, "Новый тестовый магазин")
    draft_click(flow, "form_new_store")
    assert service.draft(10).data.get("active_field") == "mcc"
    send(flow, "5411")
    assert service.draft(10).data.get("active_field") == "channel"
    ready = draft_click(flow, "form_channel:offline")
    assert service.draft(10).data.get("active_field") is None
    assert "Отправить на проверку" in _labels(ready)
    saved = draft_click(flow, "form_save")
    assert service.draft(10) is None
    assert service.own_proposals(10)[0].status == "pending"
    assert "Новый тестовый магазин" in _text(saved)


def test_back_from_payment_picker_keeps_entered_values_and_editable_summary(flow):
    service = _pilot(flow, 2)
    start_data(flow, user=2)
    send(flow, "Магазин возврата", 2)
    draft_click(flow, "form_new_store", 2)
    send(flow, "5411", 2)
    values = service.draft(2).data["values"].copy()
    returned = draft_click(flow, "form_back", 2)
    assert service.draft(2).data["values"] == values
    assert service.draft(2).data.get("active_field") is None
    assert any("MCC" in label for label in _labels(returned))
    draft_click(flow, "form_field:channel", 2)
    ready = draft_click(flow, "form_channel:offline", 2)
    assert "Опубликовать" in _labels(ready)


def test_duplicate_mcc_can_be_corrected_and_published_from_same_pilot_form(flow):
    service = _pilot(flow, 2)
    merchant(flow, "Магазин дубликата")
    start_data(flow, user=2)
    send(flow, "Магазин дубликата", 2)
    send(flow, "5411", 2)
    draft_click(flow, "form_channel:offline", 2)
    before = service.draft(2)
    rejected = draft_click(flow, "form_save", 2)
    current = service.draft(2)
    assert current.stage == "form_editor"
    assert current.id == before.id
    assert current.version == before.version
    assert current.data["values"] == before.data["values"]
    assert "MCC" in _text(rejected)
    draft_click(flow, "form_field:mcc", 2)
    send(flow, "5812", 2)
    draft_click(flow, "form_save", 2)
    assert service.draft(2) is None
    proposal = service.own_proposals(2)[0]
    assert proposal.status == "approved"
    assert proposal.payload["mcc"] == "5812"


@pytest.mark.parametrize("invalid_rate", ["101", "0", "-1", "NaN"])
def test_pilot_partner_rate_is_checked_immediately_and_can_be_corrected(flow, invalid_rate):
    service = _pilot(flow, 2)
    _partner(flow)
    start_data(flow, user=2, kind="partner")
    send(flow, "Тестовый партнёр", 2)
    assert service.draft(2).data["active_field"] == "card_id"
    draft_click(flow, "form_card:0", 2)
    draft_click(flow, "form_channel:any", 2)
    assert service.draft(2).data["active_field"] == "value"
    before = service.draft(2)
    send(flow, invalid_rate, 2)
    after = service.draft(2)
    assert after.stage == "form_editor"
    assert after.version == before.version
    assert after.data["active_field"] == "value"
    assert "value" not in after.data["values"]
    ready = send(flow, "2,5", 2)
    assert "2,5%" in _text(ready) or "2.5%" in _text(ready)
    assert "балл" in _text(ready).lower()
    assert "Опубликовать" in _labels(ready)
    draft_click(flow, "form_save", 2)
    assert service.draft(2) is None
    assert service.own_proposals(2)[0].status == "approved"


@pytest.mark.parametrize("reviewer_id", [1, 2])
def test_rejection_reason_uses_pilot_wording_only_for_enrolled_reviewer(flow, reviewer_id):
    service = _pilot(flow, 2)
    proposal = proposal_flow(flow)
    click(flow, f"q:{proposal.id}:{proposal.version}:claim", reviewer_id)
    claimed = service.proposal(reviewer_id, proposal.id)
    opened = click(flow, f"q:{proposal.id}:{claimed.version}:reject_reason", reviewer_id)
    if reviewer_id == 1:
        assert "Что нужно уточнить у автора?" in _text(opened)
    else:
        assert "отклон" in _text(opened).lower()
        assert "Что нужно уточнить у автора?" not in _text(opened)
        preview = send(flow, "Этот MCC уже подтверждён для магазина.", reviewer_id)
        assert "Отклонить заявку" in _labels(preview)


def test_pilot_clarification_button_says_send_question(flow):
    service = _pilot(flow, 2)
    proposal = proposal_flow(flow)
    click(flow, f"q:{proposal.id}:{proposal.version}:claim", 2)
    claimed = service.proposal(2, proposal.id)
    click(flow, f"q:{proposal.id}:{claimed.version}:clarify", 2)
    preview = send(flow, "В каком магазине прошла эта оплата?", 2)
    assert "Отправить вопрос" in _labels(preview)


def test_pilot_invalid_save_updates_the_bound_message_without_a_duplicate_form(flow):
    service = _pilot(flow, 2)
    merchant(flow, "Bound shop")
    flow.bot.edit_message_text = AsyncMock()
    flow.bot.delete_message = AsyncMock()
    opened = update(2, data="community:add:store_mcc")
    opened.effective_message.reply_text.side_effect = [
        SimpleNamespace(chat_id=2, message_id=500),
        SimpleNamespace(chat_id=2, message_id=501),
    ]
    asyncio.run(callback(opened, flow))
    assert service.editor_message(2, service.draft(2).id) == (2, 501)
    named = send(flow, "Bound shop", 2)
    named.effective_message.reply_text.assert_not_awaited()
    send(flow, "5411", 2)

    def bound_click(action):
        draft = service.draft(2)
        event = update(2, data=f"community:d:{draft.id}:{draft.version}:{action}")
        event.callback_query.message = SimpleNamespace(chat_id=2, message_id=501)
        event.callback_query.edit_message_text = AsyncMock()
        asyncio.run(callback(event, flow))
        event.effective_message.reply_text.assert_not_awaited()
        event.callback_query.edit_message_text.assert_awaited_once()
        return event

    bound_click("form_channel:offline")
    before = service.draft(2)
    rejected = bound_click("form_save")
    assert service.draft(2) == before
    assert service.editor_message(2, before.id) == (2, 501)
    assert "MCC" in rejected.callback_query.edit_message_text.await_args.args[0]
    flow.bot.delete_message.assert_not_awaited()
