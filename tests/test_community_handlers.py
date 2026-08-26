"""Telegram-visible workflow tests with real state and mocked network calls."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden

from mcc_bot.community import CommunityService
from mcc_bot.community_handlers import (
    ADD,
    INFO,
    MANAGE,
    MINE,
    QUEUE,
    SUGGEST,
    callback,
    handle_media,
    handle_text,
    keyboard_for,
    show_menu,
)
from mcc_bot.stores import StoreRepository


@pytest.fixture
def flow(tmp_path):
    service = CommunityService(StoreRepository(tmp_path / "stores.sqlite3"), owner_id=1)
    service.initialize()
    service.set_role(1, 2, True)
    bot = SimpleNamespace(send_message=AsyncMock())
    return SimpleNamespace(application=SimpleNamespace(bot_data={"community": service}), bot=bot)


def update(
    user=10,
    *,
    text=None,
    data=None,
    chat_type="private",
    update_id=None,
    photo=None,
    caption=None,
    document=None,
):
    message = SimpleNamespace(
        text=text,
        photo=photo,
        caption=caption,
        document=document,
        reply_text=AsyncMock(),
        reply_photo=AsyncMock(),
        is_accessible=True,
    )
    query = SimpleNamespace(data=data, answer=AsyncMock(), message=message) if data else None
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user),
        effective_chat=SimpleNamespace(id=user, type=chat_type),
        effective_message=message,
        callback_query=query,
        update_id=update_id,
    )


def send(flow, text, user=10, **kwargs):
    event = update(user, text=text, **kwargs)
    assert asyncio.run(handle_text(event, flow))
    return event


def click(flow, action, user=10):
    event = update(user, data="community:" + action)
    asyncio.run(callback(event, flow))
    return event


def draft_click(flow, action, user=10):
    draft = flow.application.bot_data["community"].draft(user)
    return click(flow, f"d:{draft.id}:{draft.version}:{action}", user)


def screenshot(flow, user=10, *, caption="Test payment", size=2000, file_id="secret"):
    event = update(
        user,
        caption=caption,
        photo=[SimpleNamespace(file_id=file_id, file_unique_id="unique", file_size=size)],
    )
    assert asyncio.run(handle_media(event, flow))
    return event


def merchant(flow, name="Shop", mcc="5411"):
    return (
        flow.application.bot_data["community"]
        .stores.apply_change("add_merchant", {"name": name, "channel": "offline", "mcc": mcc}, 1)
        .merchant_id
    )


def proposal_flow(flow, *, user=10):
    send(flow, SUGGEST, user)
    send(flow, "New shop", user)
    draft_click(flow, "new", user)
    draft_click(flow, "channel:offline", user)
    send(flow, "5411", user)
    screenshot(flow, user)
    draft_click(flow, "submit", user)
    return flow.application.bot_data["community"].own_proposals(user)[0]


def all_buttons(event):
    result = []
    for call in event.effective_message.reply_text.await_args_list:
        markup = call.kwargs.get("reply_markup")
        if markup and hasattr(markup, "inline_keyboard"):
            result.extend(button for row in markup.inline_keyboard for button in row)
    return result


def test_persistent_role_keyboard_and_durable_start_resume(flow):
    service = flow.application.bot_data["community"]
    assert keyboard_for(service, 10).is_persistent
    assert [button.text for row in keyboard_for(service, 10).keyboard for button in row] == [
        INFO,
        SUGGEST,
        MINE,
    ]
    assert [button.text for row in keyboard_for(service, 2).keyboard for button in row] == [
        INFO,
        ADD,
        QUEUE,
        MANAGE,
    ]
    send(flow, SUGGEST)
    current = service.draft(10)
    event = update()
    asyncio.run(show_menu(event, flow))
    assert (
        "четырёхзначный MCC или название магазина"
        in event.effective_message.reply_text.await_args_list[0].args[0]
    )
    assert service.draft(10) == current
    assert any(button.text == "Продолжить" for button in all_buttons(event))


def test_user_flow_requires_screenshot_preview_and_submit(flow):
    send(flow, SUGGEST)
    send(flow, "New shop")
    draft_click(flow, "new")
    draft_click(flow, "channel:offline")
    send(flow, "5411")
    service = flow.application.bot_data["community"]
    blocked = draft_click(flow, "skip")
    assert "скриншот" in blocked.effective_message.reply_text.await_args.args[0]
    assert service.draft(10).stage == "evidence"
    preview = screenshot(flow)
    assert any(button.text == "Отправить на проверку" for button in all_buttons(preview))
    draft = service.draft(10)
    action = f"d:{draft.id}:{draft.version}:submit"
    click(flow, action)
    click(flow, action)
    assert len(service.own_proposals(10)) == 1
    assert not service.stores.search("New shop").matches


def test_admin_direct_save_optional_screenshot(flow):
    send(flow, ADD, 2)
    send(flow, "Trusted shop", 2)
    draft_click(flow, "new", 2)
    draft_click(flow, "channel:online", 2)
    send(flow, "5812", 2)
    draft_click(flow, "skip", 2)
    event = draft_click(flow, "skip", 2)
    assert any(button.text == "Сохранить в базу" for button in all_buttons(event))
    draft_click(flow, "submit", 2)
    service = flow.application.bot_data["community"]
    assert service.own_proposals(2)[0].status == "approved"
    assert service.stores.find_exact("Trusted shop", "online")


def test_context_preselection_and_global_button_starts_from_scratch(flow):
    merchant_id = merchant(flow)
    click(flow, f"start:{merchant_id}")
    service = flow.application.bot_data["community"]
    assert service.draft(10).data["merchant_id"] == merchant_id
    send(flow, SUGGEST)
    assert service.draft(10).stage == "name"
    assert "merchant_id" not in service.draft(10).data


def test_private_only_and_effective_user_not_chat_identity(flow):
    event = update(user=10, text=ADD, chat_type="group")
    asyncio.run(handle_text(event, flow))
    assert flow.application.bot_data["community"].draft(10) is None
    event = update(user=10, data="community:edit")
    event.effective_chat.id = 1
    asyncio.run(callback(event, flow))
    assert flow.application.bot_data["community"].draft(10) is None
    assert "доступ" in event.effective_message.reply_text.await_args.args[0].lower()
    event = update(user=1, data="community:role:10:0:1", chat_type="supergroup")
    asyncio.run(callback(event, flow))
    assert not flow.application.bot_data["community"].is_admin(10)


def test_user_cannot_borrow_another_draft_callback(flow):
    send(flow, SUGGEST, 10)
    service = flow.application.bot_data["community"]
    draft = service.draft(10)
    click(flow, f"d:{draft.id}:{draft.version}:cancel", 11)
    assert service.draft(10) == draft


def test_revoked_reviewer_stale_preview_cannot_save(flow):
    merchant_id = merchant(flow)
    click(flow, f"edit:{merchant_id}", 2)
    draft_click(flow, "operation:rename_merchant", 2)
    send(flow, "Changed name", 2)
    draft_click(flow, "skip", 2)
    service = flow.application.bot_data["community"]
    draft = service.draft(2)
    service.set_role(1, 2, False)
    click(flow, f"d:{draft.id}:{draft.version}:submit", 2)
    assert service.stores.get(merchant_id).name == "Shop"


def test_approval_notifies_author_and_forbidden_does_not_undo(flow):
    proposal = proposal_flow(flow)
    service = flow.application.bot_data["community"]
    click(flow, f"q:{proposal.id}:{proposal.version}:claim", 2)
    claimed = service.proposal(2, proposal.id)
    flow.bot.send_message.side_effect = Forbidden("blocked")
    event = click(flow, f"q:{proposal.id}:{claimed.version}:approve", 2)
    assert service.proposal(2, proposal.id).status == "approved"
    assert "сохранено" in event.effective_message.reply_text.await_args.args[0]


def test_clarification_answer_and_cancel(flow):
    proposal = proposal_flow(flow)
    service = flow.application.bot_data["community"]
    click(flow, f"q:{proposal.id}:{proposal.version}:claim", 2)
    claimed = service.proposal(2, proposal.id)
    click(flow, f"q:{proposal.id}:{claimed.version}:clarify", 2)
    send(flow, "Which address?", 2)
    draft_click(flow, "decision", 2)
    asked = service.proposal(10, proposal.id)
    assert asked.status == "clarification"
    assert "Which address?" in flow.bot.send_message.await_args.kwargs["text"]
    click(flow, f"respond:{proposal.id}:{asked.version}")
    send(flow, "Town centre")
    draft_click(flow, "skip")
    draft_click(flow, "submit")
    pending = service.proposal(10, proposal.id)
    assert pending.status == "pending"
    click(flow, f"cancel:{proposal.id}:{pending.version}")
    assert service.proposal(10, proposal.id).status == "cancelled"


def test_role_request_grant_revoke_decline_and_consent_epoch(flow):
    service = flow.application.bot_data["community"]
    click(flow, "volunteer")
    roles = click(flow, "roles", 1)
    assert any("Назначить" in button.text for button in all_buttons(roles))
    assert any(button.text == "Отказать" for button in all_buttons(roles))
    click(flow, "role:10:0:1", 1)
    assert service.is_admin(10)
    assert flow.bot.send_message.await_args.kwargs["chat_id"] == 10
    epoch = service.role_epoch(10)
    click(flow, f"digest:1:{epoch}")
    assert service.digest_enabled(10)
    click(flow, f"role:10:{epoch}:0", 1)
    service.set_role(1, 10, True)
    click(flow, f"digest:1:{epoch}")
    assert not service.digest_enabled(10)
    click(flow, "volunteer", 11)
    click(flow, "decline:11:0", 1)
    assert not service.is_admin(11)
    click(flow, "role:11:0:1", 1)
    assert not service.is_admin(11)


def test_media_bounds_document_rejection_privacy_and_unavailable_photo(flow):
    send(flow, SUGGEST)
    send(flow, "Shop")
    draft_click(flow, "new")
    draft_click(flow, "channel:offline")
    send(flow, "5411")
    event = screenshot(flow, size=11 * 1024 * 1024)
    assert "10 МБ" in event.effective_message.reply_text.await_args.args[0]
    event = update(document=SimpleNamespace(mime_type="image/png", file_id="doc", file_size=100))
    asyncio.run(handle_media(event, flow))
    assert "фотографию" in event.effective_message.reply_text.await_args.args[0]
    screenshot(flow)
    draft_click(flow, "submit")
    proposal = flow.application.bot_data["community"].own_proposals(10)[0]
    denied = click(flow, f"media:{proposal.id}", 11)
    denied.effective_message.reply_photo.assert_not_awaited()
    event = update(user=2, data=f"community:media:{proposal.id}")
    event.effective_message.reply_photo.side_effect = BadRequest("file not available")
    asyncio.run(callback(event, flow))
    assert event.effective_message.reply_photo.await_args.kwargs["protect_content"]
    assert "недоступен" in event.effective_message.reply_text.await_args.args[0]


@pytest.mark.parametrize(
    "operation", ["add_mcc", "replace_mcc", "rename_merchant", "merge_merchant"]
)
def test_unified_report_contains_and_opens_each_operation(flow, operation):
    merchant_id = merchant(flow)
    event = click(flow, f"report:{merchant_id}")
    assert (
        len([button for button in all_buttons(event) if "operation:" in button.callback_data]) == 4
    )
    draft_click(flow, "operation:" + operation)
    service = flow.application.bot_data["community"]
    assert service.draft(10).data["kind"] == operation
    if operation == "replace_mcc":
        draft_click(flow, "old:5411")
        send(flow, "5812")
        screenshot(flow)
        draft_click(flow, "submit")
        proposal = service.own_proposals(10)[0]
        assert proposal.kind == "replace_mcc"
        assert proposal.payload == {"merchant_id": merchant_id, "old_mcc": "5411", "mcc": "5812"}


@pytest.mark.parametrize(
    "operation,value,expected",
    [
        ("rename_merchant", "New name", "New name"),
        ("aliases", "Alias one, Alias two", ("Alias one", "Alias two")),
    ],
)
def test_editor_name_and_alias_changes_require_preview(flow, operation, value, expected):
    merchant_id = merchant(flow)
    click(flow, f"edit:{merchant_id}", 2)
    draft_click(flow, "operation:" + operation, 2)
    send(flow, value, 2)
    draft_click(flow, "skip", 2)
    service = flow.application.bot_data["community"]
    assert service.stores.get(merchant_id).name == "Shop"
    draft_click(flow, "submit", 2)
    result = service.stores.get(merchant_id)
    assert (result.name if operation == "rename_merchant" else result.aliases) == expected


def test_editor_archive_history_and_safe_undo(flow):
    merchant_id = merchant(flow)
    click(flow, f"edit:{merchant_id}", 2)
    draft_click(flow, "operation:archive_mcc", 2)
    draft_click(flow, "old:5411", 2)
    draft_click(flow, "submit", 2)
    service = flow.application.bot_data["community"]
    assert not service.stores.list_mcc(merchant_id)
    click(flow, f"edit:{merchant_id}", 2)
    draft_click(flow, "history", 2)
    last = service.stores.history(merchant_id)[0]
    draft_click(flow, f"undo:{last.id}", 2)
    draft_click(flow, "submit", 2)
    assert service.stores.list_mcc(merchant_id)[0].mcc == "5411"


def test_editor_merge_and_archive_store(flow):
    source = merchant(flow, "Source")
    target = merchant(flow, "Target", "5812")
    click(flow, f"edit:{source}", 2)
    draft_click(flow, "operation:merge_merchant", 2)
    send(flow, "Target", 2)
    draft_click(flow, f"select:{target}", 2)
    draft_click(flow, "skip", 2)
    draft_click(flow, "submit", 2)
    service = flow.application.bot_data["community"]
    assert not service.stores.get(source)
    assert {item.mcc for item in service.stores.list_mcc(target)} == {"5411", "5812"}
    click(flow, f"edit:{target}", 2)
    draft_click(flow, "operation:archive_merchant", 2)
    draft_click(flow, "submit", 2)
    assert not service.stores.get(target)


@pytest.mark.parametrize(
    "action",
    [
        "media:9223372036854775808",
        "own:9223372036854775808",
        "start:9223372036854775808",
        "report:9223372036854775808",
        "edit:9223372036854775808",
        "cancel:9223372036854775808:1",
        "q:9223372036854775808:1:claim",
        "role:9223372036854775808:0:1",
        "media:-9223372036854775809",
    ],
)
def test_callback_numeric_ids_are_bounded_before_sqlite(flow, action):
    event = click(flow, action, 1)
    assert "Кнопка устарела" in event.effective_message.reply_text.await_args.args[0]


def test_channel_back_navigation_restores_existing_merchant(flow):
    merchant_id = merchant(flow)
    click(flow, f"start:{merchant_id}")
    draft_click(flow, "channel:online")
    draft_click(flow, "back")
    draft_click(flow, "channel:offline")
    send(flow, "5812")
    screenshot(flow)
    service = flow.application.bot_data["community"]
    draft = service.draft(10)
    assert draft.data["kind"] == "add_mcc"
    assert draft.data["payload"] == {"merchant_id": merchant_id, "mcc": "5812"}


def test_back_out_of_report_starts_new_contribution_without_stale_state(flow):
    merchant_id = merchant(flow)
    click(flow, f"report:{merchant_id}")
    draft_click(flow, "operation:replace_mcc")
    draft_click(flow, "old:5411")
    for _ in range(3):
        draft_click(flow, "back")
    send(flow, "New merchant")
    draft_click(flow, "new")
    draft_click(flow, "channel:offline")
    send(flow, "5812")
    screenshot(flow)
    assert flow.application.bot_data["community"].draft(10).data["kind"] == "add_merchant"


def test_archived_store_is_recoverable_from_management_history(flow):
    merchant_id = merchant(flow)
    click(flow, f"edit:{merchant_id}", 2)
    draft_click(flow, "operation:archive_merchant", 2)
    draft_click(flow, "submit", 2)
    service = flow.application.bot_data["community"]
    assert service.stores.get(merchant_id) is None
    event = click(flow, "recent", 2)
    assert any(
        button.callback_data == f"community:history:{merchant_id}" for button in all_buttons(event)
    )
    click(flow, f"history:{merchant_id}", 2)
    audit_id = service.stores.history(merchant_id)[0].id
    draft_click(flow, f"undo:{audit_id}", 2)
    draft_click(flow, "submit", 2)
    assert service.stores.get(merchant_id)


def test_recent_history_page_two_restores_an_older_archived_store(flow):
    service = flow.application.bot_data["community"]
    merchant_id = merchant(flow, "Archived older shop")
    archived = service.stores.apply_change("archive_merchant", {"merchant_id": merchant_id}, 1)
    for index in range(12):
        merchant(flow, f"Later shop {index}")
    first = click(flow, "recent", 2)
    assert all(
        button.callback_data != f"community:history:{merchant_id}" for button in all_buttons(first)
    )
    assert any(button.callback_data == "community:recent:10" for button in all_buttons(first))
    second = click(flow, "recent:10", 2)
    assert any(
        button.callback_data == f"community:history:{merchant_id}" for button in all_buttons(second)
    )
    assert any(button.callback_data == "community:recent:0" for button in all_buttons(second))
    assert all(len(button.callback_data.encode()) <= 64 for button in all_buttons(second))
    click(flow, f"history:{merchant_id}", 2)
    draft_click(flow, f"undo:{archived.audit_id}", 2)
    draft_click(flow, "submit", 2)
    assert service.stores.get(merchant_id)


def test_merchant_history_pages_allow_older_displayed_addition_to_be_undone(flow):
    service = flow.application.bot_data["community"]
    merchant_id = merchant(flow)
    changes = [
        service.stores.apply_change(
            "add_mcc", {"merchant_id": merchant_id, "mcc": f"{5000 + index:04d}"}, 1
        )
        for index in range(12)
    ]
    first = click(flow, f"history:{merchant_id}", 2)
    assert service.draft(2).data["history_offset"] == 0
    assert len(service.draft(2).data["history_ids"]) == 10
    assert any(button.callback_data.endswith(":history_page:10") for button in all_buttons(first))
    second = draft_click(flow, "history_page:10", 2)
    assert service.draft(2).data["history_offset"] == 10
    assert changes[0].audit_id in service.draft(2).data["history_undo_ids"]
    assert any(button.callback_data.endswith(":history_page:0") for button in all_buttons(second))
    assert all(len(button.callback_data.encode()) <= 64 for button in all_buttons(second))
    draft_click(flow, f"undo:{changes[0].audit_id}", 2)
    draft_click(flow, "submit", 2)
    assert "5000" not in {fact.mcc for fact in service.stores.list_mcc(merchant_id)}
    assert "5011" in {fact.mcc for fact in service.stores.list_mcc(merchant_id)}


def test_history_undo_rejects_other_page_stale_and_newly_inserted_forged_entries(flow):
    service = flow.application.bot_data["community"]
    merchant_id = merchant(flow)
    changes = [
        service.stores.apply_change(
            "add_mcc", {"merchant_id": merchant_id, "mcc": f"{5000 + index:04d}"}, 1
        )
        for index in range(12)
    ]
    click(flow, f"history:{merchant_id}", 2)
    first = service.draft(2)
    rejected = draft_click(flow, f"undo:{changes[0].audit_id}", 2)
    assert "не показано" in rejected.effective_message.reply_text.await_args.args[0]
    assert service.draft(2).stage == "history"
    inserted = service.stores.apply_change(
        "add_mcc", {"merchant_id": merchant_id, "mcc": "5999"}, 1
    )
    rejected = draft_click(flow, f"undo:{inserted.audit_id}", 2)
    assert "не показано" in rejected.effective_message.reply_text.await_args.args[0]
    draft_click(flow, "history_page:10", 2)
    rejected = click(flow, f"d:{first.id}:{first.version}:undo:{changes[-1].audit_id}", 2)
    assert "устарела" in rejected.effective_message.reply_text.await_args.args[0]
    assert service.draft(2).stage == "history"
    assert len(service.stores.list_mcc(merchant_id)) == 14


@pytest.mark.parametrize("offset", ["1", "-10", "1000010"])
def test_history_page_offsets_are_bounded_and_aligned(flow, offset):
    event = click(flow, "recent:" + offset, 2)
    text = event.effective_message.reply_text.await_args.args[0]
    assert "устарела" in text or "недоступна" in text


def test_review_reason_and_preview_actions_extend_only_live_lease(flow, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("mcc_bot.community.time.time", lambda: clock[0])
    proposal = proposal_flow(flow)
    service = flow.application.bot_data["community"]
    click(flow, f"q:{proposal.id}:{proposal.version}:claim", 2)
    claimed = service.proposal(2, proposal.id)
    clock[0] = 200.0
    click(flow, f"q:{proposal.id}:{claimed.version}:reject", 2)
    assert service.proposal(2, proposal.id).lease_until == 1100
    clock[0] = 300.0
    send(flow, "Incorrect screenshot", 2)
    assert service.proposal(2, proposal.id).lease_until == 1200
    clock[0] = 400.0
    draft_click(flow, "resume", 2)
    assert service.proposal(2, proposal.id).lease_until == 1300
    assert service.proposal(2, proposal.id).version == claimed.version
    clock[0] = 1300.0
    event = draft_click(flow, "resume", 2)
    assert "истёк" in event.effective_message.reply_text.await_args.args[0]
    assert service.proposal(2, proposal.id).lease_until == 1300


def test_replace_and_owned_screenshot_extend_but_other_reviewer_does_not(flow, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("mcc_bot.community.time.time", lambda: clock[0])
    service = flow.application.bot_data["community"]
    service.set_role(1, 3, True)
    merchant_id = merchant(flow)
    click(flow, f"start:{merchant_id}")
    draft_click(flow, "channel:offline")
    send(flow, "5812")
    screenshot(flow)
    draft_click(flow, "submit")
    proposal = service.own_proposals(10)[0]
    view = click(flow, f"q:{proposal.id}:{proposal.version}:claim", 2)
    claimed = service.proposal(2, proposal.id)
    assert any(
        button.callback_data == f"community:media:{proposal.id}:{claimed.version}"
        for button in all_buttons(view)
    )
    clock[0] = 200.0
    click(flow, f"q:{proposal.id}:{claimed.version}:replace", 2)
    assert service.proposal(2, proposal.id).lease_until == 1100
    clock[0] = 300.0
    click(flow, f"media:{proposal.id}:{claimed.version}", 2)
    assert service.proposal(2, proposal.id).lease_until == 1200
    clock[0] = 400.0
    view = click(flow, f"media:{proposal.id}", 3)
    view.effective_message.reply_photo.assert_awaited_once()
    assert service.proposal(2, proposal.id).lease_until == 1200
    denied = click(flow, f"media:{proposal.id}:{claimed.version}", 3)
    denied.effective_message.reply_photo.assert_not_awaited()
    assert service.proposal(2, proposal.id).reviewer_id == 2
    clock[0] = 1200.0
    denied = click(flow, f"media:{proposal.id}:{claimed.version}", 2)
    denied.effective_message.reply_photo.assert_not_awaited()
    assert service.proposal(2, proposal.id).lease_until == 1200
    denied = click(flow, f"q:{proposal.id}:{claimed.version}:renew", 2)
    assert "истёк" in denied.effective_message.reply_text.await_args.args[0]
    assert service.proposal(2, proposal.id).lease_until == 1200
