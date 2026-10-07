"""Voice call records, the daily quota and the one-call-per-user lock (SQLite)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from assistant.service import ensure_main_session
from cache.memory_cache import MemoryCache
from db.base import get_db_session
from db.models.voice import VoiceCall, VoiceTurn
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from voice import calls
from voice.meter import CallMeter


@pytest.fixture
def cache(monkeypatch):
    shared = MemoryCache()  # stands for Redis, which every worker shares
    monkeypatch.setattr(calls, "_cache", lambda: shared)
    return shared


async def new_call():
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    call_id = await calls.create_call(user_id=owner, workspace_id=workspace, main_session_id=main.id,
                                      client="web", model="qwen3.8-omni-flash-realtime", voice="Serena")
    return owner, call_id


async def test_one_call_per_user_across_workers(cache):
    assert await calls.acquire_lock("u1")
    assert not await calls.acquire_lock("u1")  # a second device, or a second worker
    assert await calls.acquire_lock("u2")
    await calls.renew_lock("u1")
    assert not await calls.acquire_lock("u1")  # a renewed lock is still an integer counter
    await calls.release_lock("u1")
    assert await calls.acquire_lock("u1")


async def test_finish_records_the_ledger_and_turn_stamps():
    owner, call_id = await new_call()
    meter = CallMeter()
    meter.settle("r1", "completed", {"input_tokens_details": {"text_tokens": 1000, "audio_tokens": 40},
                                     "output_tokens_details": {"text_tokens": 0, "audio_tokens": 100}})
    meter.audio("r2", b"\x00" * 48000)
    meter.finish()
    await calls.finish_call(call_id, status="ended", end_reason="hangup", duration_seconds=62.6, turns=2,
                            snapshot=meter.snapshot())
    await calls.add_turn(turn_id="t1", call_id=call_id, user_id=owner, provider_call_id="call-1",
                         transcript="帮我看看", inbox_id="inbox-1")
    await calls.update_turn("t1", settled=True, result_message_id="m2")
    await calls.update_turn("t1", delivered=True, outcome="delivered")
    async with get_db_session() as db:
        call = await db.get(VoiceCall, call_id)
        turn = await db.get(VoiceTurn, "t1")
    assert (call.status, call.end_reason, call.duration_seconds, call.turns) == ("ended", "hangup", 63, 2)
    assert call.usage == {"input_text": 1000, "input_audio": 40, "output_text": 0, "output_audio": 100}
    assert call.estimated_yuan == meter.snapshot()["total_yuan"] and call.unreported_rounds == 1
    assert call.ended_at is not None and call.price_date == "2026-10-07"
    assert (turn.outcome, turn.result_message_id) == ("delivered", "m2")
    assert turn.settled_at is not None and turn.delivered_at is not None and turn.requested_at is not None


async def test_summaries_are_saved_on_the_call_and_the_latest_recent_one_is_read_back():
    owner, first = await new_call()
    workspace = await _workspace(first)
    assert await calls.latest_call_summary(owner, workspace) is None
    assert await calls.previous_call(owner, workspace) is None
    meter = CallMeter()
    await calls.finish_call(first, status="ended", end_reason="hangup", duration_seconds=40, turns=1,
                            snapshot=meter.snapshot())
    await calls.save_summary(first, "聊了贪吃蛇的进展；用户希望被叫 Mary。" + "很长" * 400)
    second = await calls.create_call(user_id=owner, workspace_id=workspace, main_session_id=await _main(first),
                                     client="web", model="m", voice="Serena")
    await calls.finish_call(second, status="ended", end_reason="hangup", duration_seconds=20, turns=0,
                            snapshot=meter.snapshot())  # ended later, without a summary
    latest = await calls.latest_call_summary(owner, workspace)
    assert latest["call_id"] == first and latest["summary"].startswith("聊了贪吃蛇的进展")
    assert len(latest["summary"]) == calls.SUMMARY_CHARS and latest["ended_at"].tzinfo is not None
    previous = await calls.previous_call(owner, workspace)
    assert (previous["call_id"], previous["summary"]) == (second, "")
    stranger, _ = await new_call()
    assert await calls.latest_call_summary(stranger, workspace) is None  # only the owner's own calls
    async with get_db_session() as db:
        await db.execute(update(VoiceCall).where(VoiceCall.id == first).values(
            ended_at=datetime.now(timezone.utc) - timedelta(hours=25)))
    assert await calls.latest_call_summary(owner, workspace) is None  # older than a day
    assert (await calls.latest_call_summary(owner, workspace, within_hours=48))["call_id"] == first


async def _workspace(call_id):
    async with get_db_session() as db:
        return (await db.get(VoiceCall, call_id)).workspace_id


async def _main(call_id):
    async with get_db_session() as db:
        return (await db.get(VoiceCall, call_id)).main_session_id
