"""At-most-once daily delivery, opt-in, role races and scheduling tests."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden, NetworkError
from telegram.ext import ApplicationBuilder

from mcc_bot.community import CommunityService
from mcc_bot.notifications import (
    MINSK,
    install_jobs,
    send_daily_digest,
    send_reminder_invitations,
)
from mcc_bot.stores import StoreRepository
from mcc_bot.users import UserRegistry


@pytest.fixture
def service(tmp_path):
    value = CommunityService(StoreRepository(tmp_path / "stores.sqlite3"), owner_id=1)
    value.initialize()
    value.set_role(1, 2, True)
    return value


def pending(service):
    draft = service.begin(
        10,
        data={
            "kind": "add_merchant",
            "payload": {"name": "Shop", "channel": "offline", "mcc": "5411"},
        },
    )
    draft = service.advance(
        10, draft.id, draft.version, "preview", draft.data, media=("file", "unique")
    )
    return service.submit(10, draft.id, draft.version)


def run(service, bot, day=26, hour=20):
    return asyncio.run(
        send_daily_digest(service, bot, now=datetime(2026, 8, day, hour, tzinfo=MINSK))
    )


def test_no_queue_no_consent_no_recipients(service):
    bot = SimpleNamespace(send_message=AsyncMock())
    service.set_digest(2, True)
    assert run(service, bot)["sent"] == 0
    pending(service)
    service.set_digest(2, False)
    assert run(service, bot)["sent"] == 0
    bot.send_message.assert_not_awaited()
    assert len(service.queue(2)) == 1


def test_count_only_and_daily_deduplication(service):
    pending(service)
    service.set_digest(2, True)
    bot = SimpleNamespace(send_message=AsyncMock())
    assert run(service, bot, hour=19)["sent"] == 0
    assert run(service, bot)["sent"] == 1
    assert run(service, bot)["sent"] == 0
    assert run(service, bot, day=28)["sent"] == 1
    assert bot.send_message.await_count == 2
    call = bot.send_message.await_args.kwargs
    assert call["chat_id"] == 2
    assert "1" in call["text"] and "Shop" not in call["text"]
    assert call["disable_notification"] is True
    assert call["reply_markup"].inline_keyboard[0][0].callback_data == "community:queue:0"
    with service.stores.connection() as conn:
        assert [
            row[0] for row in conn.execute("SELECT day FROM community_digests ORDER BY day")
        ] == ["2026-08-26", "2026-08-28"]


def test_uncertain_send_is_not_retried_after_restart(service):
    pending(service)
    service.set_digest(2, True)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=NetworkError("timeout")))
    assert run(service, bot)["uncertain"] == 1
    reopened = CommunityService(service.stores, owner_id=1)
    reopened.initialize()
    assert run(reopened, bot)["uncertain"] == 0
    bot.send_message.assert_awaited_once()


def test_forbidden_digest_blocks_and_skips_future_notifications(service, tmp_path):
    pending(service)
    service.set_digest(2, True)
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(2)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=Forbidden("blocked")))

    first = asyncio.run(
        send_daily_digest(
            service, bot, registry=registry, now=datetime(2026, 8, 26, 20, tzinfo=MINSK)
        )
    )
    second = asyncio.run(
        send_daily_digest(
            service, bot, registry=registry, now=datetime(2026, 8, 27, 20, tzinfo=MINSK)
        )
    )

    assert first["uncertain"] == 1
    assert second["skipped"] == 1
    assert bot.send_message.await_count == 1
    assert registry.blocked_chat(2) is not None


def test_intent_is_durable_before_call(service):
    pending(service)
    service.set_digest(2, True)

    async def send(**kwargs):
        with service.stores.connection() as conn:
            assert conn.execute("SELECT state FROM community_digests").fetchone()[0] == "intent"

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    assert run(service, bot)["sent"] == 1


def test_revoke_between_reserve_and_send_skips(service, monkeypatch):
    pending(service)
    service.set_digest(2, True)
    original = service.reserve_digest

    def revoke(*args):
        result = original(*args)
        service.set_role(1, 2, False)
        return result

    monkeypatch.setattr(service, "reserve_digest", revoke)
    bot = SimpleNamespace(send_message=AsyncMock())
    assert run(service, bot)["skipped"] == 1
    bot.send_message.assert_not_awaited()


def test_claim_between_reserve_and_send_is_removed_from_digest_count(service, monkeypatch):
    proposal = pending(service)
    service.set_digest(2, True)
    original = service.reserve_digest

    def claim_after_reserve(*args):
        result = original(*args)
        service.claim(2, proposal.id, proposal.version)
        return result

    monkeypatch.setattr(service, "reserve_digest", claim_after_reserve)
    bot = SimpleNamespace(send_message=AsyncMock())

    assert run(service, bot)["skipped"] == 1
    bot.send_message.assert_not_awaited()


def test_jobqueue_uses_minsk_20_and_no_catchup():
    app = ApplicationBuilder().token("123456:unit-test").build()
    install_jobs(app)
    jobs = app.job_queue.jobs()
    digest = next(job for job in jobs if job.name == "community-daily-digest")
    assert str(digest.job.trigger.timezone) == "Europe/Minsk"
    assert digest.job.misfire_grace_time == 60
    assert "hour='20'" in str(digest.job.trigger)
    assert any(job.name == "community-media-cleanup" for job in jobs)
    invitations = next(job for job in jobs if job.name == "community-reminder-invitations")
    assert invitations.job.max_instances == 1
    assert invitations.job.trigger.interval.total_seconds() == 60


def invite(service, bot, *, registry=None, day=26):
    return asyncio.run(
        send_reminder_invitations(
            service, bot, registry=registry, now=datetime(2026, 8, day, 10, tzinfo=MINSK)
        )
    )


def test_initial_invitation_reaches_all_reviewers_once_and_is_silent(service):
    pending(service)
    service.set_role(1, 3, True)
    service.set_role(1, 3, True, role="superadmin")
    bot = SimpleNamespace(send_message=AsyncMock())
    assert invite(service, bot)["sent"] == 3
    assert {call.kwargs["chat_id"] for call in bot.send_message.await_args_list} == {1, 2, 3}
    for call in bot.send_message.await_args_list:
        assert call.kwargs["disable_notification"] is True
        assert "Давайте развивать его вместе" in call.kwargs["text"]  # noqa: RUF001
        assert "Откажетесь — вероятность бана вырастет в разы 😈" in call.kwargs["text"]
        buttons = [b for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
        assert [b.text for b in buttons] == ["Да, напоминать", "Нет, спасибо"]
    assert not service.digest_enabled(2)
    service.set_digest(2, True)
    assert run(service, bot)["sent"] == 0
    assert invite(service, bot, day=27)["sent"] == 0


def test_only_helper_applications_notify_owner_and_superadmin(service):
    service.set_role(1, 3, True)
    service.set_role(1, 3, True, role="superadmin")
    service.request_role(10, username="volunteer", first_name="Test")
    bot = SimpleNamespace(send_message=AsyncMock())
    assert invite(service, bot)["sent"] == 2
    assert {call.kwargs["chat_id"] for call in bot.send_message.await_args_list} == {1, 3}
    for call in bot.send_message.await_args_list:
        assert "Заявок в помощники: 1" in call.kwargs["text"]
        assert "volunteer" not in call.kwargs["text"]
    for uid in (1, 2, 3):
        service.set_digest(uid, True)
    bot.send_message.reset_mock()
    assert run(service, bot, day=27)["sent"] == 2
    assert {call.kwargs["chat_id"] for call in bot.send_message.await_args_list} == {1, 3}
    assert bot.send_message.await_args.kwargs["reply_markup"].inline_keyboard[0][
        0
    ].callback_data == ("community:roles:0")


def test_invitation_requires_registered_eligible_chat_and_does_not_retry_uncertain(
    service, tmp_path
):
    pending(service)
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(2)
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=NetworkError("uncertain")))
    assert invite(service, bot, registry=registry)["uncertain"] == 1
    reopened = CommunityService(service.stores, owner_id=1)
    reopened.initialize()
    assert invite(reopened, bot, registry=registry, day=27)["sent"] == 0
    bot.send_message.assert_awaited_once()


def test_role_revoked_before_invitation_does_not_receive_message(service, tmp_path, monkeypatch):
    pending(service)
    registry = UserRegistry(tmp_path / "users.sqlite3")
    registry.initialize()
    registry.remember(2)
    original = service.reserve_reminder_invitation

    def revoke(user_id, day):
        reserved = original(user_id, day)
        service.set_role(1, user_id, False)
        return reserved

    monkeypatch.setattr(service, "reserve_reminder_invitation", revoke)
    bot = SimpleNamespace(send_message=AsyncMock())
    assert invite(service, bot, registry=registry)["sent"] == 0
    bot.send_message.assert_not_awaited()
