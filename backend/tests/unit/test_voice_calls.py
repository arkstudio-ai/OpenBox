"""Voice call records, the daily quota and the one-call-per-user lock (SQLite)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

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


async def test_live_conversation_context_is_scoped_and_never_creates_business_turns(cache):
    from types import SimpleNamespace
    from voice import live_context
    from voice.tools import CallScope
    from voice.transcript import CallTranscript
    owner, identity = await new_call()
    async with get_db_session() as db:
        row = await db.get(VoiceCall, identity)
        main = SimpleNamespace(id=row.main_session_id, user_id=owner, workspace_id=row.workspace_id)
        scope = CallScope(owner, row.workspace_id, row.main_session_id, identity)
    earlier = datetime.now(timezone.utc) - timedelta(seconds=2)
    transcript = CallTranscript(now=lambda: earlier)
    transcript.add("user", "周末想去青桐公园走走")
    transcript.add("assistant", "可以带点水")
    transcript.add("note", "内部后台结果不能冒充用户说的话")
    transcript.now = lambda: earlier + timedelta(seconds=10)
    transcript.add("user", "后来才聊到书店")
    await live_context.publish(scope, transcript.lines)
    async with get_db_session() as db:
        value = await live_context.read(db, main)
        assert [line["speaker"] for line in value["lines"]] == ["user", "assistant", "user"]
        as_of_input = await live_context.read(db, main, before=earlier + timedelta(seconds=1))
        assert [line["text"] for line in as_of_input["lines"]] == ["周末想去青桐公园走走", "可以带点水"]
        assert await live_context.read(db, main, before=earlier - timedelta(seconds=1)) is None
        assert not (await db.scalars(select(VoiceTurn).where(VoiceTurn.call_id == identity))).all()
        assert await live_context.read(db, SimpleNamespace(id=main.id, user_id="stranger", workspace_id=main.workspace_id)) is None
        # Cached text cannot grant access without the matching owned SQL call.
        await cache.set(live_context.key(owner, main.workspace_id, main.id), {**value, "call_id": "another-call"})
        assert await live_context.read(db, main) is None


async def test_revoked_live_context_is_hidden_before_socket_refresh_and_empty_publish_replaces_it(cache, monkeypatch):
    from types import SimpleNamespace
    from core.config import OpenBoxConfig
    from memory import service
    from voice import live_context
    from voice.memory_context import RecentContext
    from voice.tools import CallScope
    from voice.transcript import CallTranscript
    owner, identity = await new_call()
    async with get_db_session() as db:
        row = await db.get(VoiceCall, identity)
        main = SimpleNamespace(id=row.main_session_id, user_id=owner, workspace_id=row.workspace_id)
        scope = CallScope(owner, row.workspace_id, row.main_session_id, identity)
    note = await service.create_note(user_id=owner, workspace_id=main.workspace_id, summary="周六读书")
    transcript = CallTranscript()
    transcript.add("assistant", "周六读书")
    config = OpenBoxConfig(model="test/model", memory={"retrieval_v2": True})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    await live_context.publish(scope, transcript.lines)
    async with get_db_session() as db:
        assert await live_context.read(db, main) is None  # pre-upgrade Redis text has no epoch
    await live_context.publish(scope, RecentContext(transcript.lines, 0))
    async with get_db_session() as db:
        assert await live_context.read(db, main)
    await service.delete_memory(user_id=owner, workspace_id=main.workspace_id, memory_id=note["id"])
    async with get_db_session() as db:
        assert await live_context.read(db, main) is None
    assert (await cache.get(live_context.key(owner, main.workspace_id, main.id)))["lines"]
    await live_context.publish(scope, RecentContext([], 1))
    assert (await cache.get(live_context.key(owner, main.workspace_id, main.id)))["lines"] == []


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
