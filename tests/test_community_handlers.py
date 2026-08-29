"""Telegram-visible workflow tests with real state and mocked network calls."""

# Russian UI copy and ordinary Unicode buttons are intentional.
# ruff: noqa: RUF001

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden

from mcc_bot.community import CommunityService
from mcc_bot.community_handlers import (
    ADD,
    APPLICATION_PENDING,
    GUIDE,
    INFO,
    LEGACY_MINE,
    MANAGE,
    QUEUE,
    SUGGEST,
    VOLUNTEER,
    callback,
    handle_media,
    handle_text,
    keyboard_for,
    show_menu,
)
from mcc_bot.stores import StoreRepository
from mcc_bot.users import UserRegistry


@pytest.fixture
def flow(tmp_path):
    service = CommunityService(StoreRepository(tmp_path / "stores.sqlite3"), owner_id=1)
    service.initialize()
    service.set_role(1, 2, True)
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(10)
    bot = SimpleNamespace(send_message=AsyncMock())
    return SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"community": service, "user_registry": registry, "descriptions": {}}
        ),
        bot=bot,
    )


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
    username="tester",
    first_name="Test",
    last_name="User",
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
        effective_user=SimpleNamespace(
            id=user, username=username, first_name=first_name, last_name=last_name
        ),
        effective_chat=SimpleNamespace(id=user, type=chat_type),
        effective_message=message,
        callback_query=query,
        update_id=update_id,
    )


def send(flow, text, user=10, **kwargs):
    event = update(user, text=text, **kwargs)
    assert asyncio.run(handle_text(event, flow))
    return event


def click(flow, action, user=10, **kwargs):
    event = update(user, data="community:" + action, **kwargs)
    asyncio.run(callback(event, flow))
    return event


def draft_click(flow, action, user=10):
    draft = flow.application.bot_data["community"].draft(user)
    return click(flow, f"d:{draft.id}:{draft.version}:{action}", user)


def keep_without_note(flow, user=10):
    return draft_click(flow, "note_keep", user)


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
    draft_click(flow, "channel:offline", user)
    send(flow, "5411", user)
    keep_without_note(flow, user)
    draft_click(flow, "preview:evidence", user)
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
        GUIDE,
        SUGGEST,
        VOLUNTEER,
    ]
    assert [button.text for row in keyboard_for(service, 2).keyboard for button in row] == [
        INFO,
        GUIDE,
        ADD,
        QUEUE,
        MANAGE,
    ]
    send(flow, SUGGEST)
    current = service.draft(10)
    event = update()
    asyncio.run(show_menu(event, flow))
    assert any(
        "четырёхзначный MCC или название магазина" in call.args[0]
        for call in event.effective_message.reply_text.await_args_list
    )
    assert any(
        "👥 Пользователей: 1 · 🤝 Помощников: 1" in call.args[0]
        for call in event.effective_message.reply_text.await_args_list
    )
    assert service.draft(10) == current
    assert any(button.text == "Продолжить" for button in all_buttons(event))


def test_role_aware_guide_explains_the_short_user_and_helper_paths(flow):
    user = send(flow, GUIDE, 10)
    user_text = user.effective_message.reply_text.await_args.args[0]
    assert "Напишите название магазина" in user_text
    assert "четыре цифры из операции" in user_text
    assert "Заполните короткую форму" in user_text

    helper = send(flow, GUIDE, 2)
    helper_text = helper.effective_message.reply_text.await_args.args[0]
    assert "закрепится за вами на 15 минут" in helper_text
    assert "Принять" in helper_text
    assert "Отклонить" in helper_text
    assert "Уточнить" in helper_text


def test_management_has_no_duplicate_store_editor_and_old_button_explains_the_path(flow):
    management = send(flow, MANAGE, 2)
    labels = [button.text for button in all_buttons(management)]
    assert "Редактировать магазин" not in labels
    assert "Журнал изменений и восстановление" in labels

    stale = click(flow, "edit", 2)
    assert flow.application.bot_data["community"].draft(2) is None
    assert "Найдите бренд по названию" in stale.effective_message.reply_text.await_args.args[0]


def test_user_flow_allows_no_screenshot_preview_and_submit(flow):
    send(flow, SUGGEST)
    send(flow, "New shop")
    draft_click(flow, "channel:offline")
    send(flow, "5411")
    service = flow.application.bot_data["community"]
    choice = service.draft(10)
    assert choice.stage == "note_choice"
    note_step = draft_click(flow, "note_edit")
    assert (
        "Текущая подпись: отсутствует" in note_step.effective_message.reply_text.await_args.args[0]
    )
    send(flow, "У кассы")
    assert service.draft(10).data["payload"]["note"] == "У кассы"
    draft_click(flow, "preview:evidence")
    preview = draft_click(flow, "skip")
    assert "Без скриншота" in preview.effective_message.reply_text.await_args.args[0]
    assert any(button.text == "Отправить на проверку" for button in all_buttons(preview))
    draft = service.draft(10)
    action = f"d:{draft.id}:{draft.version}:submit"
    submitted = update(data="community:" + action)
    submitted.callback_query.edit_message_text = AsyncMock()
    asyncio.run(callback(submitted, flow))
    confirmation = submitted.effective_message.reply_text.await_args.args[0]
    assert "Спасибо! Отправлено на проверку" in confirmation
    assert "Мои предложения" not in confirmation
    click(flow, action)
    assert len(service.own_proposals(10)) == 1
    assert not service.stores.search("New shop").matches


def test_text_at_evidence_step_repeats_inline_screenshot_choice(flow):
    send(flow, SUGGEST)
    send(flow, "New shop")
    draft_click(flow, "channel:offline")
    send(flow, "5411")
    keep_without_note(flow)
    draft_click(flow, "preview:evidence")
    service = flow.application.bot_data["community"]
    before = service.draft(10)

    repeated = send(flow, "Без фото")

    text = repeated.effective_message.reply_text.await_args.args[0]
    assert "Текст на этом шаге не используется" in text
    assert "Пришлите скриншот как фотографию" in text
    assert any(button.text == "Без скриншота" for button in all_buttons(repeated))
    assert service.draft(10) == before


def test_four_digits_are_validated_on_button_steps_but_allowed_in_free_text(flow):
    send(flow, SUGGEST)
    send(flow, "Yearly shop")
    rejected = send(flow, "1233")
    assert rejected.effective_message.reply_text.await_args.args[0] == (
        "MCC 1233 не найден в справочнике. Проверьте код."
    )
    assert flow.application.bot_data["community"].draft(10).stage == "channel"

    draft_click(flow, "channel:offline")
    send(flow, "5411")
    draft_click(flow, "note_edit")
    preview = send(flow, "2025")
    assert "Подпись к MCC: 2025" in preview.effective_message.reply_text.await_args.args[0]


def test_admin_direct_save_optional_screenshot(flow):
    send(flow, ADD, 2)
    send(flow, "Trusted shop", 2)
    draft_click(flow, "channel:online", 2)
    send(flow, "5812", 2)
    keep_without_note(flow, 2)
    event = draft_click(flow, "preview:more", 2)
    assert all("comment" not in button.callback_data for button in all_buttons(event))
    event = draft_click(flow, "back", 2)
    assert any(button.text == "✅ Сохранить в базу" for button in all_buttons(event))
    saved = draft_click(flow, "submit", 2)
    assert saved.effective_message.reply_text.await_args.args[0] == "Спасибо за добавление!"
    assert [button.text for button in all_buttons(saved)] == ["🏪 Открыть бренд"]
    service = flow.application.bot_data["community"]
    assert service.own_proposals(2)[0].status == "approved"
    assert service.stores.find_exact("Trusted shop", "online")


def test_new_brand_optional_fields_include_aliases_but_no_helper_comment(flow):
    send(flow, ADD, 2)
    send(flow, "Canonical shop", 2)
    draft_click(flow, "channel:offline", 2)
    send(flow, "5411", 2)
    keep_without_note(flow, 2)

    more = draft_click(flow, "preview:more", 2)
    labels = [button.text for button in all_buttons(more)]
    assert "Другие названия" in labels
    assert all("Комментарий проверяющему" not in label for label in labels)
    draft_click(flow, "more:aliases", 2)
    preview = send(flow, "Alias one, Другое имя", 2)
    assert (
        "Другие названия: Alias one, Другое имя"
        in (preview.effective_message.reply_text.await_args.args[0])
    )

    draft_click(flow, "submit", 2)
    brand = flow.application.bot_data["community"].stores.search("Canonical shop").matches[0]
    assert brand.aliases == ("Alias one", "Другое имя")


def test_context_preselection_and_global_button_starts_from_scratch(flow):
    merchant_id = merchant(flow)
    service = flow.application.bot_data["community"]
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    click(flow, f"start:{brand_id}:offline")
    assert service.draft(10).data["merchant_id"] == merchant_id
    assert service.draft(10).stage == "mcc"
    send(flow, SUGGEST)
    assert service.draft(10).stage == "name"
    assert "merchant_id" not in service.draft(10).data


def test_open_brand_keeps_html_parse_mode_when_inline_edit_falls_back(flow):
    merchant_id = merchant(flow, name="A < B")
    service = flow.application.bot_data["community"]
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    flow.application.bot_data["descriptions"] = {}
    event = update(2, data=f"community:open_brand:{brand_id}")
    event.callback_query.edit_message_text = AsyncMock(
        side_effect=BadRequest("message cannot be edited")
    )

    asyncio.run(callback(event, flow))

    edited = event.callback_query.edit_message_text.await_args
    assert edited.kwargs["parse_mode"] == ParseMode.HTML
    assert "A &lt; B" in edited.args[0]
    fallback = event.effective_message.reply_text.await_args
    assert fallback.kwargs["parse_mode"] == ParseMode.HTML
    assert "A &lt; B" in fallback.args[0]


def test_private_only_and_effective_user_not_chat_identity(flow):
    event = update(user=10, text=ADD, chat_type="group")
    asyncio.run(handle_text(event, flow))
    assert flow.application.bot_data["community"].draft(10) is None
    event = update(user=10, data="community:edit")
    event.effective_chat.id = 1
    asyncio.run(callback(event, flow))
    assert flow.application.bot_data["community"].draft(10) is None
    assert "Найдите бренд" in event.effective_message.reply_text.await_args.args[0]
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
    service = flow.application.bot_data["community"]
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "brand_actions", 2)
    draft_click(flow, "operation:names", 2)
    draft_click(flow, "name_primary", 2)
    send(flow, "Changed name", 2)
    draft_click(flow, "skip", 2)
    draft = service.draft(2)
    service.set_role(1, 2, False)
    click(flow, f"d:{draft.id}:{draft.version}:submit", 2)
    assert service.stores.get(merchant_id).name == "Shop"


def test_approval_does_not_notify_author(flow):
    proposal = proposal_flow(flow)
    service = flow.application.bot_data["community"]
    click(flow, f"q:{proposal.id}:{proposal.version}:claim", 2)
    claimed = service.proposal(2, proposal.id)
    flow.bot.send_message.reset_mock()
    event = click(flow, f"q:{proposal.id}:{claimed.version}:approve", 2)
    assert service.proposal(2, proposal.id).status == "approved"
    assert "сохранено" in event.effective_message.reply_text.await_args.args[0]
    flow.bot.send_message.assert_not_awaited()


def test_clarification_answer_and_legacy_cancel_is_inert(flow):
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
    response_draft = service.draft(10)
    click(flow, f"respond:{proposal.id}:{asked.version}")
    assert service.draft(10) == response_draft
    draft_click(flow, "skip")
    draft_click(flow, "submit")
    pending = service.proposal(10, proposal.id)
    assert pending.status == "pending"
    stale = click(flow, f"cancel:{proposal.id}:{pending.version}")
    assert service.proposal(10, proposal.id).status == "pending"
    assert "/start" in stale.effective_message.reply_text.await_args.args[0]


def test_cancel_clarification_answer_returns_the_proposal_to_queue(flow):
    proposal = proposal_flow(flow)
    service = flow.application.bot_data["community"]
    click(flow, f"q:{proposal.id}:{proposal.version}:claim", 2)
    claimed = service.proposal(2, proposal.id)
    click(flow, f"q:{proposal.id}:{claimed.version}:clarify", 2)
    send(flow, "Какой адрес?", 2)
    draft_click(flow, "decision", 2)
    asked = service.proposal(10, proposal.id)

    click(flow, f"respond:{proposal.id}:{asked.version}")
    cancelled = draft_click(flow, "cancel")

    returned = service.proposal(10, proposal.id)
    assert returned.status == "pending"
    assert service.draft(10) is None
    assert (
        f"Заявка №{proposal.id} возвращена на проверку"
        in (cancelled.effective_message.reply_text.await_args.args[0])
    )
    queue = send(flow, QUEUE, 2)
    buttons = all_buttons(queue)
    assert [button.text for button in buttons] == [f"№{proposal.id} · New shop — добавление MCC"]


def test_helper_application_main_button_is_idempotent(flow):
    service = flow.application.bot_data["community"]

    requested = send(
        flow,
        VOLUNTEER,
        username="alice_helper",
        first_name="Alice",
        last_name="Smith",
    )
    markup = requested.effective_message.reply_text.await_args.kwargs["reply_markup"]
    assert [button.text for row in markup.keyboard for button in row][-1] == APPLICATION_PENDING
    with service.stores.connection() as connection:
        created_at = connection.execute(
            "SELECT created_at FROM community_role_requests WHERE user_id=10"
        ).fetchone()[0]

    repeated = send(flow, APPLICATION_PENDING)

    assert "уже отправлена" in repeated.effective_message.reply_text.await_args.args[0]
    with service.stores.connection() as connection:
        assert (
            connection.execute(
                "SELECT created_at FROM community_role_requests WHERE user_id=10"
            ).fetchone()[0]
            == created_at
        )


def test_legacy_personal_proposals_button_is_inert(flow):
    event = send(flow, LEGACY_MINE)

    assert "/start" in event.effective_message.reply_text.await_args.args[0]


def test_role_request_grant_revoke_decline_and_consent_epoch(flow):
    service = flow.application.bot_data["community"]
    click(flow, "volunteer", username="alice_helper", first_name="Alice", last_name="Smith")
    roles = click(flow, "roles", 1)
    assert any(
        "Заявка · @alice_helper · Alice Smith" in button.text for button in all_buttons(roles)
    )
    candidate = click(flow, "role_view:10:0", 1)
    candidate_text = candidate.effective_message.reply_text.await_args.args[0]
    assert "@alice_helper" in candidate_text
    assert "Alice Smith" in candidate_text
    assert "Telegram ID: 10" in candidate_text
    assert any(button.text == "Назначить помощником" for button in all_buttons(candidate))
    granted = click(flow, "role:10:0:1", 1)
    assert service.is_admin(10)
    assert flow.bot.send_message.await_args.kwargs["chat_id"] == 10
    assert "✅ Вы назначены помощником" in flow.bot.send_message.await_args.kwargs["text"]
    assert "/start" in flow.bot.send_message.await_args.kwargs["text"]
    assert "Пользователь уведомлён" in granted.effective_message.reply_text.await_args.args[0]
    epoch = service.role_epoch(10)
    click(flow, f"digest:1:{epoch}")
    assert service.digest_enabled(10)
    click(flow, f"role:10:{epoch}:0", 1)
    service.set_role(1, 10, True)
    click(flow, f"digest:1:{epoch}")
    assert not service.digest_enabled(10)
    click(flow, "volunteer", 11, username=None, first_name="Bob", last_name="NoUsername")
    roles = click(flow, "roles", 1)
    assert any("без @username · Bob NoUsername" in button.text for button in all_buttons(roles))
    candidate = click(flow, "role_view:11:0", 1)
    assert "без @username" in candidate.effective_message.reply_text.await_args.args[0]
    click(flow, "decline:11:0", 1)
    assert not service.is_admin(11)
    click(flow, "role:11:0:1", 1)
    assert not service.is_admin(11)
    click(flow, "role:12:0:1", 1)
    assert not service.is_admin(12)
    management = click(flow, "manage", 1)
    assert all("user ID" not in button.text for button in all_buttons(management))


def test_role_grant_delivery_failure_does_not_undo_role(flow):
    service = flow.application.bot_data["community"]
    service.request_role(12, "helper_12", "Helper", "Twelve")
    flow.bot.send_message.side_effect = Forbidden("blocked")

    event = click(flow, "role:12:0:1", 1)

    assert service.is_admin(12)
    assert "не доставлено" in event.effective_message.reply_text.await_args.args[0]
    repeated = click(flow, "role:12:0:1", 1)
    assert service.is_admin(12)
    assert flow.bot.send_message.await_count == 1
    assert "изменилась" in repeated.effective_message.reply_text.await_args.args[0]


def test_media_bounds_document_rejection_privacy_and_unavailable_photo(flow):
    send(flow, SUGGEST)
    send(flow, "Shop")
    draft_click(flow, "channel:offline")
    send(flow, "5411")
    keep_without_note(flow)
    draft_click(flow, "preview:evidence")
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


def test_public_brand_report_uses_a_short_role_specific_mcc_action(flow):
    merchant_id = merchant(flow)
    service = flow.application.bot_data["community"]
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    event = click(flow, f"report:{brand_id}")
    operations = [button for button in all_buttons(event) if "operation:" in button.callback_data]
    assert [button.text for button in operations] == ["Предложить MCC"]
    draft_click(flow, "operation:add_mcc")
    assert service.draft(10).data["kind"] == "add_mcc"


def test_both_channels_are_one_change_and_preserve_existing_notes(flow):
    service = flow.application.bot_data["community"]
    merchant_id = merchant(flow, "Both shop")
    service.stores.apply_change(
        "edit_mcc_note",
        {"merchant_id": merchant_id, "mcc": "5411", "note": "Старая подпись"},
        1,
    )
    brand_id = service.stores.brand_for_merchant(merchant_id).id

    started = click(flow, f"start:{brand_id}", 1)
    assert any(button.text == "🏬🌐 Офлайн и онлайн" for button in all_buttons(started))
    draft_click(flow, "channel:both", 1)
    send(flow, "5411", 1)
    draft_click(flow, "note_edit", 1)
    preview = send(flow, "Новая подпись", 1)
    assert (
        "Существующие подписи останутся без изменений"
        in (preview.effective_message.reply_text.await_args.args[0])
    )
    assert service.draft(1).data["kind"] == "add_mcc_both"
    assert service.draft(1).data["payload"] == {
        "brand_id": brand_id,
        "mcc": "5411",
        "note": "Новая подпись",
    }
    draft_click(flow, "submit", 1)

    offline = service.stores.list_brand_mcc(brand_id, channel="offline")[0]
    online = service.stores.list_brand_mcc(brand_id, channel="online")[0]
    assert offline.note == "Старая подпись"
    assert online.note == "Новая подпись"
    assert service.own_proposals(1)[0].kind == "add_mcc_both"
    assert service.stores.brand_history(brand_id)[0].kind == "add_mcc_both"


def test_editor_shows_current_values_and_selects_one_concrete_fact(flow):
    merchant_id = merchant(flow)
    service = flow.application.bot_data["community"]
    service.stores.apply_change(
        "edit_mcc_note", {"merchant_id": merchant_id, "mcc": "5411", "note": "У кассы"}, 1
    )
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    editor = click(flow, f"edit:{brand_id}", 2)
    labels = [button.text for button in all_buttons(editor)]
    assert labels == ["➕ Добавить MCC", "✏️ Изменить MCC", "⋯ Ещё", "⬅️ К бренду"]
    editor_text = editor.effective_message.reply_text.await_args.args[0]
    assert "Редактирование: Shop" in editor_text
    assert "🏬 Офлайн: 5411" in editor_text

    unchanged = service.draft(2)
    rejected = draft_click(flow, "operation:archive_mcc", 2)
    assert "Изменение недоступно" in rejected.effective_message.reply_text.await_args.args[0]
    assert service.draft(2) == unchanged

    facts = draft_click(flow, "mcc_actions", 2)
    assert service.draft(2).stage == "mcc_facts"
    assert (
        "1. 🏬 Офлайн · MCC 5411 — У кассы" in facts.effective_message.reply_text.await_args.args[0]
    )
    assert any(button.text == "1 · 🏬 Офлайн · 5411" for button in all_buttons(facts))

    selected = draft_click(flow, "fact:offline:5411", 2)
    assert service.draft(2).stage == "mcc_fact"
    text = selected.effective_message.reply_text.await_args.args[0]
    assert "Способ оплаты: 🏬 Офлайн" in text
    assert "MCC: 5411" in text
    assert "Подпись: У кассы" in text
    assert [button.text for button in all_buttons(selected)][:3] == [
        "✏️ Заменить MCC",
        "✏️ Изменить подпись",
        "🗑 Убрать MCC",
    ]


def test_mcc_navigation_clean_cancel_and_dirty_cancel_are_predictable(flow):
    merchant_id = merchant(flow)
    service = flow.application.bot_data["community"]
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "mcc_actions", 2)
    draft_click(flow, "back", 2)
    assert service.draft(2).stage == "editor"
    assert service.draft(2).data["brand_id"] == brand_id

    draft_click(flow, "mcc_actions", 2)
    cancelled = draft_click(flow, "cancel", 2)
    assert service.draft(2) is None
    assert "Shop" in cancelled.effective_message.reply_text.await_args.args[0]

    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "mcc_actions", 2)
    draft_click(flow, f"fact:{merchant_id}:5411", 2)
    draft_click(flow, "fact_replace", 2)
    send(flow, "5812", 2)
    assert service.draft(2).stage == "note_choice"
    confirm = draft_click(flow, "cancel", 2)
    assert service.draft(2).stage == "cancel_confirm"
    assert [button.text for button in all_buttons(confirm)] == ["Да, отменить", "Нет, продолжить"]
    draft_click(flow, "cancel_no", 2)
    assert service.draft(2).stage == "note_choice"
    assert service.draft(2).data["mcc"] == "5812"


def test_clean_editor_text_closes_navigation_and_falls_through_to_search(flow):
    merchant_id = merchant(flow)
    service = flow.application.bot_data["community"]
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    click(flow, f"edit:{brand_id}", 2)

    event = update(2, text="Другой бренд")
    assert not asyncio.run(handle_text(event, flow))
    assert service.draft(2) is None
    event.effective_message.reply_text.assert_not_awaited()


def test_dirty_navigation_and_input_stages_still_keep_the_draft(flow):
    merchant_id = merchant(flow)
    service = flow.application.bot_data["community"]
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "mcc_actions", 2)
    draft_click(flow, "fact:offline:5411", 2)
    draft_click(flow, "fact_replace", 2)

    rejected = send(flow, "не MCC", 2)
    assert "ровно четыре цифры" in rejected.effective_message.reply_text.await_args.args[0]
    assert service.draft(2).stage == "mcc"
    send(flow, "5812", 2)
    draft_click(flow, "back", 2)
    draft_click(flow, "back", 2)
    assert service.draft(2).stage == "mcc_fact"
    before = service.draft(2)
    retained = send(flow, "Другой бренд", 2)
    assert (
        "Текст на этом шаге не используется"
        in (retained.effective_message.reply_text.await_args.args[0])
    )
    assert service.draft(2) == before


def test_prefilled_brand_and_channel_cancel_without_a_fake_dirty_draft(flow):
    merchant_id = merchant(flow)
    service = flow.application.bot_data["community"]
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    started = click(flow, f"start:{brand_id}:offline")
    assert service.draft(10).stage == "mcc"
    assert any(button.text == "⬅️ К бренду" for button in all_buttons(started))

    cancelled = draft_click(flow, "cancel")
    assert service.draft(10) is None
    assert "Shop" in cancelled.effective_message.reply_text.await_args.args[0]


def test_replace_mcc_keeps_then_edits_and_removes_the_current_signature(flow):
    merchant_id = merchant(flow)
    service = flow.application.bot_data["community"]
    service.stores.apply_change(
        "edit_mcc_note", {"merchant_id": merchant_id, "mcc": "5411", "note": "Старая"}, 1
    )
    brand_id = service.stores.brand_for_merchant(merchant_id).id

    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "mcc_actions", 2)
    draft_click(flow, f"fact:{merchant_id}:5411", 2)
    draft_click(flow, "fact_replace", 2)
    choice = send(flow, "5262", 2)
    choice_text = choice.effective_message.reply_text.await_args.args[0]
    assert "MCC: 5411 → 5262" in choice_text
    assert "Текущая подпись: Старая" in choice_text
    assert [button.text for button in all_buttons(choice)][:3] == [
        "Оставить подпись",
        "✏️ Изменить подпись",
        "🗑 Убрать подпись",
    ]
    preview = draft_click(flow, "note_keep", 2)
    preview_text = preview.effective_message.reply_text.await_args.args[0]
    assert "MCC: 5411 → 5262" in preview_text
    assert "Подпись: Старая (без изменений)" in preview_text
    draft_click(flow, "submit", 2)
    fact = service.stores.list_mcc(merchant_id)[0]
    assert (fact.mcc, fact.note) == ("5262", "Старая")

    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "mcc_actions", 2)
    draft_click(flow, f"fact:{merchant_id}:5262", 2)
    note = draft_click(flow, "fact_note", 2)
    assert "Текущая подпись: Старая" in note.effective_message.reply_text.await_args.args[0]
    preview = send(flow, "Новая", 2)
    assert "Подпись: «Старая» → «Новая»" in preview.effective_message.reply_text.await_args.args[0]
    draft_click(flow, "submit", 2)
    assert service.stores.list_mcc(merchant_id)[0].note == "Новая"

    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "mcc_actions", 2)
    draft_click(flow, f"fact:{merchant_id}:5262", 2)
    draft_click(flow, "fact_note", 2)
    preview = draft_click(flow, "skip", 2)
    assert "Подпись: «Новая» → удалена" in preview.effective_message.reply_text.await_args.args[0]
    draft_click(flow, "submit", 2)
    assert service.stores.list_mcc(merchant_id)[0].note == ""


def test_same_mcc_in_two_channels_is_shown_as_two_unambiguous_editable_facts(flow):
    offline = merchant(flow, "eMall")
    service = flow.application.bot_data["community"]
    brand = service.stores.brand_for_merchant(offline)
    service.stores.apply_change(
        "add_merchant",
        {"brand_id": brand.id, "name": brand.name, "channel": "online", "mcc": "5411"},
        1,
    )

    click(flow, f"edit:{brand.id}", 2)
    facts = draft_click(flow, "mcc_actions", 2)
    text = facts.effective_message.reply_text.await_args.args[0]
    assert "1. 🏬 Офлайн · MCC 5411" in text
    assert "2. 🌐 Онлайн · MCC 5411" in text
    callbacks = [
        button.callback_data for button in all_buttons(facts) if ":fact:" in button.callback_data
    ]
    assert any(":fact:offline:5411" in value for value in callbacks)
    assert any(":fact:online:5411" in value for value in callbacks)


def test_aggregated_fact_replace_and_remove_never_leave_a_hidden_duplicate(flow):
    first = merchant(flow, "Grouped")
    service = flow.application.bot_data["community"]
    brand = service.stores.brand_for_merchant(first)
    second = service.stores.apply_change(
        "add_merchant",
        {
            "brand_id": brand.id,
            "name": "Grouped source",
            "channel": "offline",
            "mcc": "5411",
        },
        1,
    ).merchant_id

    click(flow, f"edit:{brand.id}", 2)
    draft_click(flow, "mcc_actions", 2)
    draft_click(flow, "fact:offline:5411", 2)
    draft_click(flow, "fact_replace", 2)
    send(flow, "5812", 2)
    draft_click(flow, "note_keep", 2)
    draft_click(flow, "submit", 2)
    assert all(
        [fact.mcc for fact in service.stores.list_mcc(merchant_id)] == ["5812"]
        for merchant_id in (first, second)
    )

    click(flow, f"edit:{brand.id}", 2)
    draft_click(flow, "mcc_actions", 2)
    draft_click(flow, "fact:offline:5812", 2)
    draft_click(flow, "fact_remove", 2)
    draft_click(flow, "submit", 2)
    assert service.stores.list_brand_mcc(brand.id) == ()
    assert all(not service.stores.list_mcc(merchant_id) for merchant_id in (first, second))


def test_switching_fact_after_back_rebuilds_the_concurrency_snapshot(flow):
    offline = merchant(flow, "Channels")
    service = flow.application.bot_data["community"]
    brand = service.stores.brand_for_merchant(offline)
    online = service.stores.apply_change(
        "add_merchant",
        {
            "brand_id": brand.id,
            "name": "Channels online",
            "channel": "online",
            "mcc": "5812",
        },
        1,
    ).merchant_id

    click(flow, f"edit:{brand.id}", 2)
    draft_click(flow, "mcc_actions", 2)
    draft_click(flow, "fact:offline:5411", 2)
    draft_click(flow, "fact_remove", 2)
    assert f"{offline}:5411" in service.draft(2).data["expected"]["facts"]
    draft_click(flow, "back", 2)
    draft_click(flow, "back", 2)
    draft_click(flow, "fact:online:5812", 2)
    draft_click(flow, "fact_remove", 2)
    expected_facts = service.draft(2).data["expected"]["facts"]
    assert f"{online}:5812" in expected_facts
    assert f"{offline}:5411" not in expected_facts

    draft_click(flow, "submit", 2)
    assert [fact.mcc for fact in service.stores.list_mcc(offline)] == ["5411"]
    assert service.stores.list_mcc(online) == ()


def test_mcc_fact_list_pages_after_ten_rows(flow):
    merchant_id = merchant(flow, mcc="5000")
    service = flow.application.bot_data["community"]
    for value in range(5001, 5012):
        service.stores.apply_change("add_mcc", {"merchant_id": merchant_id, "mcc": str(value)}, 1)
    brand_id = service.stores.brand_for_merchant(merchant_id).id

    click(flow, f"edit:{brand_id}", 2)
    first = draft_click(flow, "mcc_actions", 2)
    assert any(button.callback_data.endswith(":fact_page:10") for button in all_buttons(first))
    second = draft_click(flow, "fact_page:10", 2)
    assert "11. 🏬 Офлайн · MCC 5010" in second.effective_message.reply_text.await_args.args[0]
    assert all(len(button.callback_data.encode()) <= 64 for button in all_buttons(second))


def test_dirty_mcc_preview_ignores_text_with_specific_guidance(flow):
    merchant_id = merchant(flow, mcc="5399")
    service = flow.application.bot_data["community"]
    service.stores.apply_change("add_mcc", {"merchant_id": merchant_id, "mcc": "5262"}, 1)
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "mcc_actions", 2)
    draft_click(flow, f"fact:{merchant_id}:5399", 2)
    draft_click(flow, "fact_remove", 2)
    before = service.draft(2)
    rejected = send(flow, "Да, убрать", 2)
    text = rejected.effective_message.reply_text.await_args.args[0]
    assert "Текст на этом шаге не используется" in text
    assert "«Сохранить в базу»".lower() in text.lower()
    assert "Черновик сохранён" not in text
    assert all_buttons(rejected)
    assert service.draft(2) == before


@pytest.mark.parametrize(("chosen", "remaining"), [("5399", "5262"), ("5262", "5399")])
def test_archive_mcc_preview_and_save_target_only_the_selected_fact(flow, chosen, remaining):
    merchant_id = merchant(flow, mcc="5399")
    service = flow.application.bot_data["community"]
    service.stores.apply_change("add_mcc", {"merchant_id": merchant_id, "mcc": "5262"}, 1)
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "mcc_actions", 2)

    before = service.draft(2)
    stale = draft_click(flow, f"fact:{merchant_id}:0000", 2)
    assert "MCC уже изменился" in stale.effective_message.reply_text.await_args.args[0]
    assert service.draft(2) == before

    draft_click(flow, f"fact:{merchant_id}:{chosen}", 2)
    preview = draft_click(flow, "fact_remove", 2)
    text = preview.effective_message.reply_text.await_args.args[0]
    assert text == (
        f"Убрать MCC {chosen} у бренда «Shop» (офлайн)?\nДругие MCC останутся без изменений."
    )
    assert remaining not in text
    assert "Проверьте перед отправкой" not in text
    assert "Скриншот" not in text
    assert "комментар" not in text.lower()
    assert [button.text for button in all_buttons(preview)] == [
        "✅ Сохранить в базу",
        "⬅️ Назад",
        "Отмена",
    ]
    for hidden_field in ("evidence", "comment"):
        before = service.draft(2)
        rejected = draft_click(flow, "preview:" + hidden_field, 2)
        assert "Поле недоступно" in rejected.effective_message.reply_text.await_args.args[0]
        assert service.draft(2) == before

    draft_click(flow, "submit", 2)
    assert {fact.mcc for fact in service.stores.list_mcc(merchant_id)} == {remaining}


def test_unified_names_screen_adds_edits_promotes_deletes_and_renames(flow):
    merchant_id = merchant(flow)
    service = flow.application.bot_data["community"]
    brand_id = service.stores.brand_for_merchant(merchant_id).id

    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "brand_actions", 2)
    names = draft_click(flow, "operation:names", 2)
    assert "Основное: Shop" in names.effective_message.reply_text.await_args.args[0]
    assert any(button.text == "➕ Добавить другое название" for button in all_buttons(names))
    assert all(len(button.callback_data.encode()) <= 64 for button in all_buttons(names))
    draft_click(flow, "name_add", 2)
    send(flow, "Alias one", 2)
    draft_click(flow, "submit", 2)
    assert service.stores.get_brand(brand_id).aliases == ("Alias one",)

    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "brand_actions", 2)
    draft_click(flow, "operation:names", 2)
    draft_click(flow, "name_alias:0", 2)
    draft_click(flow, "name_promote", 2)
    draft_click(flow, "submit", 2)
    promoted = service.stores.get_brand(brand_id)
    assert (promoted.name, promoted.aliases) == ("Alias one", ("Shop",))

    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "brand_actions", 2)
    draft_click(flow, "operation:names", 2)
    draft_click(flow, "name_alias:0", 2)
    draft_click(flow, "name_edit", 2)
    send(flow, "Shop edited", 2)
    draft_click(flow, "submit", 2)
    assert service.stores.get_brand(brand_id).aliases == ("Shop edited",)

    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "brand_actions", 2)
    draft_click(flow, "operation:names", 2)
    draft_click(flow, "name_alias:0", 2)
    draft_click(flow, "name_delete", 2)
    draft_click(flow, "submit", 2)
    assert service.stores.get_brand(brand_id).aliases == ()

    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "brand_actions", 2)
    draft_click(flow, "operation:names", 2)
    draft_click(flow, "name_primary", 2)
    preview = send(flow, "Final name", 2)
    assert "Скриншот" not in preview.effective_message.reply_text.await_args.args[0]
    assert service.stores.get_brand(brand_id).name == "Alias one"
    draft_click(flow, "submit", 2)
    assert service.stores.get_brand(brand_id).name == "Final name"


def test_editor_archive_history_and_safe_undo(flow):
    merchant_id = merchant(flow)
    service = flow.application.bot_data["community"]
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "mcc_actions", 2)
    draft_click(flow, f"fact:{merchant_id}:5411", 2)
    draft_click(flow, "fact_remove", 2)
    draft_click(flow, "submit", 2)
    assert not service.stores.list_mcc(merchant_id)
    click(flow, f"edit:{brand_id}", 2)
    draft_click(flow, "brand_actions", 2)
    history = draft_click(flow, "history", 2)
    assert any("удалён MCC 5411" in button.text for button in all_buttons(history))
    last = service.stores.history(merchant_id)[0]
    details = draft_click(flow, f"entry:{last.id}", 2)
    assert "Убран из поиска MCC: 5411" in details.effective_message.reply_text.await_args.args[0]
    draft_click(flow, f"undo:{last.id}", 2)
    draft_click(flow, "submit", 2)
    assert service.stores.list_mcc(merchant_id)[0].mcc == "5411"


def test_editor_merges_ordinary_brands_and_has_no_channel_archive_ui(flow):
    source = merchant(flow, "Source")
    target = merchant(flow, "Target", "5812")
    service = flow.application.bot_data["community"]
    source_brand = service.stores.brand_for_merchant(source).id
    target_brand = service.stores.brand_for_merchant(target).id
    click(flow, f"edit:{source_brand}", 2)
    more = draft_click(flow, "brand_actions", 2)
    labels = [button.text for button in all_buttons(more)]
    assert "Каналы / архив" not in labels
    assert "Названия" in labels
    draft_click(flow, "operation:merge_brand", 2)
    send(flow, "Target", 2)
    draft_click(flow, f"select:{target_brand}", 2)
    draft_click(flow, "skip", 2)
    draft_click(flow, "submit", 2)
    assert not service.stores.get_brand(source_brand)
    assert {item.mcc for item in service.stores.list_brand_mcc(target_brand)} == {
        "5411",
        "5812",
    }


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
    service = flow.application.bot_data["community"]
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    click(flow, f"start:{brand_id}")
    draft_click(flow, "channel:online")
    draft_click(flow, "back")
    draft_click(flow, "channel:offline")
    send(flow, "5812")
    keep_without_note(flow)
    draft_click(flow, "preview:evidence")
    screenshot(flow)
    draft = service.draft(10)
    assert draft.data["kind"] == "add_mcc"
    assert draft.data["payload"] == {"merchant_id": merchant_id, "mcc": "5812", "note": ""}


def test_back_out_of_legacy_report_never_falls_into_a_blank_brand_prompt(flow):
    merchant_id = merchant(flow)
    service = flow.application.bot_data["community"]
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    click(flow, f"report:{brand_id}")
    draft_click(flow, "operation:add_mcc")
    returned = draft_click(flow, "back")
    assert service.draft(10).stage == "report"
    assert "Что нужно дополнить" in returned.effective_message.reply_text.await_args.args[0]
    cancelled = draft_click(flow, "cancel")
    assert service.draft(10) is None
    assert "Shop" in cancelled.effective_message.reply_text.await_args.args[0]


def test_archived_store_is_recoverable_from_management_history(flow):
    merchant_id = merchant(flow)
    service = flow.application.bot_data["community"]
    service.stores.apply_change("archive_merchant", {"merchant_id": merchant_id}, 1)
    assert service.stores.get(merchant_id) is None
    event = click(flow, "recent", 2)
    audit_id = service.stores.history(merchant_id)[0].id
    assert any(
        button.callback_data == f"community:history_entry:{audit_id}:0"
        for button in all_buttons(event)
    )
    click(flow, f"history_entry:{audit_id}:0", 2)
    draft_click(flow, f"undo:{audit_id}", 2)
    draft_click(flow, "submit", 2)
    assert service.stores.get(merchant_id)


def test_history_shows_one_tannei_snapshot_not_legacy_import_rows(flow):
    service = flow.application.bot_data["community"]
    service.set_role(1, 2, False)
    service.request_role(2, "helper_two", "Bob", "Helper")
    service.set_role(1, 2, True, require_pending=True)
    imported = service.stores.import_store(
        {
            "id": 123,
            "network_id": 456,
            "network_name": "Import Shop",
            "name": "Import Shop",
            "is_online": False,
            "address": None,
        },
        [{"mcc": "5411", "payment_date": "2026-08", "merchant_type": "Groceries"}],
    )
    service.stores.apply_change("add_mcc", {"merchant_id": imported.merchant_id, "mcc": "5812"}, 2)

    event = click(flow, f"history:{imported.merchant_id}", 2)
    text = event.effective_message.reply_text.await_args.args[0]
    assert text.count("tannei.by · сводка импорта") == 1
    assert text.count("MCC 5411 · импорт: 1") == 1
    assert "Импорт данных из tannei.by" not in text
    assert "Добавлен магазин «Import Shop»" not in text
    assert "Подтверждения tannei.by для MCC 5411: +1" not in text
    assert "Изменил: tannei.by · автоматический импорт" not in text
    manual = service.stores.history(imported.merchant_id)[0]
    detail = draft_click(flow, f"entry:{manual.id}", 2)
    assert (
        "Изменил: @helper_two · Bob Helper · Telegram ID 2"
        in detail.effective_message.reply_text.await_args.args[0]
    )
    assert "MCC 5411 · импорт: 1 · 2026-08" in text
    assert "Groceries" not in text
    assert all(
        not button.callback_data.endswith(f":undo:{imported.audit_id}")
        for button in all_buttons(event)
    )


def test_helpers_can_open_all_actions_for_tannei_backed_data(flow):
    service = flow.application.bot_data["community"]
    imported = service.stores.import_store(
        {
            "id": 321,
            "network_id": 654,
            "network_name": "Locked Shop",
            "name": "Locked Shop",
            "is_online": False,
            "address": None,
        },
        [{"mcc": "5411", "payment_date": "2026-08", "merchant_type": "Groceries"}],
    )
    brand_id = service.stores.brand_for_merchant(imported.merchant_id).id

    editor = click(flow, f"edit:{brand_id}", 2)
    assert "🔒" not in editor.effective_message.reply_text.await_args.args[0]
    facts = draft_click(flow, "mcc_actions", 2)
    assert all(not button.text.startswith("🔒 ") for button in all_buttons(facts))
    selected = draft_click(flow, "fact:offline:5411", 2)
    selected_callbacks = [button.callback_data for button in all_buttons(selected)]
    assert any(value.endswith(":fact_replace") for value in selected_callbacks)
    assert any(value.endswith(":fact_note") for value in selected_callbacks)
    assert any(value.endswith(":fact_remove") for value in selected_callbacks)
    preview = draft_click(flow, "fact_remove", 2)
    assert "✅ Сохранить в базу" in [button.text for button in all_buttons(preview)]

    service.cancel_draft(2, service.draft(2).id, service.draft(2).version)
    click(flow, f"edit:{brand_id}", 2)
    more = draft_click(flow, "brand_actions", 2)
    labels = [button.text for button in all_buttons(more)]
    assert "Названия" in labels
    assert "Объединить бренд" in labels
    assert "История / отмена" in labels

    service.cancel_draft(2, service.draft(2).id, service.draft(2).version)
    click(flow, f"start:{brand_id}", 2)
    draft_click(flow, "channel:offline", 2)
    accepted = send(flow, "5411", 2)
    assert service.draft(2).stage == "note_choice"
    assert "может только владелец" not in accepted.effective_message.reply_text.await_args.args[0]


def test_helper_review_shows_publish_actions_for_tannei_backed_data(flow):
    service = flow.application.bot_data["community"]
    imported = service.stores.import_store(
        {
            "id": 777,
            "network_id": 778,
            "network_name": "Review lock",
            "name": "Review lock",
            "is_online": False,
            "address": None,
        },
        [{"mcc": "5411", "payment_date": "2026-08", "merchant_type": "Groceries"}],
    )
    brand_id = service.stores.brand_for_merchant(imported.merchant_id).id
    click(flow, f"start:{brand_id}:offline", 10)
    send(flow, "5411", 10)
    draft_click(flow, "note_keep", 10)
    draft_click(flow, "preview:evidence", 10)
    draft_click(flow, "skip", 10)
    draft_click(flow, "submit", 10)
    proposal = service.own_proposals(10)[0]

    review = click(flow, f"q:{proposal.id}:{proposal.version}:claim", 2)
    assert "только владелец" not in review.effective_message.reply_text.await_args.args[0]
    callbacks = [button.callback_data for button in all_buttons(review)]
    assert any(value.endswith(":approve") for value in callbacks)
    assert any(value.endswith(":replace") for value in callbacks)


def test_helper_review_replacement_menu_includes_tannei_backed_old_mcc(flow):
    service = flow.application.bot_data["community"]
    imported = service.stores.import_store(
        {
            "id": 779,
            "network_id": 780,
            "network_name": "Partly locked",
            "name": "Partly locked",
            "is_online": False,
            "address": None,
        },
        [{"mcc": "5411", "payment_date": "2026-08", "merchant_type": "Groceries"}],
    )
    service.stores.apply_change("add_mcc", {"merchant_id": imported.merchant_id, "mcc": "5812"}, 1)
    brand_id = service.stores.brand_for_merchant(imported.merchant_id).id
    click(flow, f"start:{brand_id}:offline", 10)
    send(flow, "5732", 10)
    draft_click(flow, "note_keep", 10)
    draft_click(flow, "preview:evidence", 10)
    draft_click(flow, "skip", 10)
    draft_click(flow, "submit", 10)
    proposal = service.own_proposals(10)[0]

    click(flow, f"q:{proposal.id}:{proposal.version}:claim", 2)
    claimed = service.proposal(2, proposal.id)
    menu = click(flow, f"q:{proposal.id}:{claimed.version}:replace", 2)
    callbacks = [button.callback_data for button in all_buttons(menu)]
    assert any(value.endswith(":5812") for value in callbacks)
    assert any(value.endswith(":5411") for value in callbacks)


def test_helper_history_offers_revert_for_manual_change_to_imported_public_mcc(flow):
    service = flow.application.bot_data["community"]
    imported = service.stores.import_store(
        {
            "id": 781,
            "network_id": 782,
            "network_name": "Revert imported",
            "name": "Revert imported",
            "is_online": False,
            "address": None,
        },
        [{"mcc": "5411", "payment_date": "2026-08", "merchant_type": "Groceries"}],
    )
    changed = service.stores.apply_change(
        "edit_mcc_note",
        {"merchant_id": imported.merchant_id, "mcc": "5411", "note": "Новая подпись"},
        1,
    )

    history = click(flow, f"history:{imported.merchant_id}", 2)

    assert any(
        button.callback_data.endswith(f":entry:{changed.audit_id}")
        for button in all_buttons(history)
    )
    detail = draft_click(flow, f"entry:{changed.audit_id}", 2)
    assert any(
        button.callback_data.endswith(f":undo:{changed.audit_id}") for button in all_buttons(detail)
    )


def test_recent_history_page_two_restores_an_older_archived_store(flow):
    service = flow.application.bot_data["community"]
    merchant_id = merchant(flow, "Archived older shop")
    archived = service.stores.apply_change("archive_merchant", {"merchant_id": merchant_id}, 1)
    for index in range(12):
        merchant(flow, f"Later shop {index}")
    first = click(flow, "recent", 2)
    assert all(
        button.callback_data != f"community:history_entry:{archived.audit_id}:0"
        for button in all_buttons(first)
    )
    assert any(button.callback_data == "community:recent:10" for button in all_buttons(first))
    second = click(flow, "recent:10", 2)
    assert any(
        button.callback_data == f"community:history_entry:{archived.audit_id}:10"
        for button in all_buttons(second)
    )
    assert any(button.callback_data == "community:recent:0" for button in all_buttons(second))
    assert all(len(button.callback_data.encode()) <= 64 for button in all_buttons(second))
    click(flow, f"history_entry:{archived.audit_id}:10", 2)
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
    brand_id = service.stores.brand_for_merchant(merchant_id).id
    click(flow, f"start:{brand_id}:offline")
    send(flow, "5812")
    keep_without_note(flow)
    draft_click(flow, "preview:evidence")
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
