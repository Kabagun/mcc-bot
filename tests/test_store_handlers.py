from __future__ import annotations

# Russian UI copy and ordinary Unicode buttons are intentional.
# ruff: noqa: RUF001
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest

from mcc_bot.catalog import CardCatalog
from mcc_bot.community import CommunityService
from mcc_bot.descriptions import DescriptionCatalog
from mcc_bot.store_handlers import handle_store_callback, search_stores
from mcc_bot.stores import StoreRepository


@pytest.fixture
def setup(tmp_path, catalog_path):
    repository = StoreRepository(tmp_path / "stores.sqlite3")
    repository.initialize()
    result = repository.apply_change("add_merchant", {"name": "Евроопт", "mcc": "5411"}, 1)
    message = SimpleNamespace(text="Евроопт", reply_text=AsyncMock(), is_accessible=True)
    callback = SimpleNamespace(
        data=None, answer=AsyncMock(), edit_message_text=AsyncMock(), message=message
    )
    update = SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=10),
        effective_chat=SimpleNamespace(type="private"),
        callback_query=callback,
    )
    context = SimpleNamespace(
        args=None,
        user_data={},
        application=SimpleNamespace(
            bot_data={
                "stores": repository,
                "catalog": CardCatalog.from_file(catalog_path),
                "descriptions": DescriptionCatalog({"5411": "Продуктовые магазины"}),
            }
        ),
    )
    return repository, result.merchant_id, update, context


def buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_single_result_always_asks_mcc_with_category(setup):
    _, merchant_id, update, context = setup
    asyncio.run(search_stores(update, context, "Euroopt"))
    update.effective_message.reply_text.assert_awaited_once()
    call = update.effective_message.reply_text.await_args
    assert "Евроопт" in call.args[0]
    choices = buttons(call.kwargs["reply_markup"])
    assert choices[0].text == "MCC 5411 — Продуктовые магазины"
    assert choices[0].callback_data == f"store:cards:{merchant_id}:offline:5411:0:0"
    assert choices[1].text == "➕ Предложить MCC"
    assert choices[1].callback_data == f"community:start:{merchant_id}"


def test_brand_card_transport_preserves_html_contract(setup):
    _, merchant_id, update, context = setup

    asyncio.run(search_stores(update, context, "Euroopt"))
    update.callback_query.data = f"store:show:{merchant_id}:0"
    asyncio.run(handle_store_callback(update, context))

    for call in (
        update.effective_message.reply_text.await_args,
        update.callback_query.edit_message_text.await_args,
    ):
        assert call.kwargs["parse_mode"] == ParseMode.HTML
        assert call.args[0].startswith("🏪 <b>Евроопт</b>")
        assert "<b>🏬 Офлайн / магазины</b>" in call.args[0]
        assert "<i>MCC может отличаться у разных касс и способов оплаты.</i>" in call.args[0]


def test_role_specific_action_is_one_and_rechecked(setup):
    _, merchant_id, update, context = setup
    service = Mock()
    service.is_admin.return_value = True
    context.application.bot_data["community"] = service
    update.callback_query.data = f"store:show:{merchant_id}:0"
    asyncio.run(handle_store_callback(update, context))
    choices = buttons(update.callback_query.edit_message_text.await_args.kwargs["reply_markup"])
    assert len(choices) == 3
    assert choices[-2].text == "➕ Добавить MCC"
    assert choices[-2].callback_data == f"community:start:{merchant_id}"
    assert choices[-1].text == "✏️ Редактировать магазин"
    assert choices[-1].callback_data == f"community:edit:{merchant_id}"
    service.is_admin.assert_called_once_with(10)


def test_same_message_cards_toggle_and_back(setup):
    _, merchant_id, update, context = setup
    for data in (
        f"store:cards:{merchant_id}:5411:0:0",
        f"store:cards:{merchant_id}:5411:0:1",
        f"store:show:{merchant_id}:0",
    ):
        update.callback_query.data = data
        asyncio.run(handle_store_callback(update, context))
    calls = update.callback_query.edit_message_text.await_args_list
    assert len(calls) == 3
    assert all("Евроопт" in call.args[0] for call in calls)
    assert "Beta Bank" not in calls[0].args[0]
    assert "Beta Bank" in calls[1].args[0]
    assert "Офлайн / магазины" in calls[2].args[0]
    assert any(button.text == "← К магазину" for button in buttons(calls[1].kwargs["reply_markup"]))
    update.effective_message.reply_text.assert_not_awaited()


def test_card_pages_preserve_global_rank_and_context(setup):
    _, merchant_id, update, context = setup
    match = context.application.bot_data["catalog"].lookup("5411")[0]
    many = tuple(
        replace(match, card=replace(match.card, id=f"card-{index}", name=f"Card {index}"))
        for index in range(150)
    )
    context.application.bot_data["catalog"] = SimpleNamespace(lookup=lambda mcc: many)
    update.callback_query.data = f"store:cards:{merchant_id}:5411:0:0"
    asyncio.run(handle_store_callback(update, context))
    first = update.callback_query.edit_message_text.await_args
    next_button = next(
        button for button in buttons(first.kwargs["reply_markup"]) if button.text == "→"
    )
    update.callback_query.data = next_button.callback_data
    asyncio.run(handle_store_callback(update, context))
    second = update.callback_query.edit_message_text.await_args
    assert "Евроопт" in second.args[0]
    assert len(second.args[0].encode("utf-16-le")) // 2 < 4096
    assert "1. " not in second.args[0].split("\n\n")[-1].splitlines()[0]
    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.parametrize(
    "data",
    [
        "store:",
        "store:show:no:0",
        "store:cards:1:5411:10001:0",
        "store:cards:1:9999:0:0",
        "store:cards:1:5411:-1:0",
        "store:show:1:0:0",
        "store:search:gone:0",
    ],
)
def test_invalid_callbacks_are_inert(setup, data):
    _, _, update, context = setup
    update.callback_query.data = data
    asyncio.run(handle_store_callback(update, context))
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_not_awaited()
    update.effective_message.reply_text.assert_not_awaited()


def test_not_modified_and_inaccessible_messages_are_safe(setup):
    _, merchant_id, update, context = setup
    update.callback_query.data = f"store:show:{merchant_id}:0"
    update.callback_query.edit_message_text.side_effect = BadRequest("Message is not modified")
    asyncio.run(handle_store_callback(update, context))
    update.callback_query.edit_message_text.reset_mock()
    update.callback_query.message.is_accessible = False
    asyncio.run(handle_store_callback(update, context))
    update.callback_query.edit_message_text.assert_not_awaited()


def test_fuzzy_search_labels_suggestions_and_escapes_names(setup):
    repository, _, update, context = setup
    repository.apply_change("add_merchant", {"name": "<Supermarket & One>"}, 1)
    asyncio.run(search_stores(update, context, "Supermarket &"))
    call = update.effective_message.reply_text.await_args
    assert call.kwargs["parse_mode"] == ParseMode.HTML
    assert "&lt;Supermarket &amp; One&gt;" in call.args[0]
    asyncio.run(search_stores(update, context, "evroppt"))
    assert "Точных совпадений" in update.effective_message.reply_text.await_args.args[0]


def test_many_search_results_inline_navigation_without_extra_messages(setup):
    repository, _, update, context = setup
    for index in range(12):
        repository.apply_change("add_merchant", {"name": f"Market {index}"}, 1)
    asyncio.run(search_stores(update, context, "Market"))
    next_button = next(
        button
        for button in buttons(update.effective_message.reply_text.await_args.kwargs["reply_markup"])
        if button.text == "→"
    )
    update.callback_query.data = next_button.callback_data
    asyncio.run(handle_store_callback(update, context))
    update.effective_message.reply_text.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once()


def test_search_and_stale_result_use_store_wording(setup):
    _, _, update, context = setup

    asyncio.run(search_stores(update, context, "missing"))
    call = update.effective_message.reply_text.await_args
    assert call.args[0] == ("Магазин <b>missing</b> не найден. Можно добавить его вместе с MCC.")
    assert buttons(call.kwargs["reply_markup"])[-1].text == "➕ Добавить новый магазин"

    update.callback_query.data = "store:show:999999:0"
    asyncio.run(handle_store_callback(update, context))
    assert update.callback_query.edit_message_text.await_args.args[0] == (
        "Магазин изменён или архивирован. Повторите поиск по названию."
    )


def test_public_brand_groups_channels_and_note_overrides_description(setup):
    repository, merchant_id, update, context = setup
    brand = repository.brand_for_merchant(merchant_id)
    repository.apply_change(
        "add_merchant",
        {"brand_id": brand.id, "name": brand.name, "channel": "online", "mcc": "5812"},
        1,
    )
    repository.apply_change(
        "edit_mcc_note",
        {"merchant_id": merchant_id, "mcc": "5411", "note": "Оплата у кассы"},
        1,
    )

    asyncio.run(search_stores(update, context, "Евроопт"))

    call = update.effective_message.reply_text.await_args
    assert "Офлайн / магазины" in call.args[0]
    assert "Онлайн / приложение" in call.args[0]
    labels = [button.text for button in buttons(call.kwargs["reply_markup"])]
    assert "MCC 5411 · Оплата у кассы" in labels
    assert not any("MCC 5411 — Продуктовые магазины ·" in label for label in labels)
    assert labels.count("➕ Предложить MCC") == 1


def test_brand_card_hides_a_channel_after_its_last_fact_is_removed(setup):
    repository, merchant_id, update, context = setup
    repository.apply_change("archive_mcc", {"merchant_id": merchant_id, "mcc": "5411"}, 1)

    asyncio.run(search_stores(update, context, "Евроопт"))

    text = update.effective_message.reply_text.await_args.args[0]
    assert "Офлайн / магазины" not in text
    assert "Онлайн / приложение" not in text
    assert "Наблюдений по офлайн- и онлайн-оплате пока нет" in text


def test_tannei_backing_does_not_lock_the_brand_card_for_helpers(setup):
    repository, _, update, context = setup
    service = CommunityService(repository, owner_id=1)
    service.initialize()
    service.set_role(1, 10, True)
    context.application.bot_data["community"] = service
    imported = repository.import_store(
        {
            "id": 111,
            "network_id": 222,
            "network_name": "Imported",
            "name": "Imported",
            "is_online": False,
            "address": None,
        },
        [{"mcc": "5411", "payment_date": "2026-08", "merchant_type": "Groceries"}],
    )
    brand = repository.brand_for_merchant(imported.merchant_id)

    asyncio.run(search_stores(update, context, brand.name))

    call = update.effective_message.reply_text.await_args
    assert "🔒" not in call.args[0]
    labels = [button.text for button in buttons(call.kwargs["reply_markup"])]
    assert "MCC 5411 — Продуктовые магазины" in labels
