"""Exercise Stars checkout and fulfillment via the real application dispatcher."""

# Russian UI copy is intentional.
# ruff: noqa: RUF001

import asyncio

from telegram import Update
from test_application_flow import incoming, rendered, tapped
from test_application_flow import telegram_app as telegram_app


def _checkout(app, payload, *, user_id=101, amount=100, sequence=2):
    return Update.de_json(
        {
            "update_id": sequence,
            "pre_checkout_query": {
                "id": f"checkout-{sequence}",
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "currency": "XTR",
                "total_amount": amount,
                "invoice_payload": payload,
            },
        },
        app.bot,
    )


def _receipt(app, payload, *, user_id=101, sequence=3):
    return Update.de_json(
        {
            "update_id": sequence,
            "message": {
                "message_id": sequence,
                "date": 1,
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "chat": {"id": user_id, "type": "private"},
                "successful_payment": {
                    "currency": "XTR",
                    "total_amount": 100,
                    "invoice_payload": payload,
                    "telegram_payment_charge_id": "test-charge",
                    "provider_payment_charge_id": "",
                },
            },
        },
        app.bot,
    )


def test_confirmed_stars_payment_unbans_and_replay_does_not_undo_later_ban(telegram_app):
    app, calls = telegram_app
    registry = app.bot_data["user_registry"]
    registry.ban(101)
    community = app.bot_data["community"]
    community.set_role(42, 101, True)

    async def scenario():
        async with app:
            await app.process_update(tapped(app, "unban_stars:buy"))
            invoice = next(data for method, data in calls if method == "sendInvoice")
            assert invoice["chat_id"] == 101 and invoice["currency"] == "XTR"
            assert invoice["prices"][0]["amount"] == 100 and invoice["start_parameter"]
            payload = invoice["payload"]
            for wrong in (
                _checkout(app, payload, user_id=102, sequence=5),
                _checkout(app, payload, amount=101, sequence=6),
            ):
                await app.process_update(wrong)
                assert calls[-1][0] == "answerPreCheckoutQuery" and not calls[-1][1]["ok"]
            await app.process_update(_checkout(app, payload))
            assert calls[-1][0] == "answerPreCheckoutQuery" and calls[-1][1]["ok"]
            assert registry.blocked_chat(101).permanent

            # Even if moderation changed access while Telegram completed checkout, honor payment.
            registry.ban(101)
            await app.process_update(_receipt(app, payload))
            assert registry.blocked_chat(101) is None and community.role(101) == "admin"
            assert "Доступ к боту восстановлен" in rendered(calls)[-1]["text"]
            assert "keyboard" in rendered(calls)[-1]["reply_markup"]

            registry.ban(101)
            calls.clear()
            await app.process_update(_receipt(app, payload, sequence=4))
            assert registry.blocked_chat(101).permanent
            assert not rendered(calls)

    asyncio.run(scenario())


def test_old_invoice_cannot_charge_after_unban_and_payment_support_works_while_banned(telegram_app):
    app, calls = telegram_app
    registry = app.bot_data["user_registry"]
    registry.remember(42, "synthetic_moderator")
    registry.ban(101)

    async def scenario():
        async with app:
            await app.process_update(incoming(app, "/paysupport"))
            assert "За платежи в боте отвечает" in rendered(calls)[-1]["text"]
            assert "https://t.me/synthetic_moderator" in str(rendered(calls)[-1]["reply_markup"])
            await app.process_update(tapped(app, "unban_stars:buy"))
            invoice = next(data for method, data in calls if method == "sendInvoice")
            registry.set_manual_access(
                101,
                banned=False,
                actor_id=42,
                expected_revision=registry.access_state(101).revision,
            )
            await app.process_update(_checkout(app, invoice["payload"]))
            assert calls[-1][0] == "answerPreCheckoutQuery" and not calls[-1][1]["ok"]
            calls.clear()
            await app.process_update(tapped(app, "unban_stars:buy"))
            assert not any(method == "sendInvoice" for method, _data in calls)

    asyncio.run(scenario())
