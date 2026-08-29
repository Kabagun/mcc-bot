"""At-most-once daily delivery, opt-in, role races and scheduling tests."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError
from telegram.ext import ApplicationBuilder

from mcc_bot.community import CommunityService
from mcc_bot.notifications import MINSK, install_jobs, send_daily_digest
from mcc_bot.stores import StoreRepository


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
