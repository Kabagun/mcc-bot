"""Exercise the registered PTB dispatcher with real updates and a fake Telegram transport."""

from __future__ import annotations

import asyncio
import json

import pytest
from telegram import Update
from telegram.request import HTTPXRequest

from mcc_bot.bot import build_application
from mcc_bot.config import BotSettings


@pytest.fixture
def telegram_app(tmp_path, catalog_path, monkeypatch):
    calls = []

    async def request(_self, url, method, request_data=None, **_kwargs):
        action = url.rsplit("/", 1)[-1]
        data = request_data.parameters if request_data else {}
        calls.append((action, data))
        if action == "getMe":
            result = {"id": 999, "is_bot": True, "first_name": "Test", "username": "mcc_test_bot"}
        elif action in {"sendMessage", "editMessageText"}:
            result = {
                "message_id": int(data.get("message_id", len(calls))),
                "date": 1,
                "chat": {"id": data["chat_id"], "type": "private"},
                "text": data["text"],
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    monkeypatch.setattr(HTTPXRequest, "do_request", request)
    app = build_application(
        BotSettings(
            token="123456:TEST",
            catalog_path=catalog_path,
            user_registry_path=tmp_path / "users.sqlite3",
            stores_path=tmp_path / "stores.sqlite3",
            owner_telegram_id=42,
        )
    )
    return app, calls


def incoming(app, text, *, user_id=101, group=False, sequence=1):
    message = {
        "message_id": sequence,
        "date": 1,
        "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
        "chat": {"id": -1001 if group else user_id, "type": "group" if group else "private"},
        "text": text,
    }
    if text.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.de_json({"update_id": sequence, "message": message}, app.bot)


def tapped(app, data, *, user_id=101, sequence=100):
    return Update.de_json(
        {
            "update_id": sequence,
            "callback_query": {
                "id": str(sequence),
                "chat_instance": "test",
                "data": data,
                "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
                "message": {
                    "message_id": 80,
                    "date": 1,
                    "text": "Result",
                    "chat": {"id": user_id, "type": "private"},
                },
            },
        },
        app.bot,
    )


def rendered(calls):
    return [data for action, data in calls if action in {"sendMessage", "editMessageText"}]


def test_registered_dispatcher_routes_numbers_text_and_removed_commands(telegram_app):
    app, calls = telegram_app

    async def scenario():
        async with app:
            await app.process_update(incoming(app, "5411"))
            assert "<b>Beta Card</b>" in rendered(calls)[-1]["text"]
            for index, text in enumerate(("/mcc 5411", "/help", "MCC 5411", "123"), start=2):
                calls.clear()
                await app.process_update(incoming(app, text, sequence=index))
                replies = rendered(calls)
                assert replies, text
                assert all("Beta Card" not in item["text"] for item in replies), text
            calls.clear()
            await app.process_update(incoming(app, "/start", sequence=9))
            keyboard = rendered(calls)[-1]["reply_markup"]["keyboard"]
            labels = [button["text"] for row in keyboard for button in row]
            assert "👤 Мои предложения" in labels
            assert "📋 Разобрать очередь" not in labels

    asyncio.run(scenario())


def test_registered_store_buttons_require_mcc_selection_and_edit_in_place(telegram_app):
    app, calls = telegram_app
    app.bot_data["stores"].apply_change(
        "add_merchant",
        {"name": "Евроопт", "channel": "offline", "mcc": "5411"},
        42,
    )

    async def scenario():
        async with app:
            await app.process_update(incoming(app, "Euroopt"))
            reply = rendered(calls)[-1]
            assert "Евроопт" in reply["text"]
            assert "Beta Card" not in reply["text"]
            buttons = [button for row in reply["reply_markup"]["inline_keyboard"] for button in row]
            mcc = next(button for button in buttons if "5411" in button["text"])
            calls.clear()
            await app.process_update(tapped(app, mcc["callback_data"]))
            assert not any(action == "sendMessage" for action, _ in calls)
            reply = rendered(calls)[-1]
            assert "Евроопт" in reply["text"] and "<b>Beta Card</b>" in reply["text"]
            buttons = [button for row in reply["reply_markup"]["inline_keyboard"] for button in row]
            details = next(button for button in buttons if "Банки" in button["text"])
            calls.clear()
            await app.process_update(tapped(app, details["callback_data"], sequence=101))
            assert not any(action == "sendMessage" for action, _ in calls)
            assert "Евроопт" in rendered(calls)[-1]["text"]
            assert "Beta Bank" in rendered(calls)[-1]["text"]

    asyncio.run(scenario())


def test_owner_menu_and_sensitive_actions_reject_group_chat(telegram_app):
    app, calls = telegram_app

    async def scenario():
        async with app:
            await app.process_update(incoming(app, "/start", user_id=42))
            labels = [
                button["text"]
                for row in rendered(calls)[-1]["reply_markup"]["keyboard"]
                for button in row
            ]
            assert "📋 Разобрать очередь" in labels
            calls.clear()
            await app.process_update(
                incoming(app, "➕ Добавить MCC магазина", user_id=42, group=True, sequence=2)  # noqa: RUF001
            )
            assert app.bot_data["community"].draft(42) is None
            assert rendered(calls)
            assert "личн" in rendered(calls)[-1]["text"].lower()

    asyncio.run(scenario())
