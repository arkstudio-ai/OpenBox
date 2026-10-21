"""Call refreshes use committed hints without losing races or heartbeat isolation."""
import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest

from bus import bus
from bus.events import AGENT_INBOX_UPDATED, ASSET_UPDATED, MESSAGE_TEXT_DELTA
from voice import updates


def watcher():
    return updates.CallUpdates(SimpleNamespace(user_id="u", workspace_id="w", main_session_id="s", call_id="c"))


async def stop(task, watch):
    watch.close()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_settlement_wakes_without_polling_and_coalesces_duplicate_hints(monkeypatch):
    monkeypatch.setattr(updates, "POLL_SECONDS", 60)
    watch = watcher()
    delivered, values = asyncio.Event(), []
    async def read():
        return len(values)
    async def deliver(value):
        values.append(value)
        delivered.set()
    task = asyncio.create_task(watch.watch(watch.reports, read, deliver, name="reports"))
    try:
        await asyncio.wait_for(delivered.wait(), .5)
        delivered.clear()
        for _ in range(30):
            bus.publish(AGENT_INBOX_UPDATED, {"userId": "u", "sessionId": "s"})
        await asyncio.wait_for(delivered.wait(), .5)
        assert values == [0, 1]
    finally:
        await stop(task, watch)


def test_hints_are_scoped_and_token_deltas_do_not_trigger_reads():
    watch = watcher()
    try:
        watch.reports.clear()
        watch.context.clear()
        for kind, data in [
            (AGENT_INBOX_UPDATED, {"userId": "other", "sessionId": "s"}),
            (AGENT_INBOX_UPDATED, {"userId": "u", "sessionId": "other"}),
            (ASSET_UPDATED, {"userId": "u", "workspaceId": "other"}),
            (MESSAGE_TEXT_DELTA, {"userId": "u", "sessionId": "s"}),
        ]:
            bus.publish(kind, data)
        assert not watch.reports.is_set() and not watch.context.is_set()
        bus.publish(ASSET_UPDATED, {"userId": "u", "workspaceId": "w"})
        assert watch.context.is_set() and not watch.reports.is_set()
        watch.context.clear()
        watch.close()
        bus.publish(ASSET_UPDATED, {"userId": "u", "workspaceId": "w"})
        assert not watch.context.is_set()
    finally:
        watch.close()


def test_manual_question_reply_and_other_conversation_status_wake_card_reads():
    from bus.events import QUESTION_REPLIED, QUESTION_REJECTED, SESSION_STATUS, SESSION_UPDATED
    watch = watcher()
    try:
        for kind in (QUESTION_REPLIED, QUESTION_REJECTED, SESSION_STATUS, SESSION_UPDATED):
            watch.cards.clear()
            watch.context.clear()
            bus.publish(kind, {"userId": "other", "session_id": "task"})
            assert not watch.cards.is_set()
            bus.publish(kind, {"userId": "u", "workspaceId": "other", "session_id": "task"})
            assert not watch.cards.is_set()
            bus.publish(kind, {"userId": "u", "session_id": "task"})
            assert watch.cards.is_set() and watch.context.is_set()
    finally:
        watch.close()


async def test_commit_during_read_is_not_lost(monkeypatch):
    monkeypatch.setattr(updates, "POLL_SECONDS", 60)
    watch, finished = watcher(), asyncio.Event()
    reads = 0
    async def read():
        nonlocal reads
        reads += 1
        if reads == 1:
            bus.publish(AGENT_INBOX_UPDATED, {"userId": "u", "sessionId": "s"})
        return reads
    async def deliver(value):
        if value == 2:
            finished.set()
    task = asyncio.create_task(watch.watch(watch.reports, read, deliver, name="reports"))
    try:
        await asyncio.wait_for(finished.wait(), .5)
        assert reads == 2
    finally:
        await stop(task, watch)


async def test_lost_hint_and_failed_read_recover_on_bounded_fallback(monkeypatch):
    monkeypatch.setattr(updates, "POLL_SECONDS", .01)
    watch, delivered = watcher(), asyncio.Event()
    reads = 0
    async def read():
        nonlocal reads
        reads += 1
        if reads == 1:
            raise TimeoutError("temporary database delay")
        return "committed without a hint"
    async def deliver(value):
        assert value == "committed without a hint"
        delivered.set()
    task = asyncio.create_task(watch.watch(watch.reports, read, deliver, name="reports"))
    try:
        await asyncio.wait_for(delivered.wait(), .5)
        assert reads == 2
    finally:
        await stop(task, watch)


async def test_cancel_waits_for_bounded_read_to_release_connection():
    watch, entered, released = watcher(), asyncio.Event(), asyncio.Event()
    async def read():
        entered.set()
        await released.wait()
        return None
    async def deliver(value):
        pytest.fail("a cancelled watcher must not deliver")
    task = asyncio.create_task(watch.watch(watch.context, read, deliver, name="context"))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    watch.close()
