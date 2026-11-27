"""Regression coverage for background shot QA, its receipts and its cumulative bill."""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from billing import media
from billing.footage import quote_review_increment
from db.base import get_db_session
from db.models.billing import CreditLedger, UsageEvent
from db.models.file_asset import FileAsset
from db.models.session import Session
from db.models.video_job import VideoShotReview as Review
from db.repository.user_repo import PgUserRepo
from session.session import create_session
from tool import footage_analyze as footage, video_production as video
from tool.tool import ToolContext
from video import ready, shot_review as review


async def production():
    uid = uuid4().hex
    user = await PgUserRepo().create(id=uid, username="rv" + uid[:10], password_hash=None)
    s = await create_session(model="qa-vision", agent="build", user_id=uid, workspace_id=user["default_workspace_id"])
    key = f"light:{s.id}:1"
    ctx = ToolContext(session_id=s.id, user_id=uid, workspace_id=user["default_workspace_id"],
                      model_id="qa-vision", run_id="r")
    async with get_db_session() as db:
        row = await db.get(Session, s.id)
        row.light_entry, row.status = "video", "busy"
        row.light_production = {"run": {"action": "render", "job_prefix": key, "perfect_film": True}}
    return ctx, key


async def reserve(ctx, key, shot):
    job, asset, _ = await video._create_pending_job(ctx=ctx, kind="segment", idempotency_key=f"{key}:shot{shot}",
        model="test", prompt=None, request_hash=str(shot), filename=None, request_data={"submit_run_id":"r", "shot":shot})
    await video._update_job(job.id, status="in_progress", provider_task_id="test")
    async with get_db_session() as db:
        row = await db.get(FileAsset, asset.id)
        row.status, row.size = "ready", 10
    return job


def report(asset, duration):
    return {"asset_id": asset, "duration_sec": duration, "transcript": "对白", "words": [{"text": "对白"}],
            "segments": [{"shot": "人物接住商品"}]}


async def finish(job, at):
    await video._update_job(job.id, status="completed", completed_at=at)


async def events(sid):
    async with get_db_session() as db:
        return (await db.scalars(select(UsageEvent).where(UsageEvent.session_id == sid))).all()


def test_round_only_cumulative_milliseconds():
    assert quote_review_increment(0, 0).minutes_billed == 0
    assert quote_review_increment(0, 4458).minutes_billed == 1
    assert quote_review_increment(4458, 55542).minutes_billed == 0
    assert quote_review_increment(60000, 1).minutes_billed == 1
    assert quote_review_increment(60001, 120000).minutes_billed == 2


async def test_completions_coalesce_without_moving_the_first_deadline_and_only_new_minutes_debit(monkeypatch):
    monkeypatch.setattr(media, "billing_mode", lambda: "enforce")
    ctx, key = await production()
    jobs = [await reserve(ctx, key, i) for i in range(1, 4)]
    at = datetime.now(timezone.utc)
    await finish(jobs[0], at)
    await finish(jobs[1], at + timedelta(seconds=4))
    assert await review._claim(at + timedelta(seconds=4.999), 10) is None
    first = await review._claim(at + review.WINDOW, 10)
    assert [r.job_id for r, _ in first] == [job.id for job in jobs[:2]]
    for (r, c), seconds in zip(first, (39.999, 20.001)):
        assert await review._finish(r, c, report(r.asset_id, seconds), None)
        assert not await review._finish(r, c, report(r.asset_id, seconds), None)
    assert not await review.group_complete(jobs[0])
    await finish(jobs[2], at + timedelta(seconds=30))
    ((r, c),) = await review._claim(at + timedelta(seconds=35), 10)
    await review._finish(r, c, report(r.asset_id, .001), None)
    assert await review.group_complete(jobs[2])
    fees = await events(ctx.session_id)
    assert sorted(row.tokens["minutes_billed"] for row in fees) == [1, 1]
    assert sum(row.credits for row in fees) == Decimal("2")
    async with get_db_session() as db:
        ledger = (await db.scalars(select(CreditLedger).where(CreditLedger.reference_id.in_([row.id for row in fees])))).all()
    assert sum(row.amount for row in ledger) == -2

    # A different explicit grouping of the same assets must re-use the reads and fee.
    async def no_read(*a, **kw):
        pytest.fail("re-reading a reviewed shot would cost twice")
    monkeypatch.setattr(footage, "_one", no_read)
    monkeypatch.setattr("video.bgm_library.tracks", lambda: asyncio.sleep(0, result=[]))
    ids = [jobs[2].output_asset_id, jobs[0].output_asset_id]
    reused = await footage.execute(footage.FootageAnalyzeArgs(asset_ids=ids, word_times=True), ctx)
    assert reused.metadata["analysis_credits"] == "0"
    assert [clip["asset_id"] for clip in reused.metadata["clips"]] == ids
    assert reused.metadata["clips"][0]["words"]
    assert len(await events(ctx.session_id)) == 2
    assert not await review.owned(ToolContext(user_id="someone", workspace_id=ctx.workspace_id,
        session_id=ctx.session_id), assets=ids)


@pytest.fixture
async def concurrent_review_db(monkeypatch, tmp_path):
    # The suite's in-memory SQLite fixture multiplexes concurrent transactions on one connection.
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from db import base
    url = os.environ.get("OPENBOX_SHOT_REVIEW_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'review.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)
    monkeypatch.setattr(base, "_session_factory", async_sessionmaker(engine, expire_on_commit=False))
    try:
        yield
    finally:
        await engine.dispose()


async def test_worker_reads_while_agent_is_busy_and_one_wait_receives_all_evidence(monkeypatch, concurrent_review_db):
    ctx, key = await production()
    a, b = [await reserve(ctx, key, i) for i in (1, 2)]
    read_a, reads, wakes = asyncio.Event(), [], []
    async def read(_, asset, tool_ctx, oss, **kw):
        assert tool_ctx.model_id == "qa-vision" and kw["word_times"] and kw["transcribe"]
        reads.append(asset)
        if asset == a.output_asset_id:
            read_a.set()
        return report(asset, 7.3)
    async def attached(*_):
        return True
    async def should_not_wake(*args, **kw):
        wakes.append(1)
    monkeypatch.setattr("core.oss.get_oss", object)
    monkeypatch.setattr(footage, "_one", read)
    monkeypatch.setattr("agent.inbox.wake_inbox_session", should_not_wake)
    monkeypatch.setattr(video, "_attach_completed", attached)
    monkeypatch.setattr(review, "WINDOW", timedelta(milliseconds=40))
    monkeypatch.setattr(ready, "POLL_SECONDS", .015)
    monkeypatch.setattr(ready, "watch", lambda _: None)
    review.start()
    try:
        await finish(a, datetime.now(timezone.utc))
        # The worker starts before the agent ever calls await_ready.
        await asyncio.wait_for(read_a.wait(), 2)
        waiting = asyncio.create_task(video.execute_generate(video.VideoGenerateArgs(action="await_ready", job_ids=[a.id,b.id]), ctx))
        await asyncio.sleep(.07)
        assert not waiting.done()
        await finish(b, datetime.now(timezone.utc))
        got = await asyncio.wait_for(waiting, 3)
        assert [item["review"]["words"] for item in got.metadata["jobs"]] == [[{"text":"对白"}]] * 2
        assert not got.metadata["pending_job_ids"] and not wakes
        assert reads == [a.output_asset_id, b.output_asset_id]
        assert sum(row.credits for row in await events(ctx.session_id)) == 1
    finally:
        await review.stop()


async def test_reclaim_stale_lease_fences_old_worker_and_failure_reaches_waiter(monkeypatch):
    ctx, key = await production()
    a, b = [await reserve(ctx, key, i) for i in (1, 2)]
    at = datetime.now(timezone.utc)
    await finish(a, at)
    ((old, _),) = await review._claim(at + review.WINDOW, 1)
    assert await review._claim(at + review.WINDOW, 1) is None
    ((fresh, _),) = await review._claim(at + review.WINDOW + review.LEASE, 1)
    assert fresh.claim != old.claim
    assert not await review._finish(old, ctx, report(old.asset_id, 5), None)
    assert await review._finish(fresh, ctx, None, "Unreadable picture and audio")
    monkeypatch.setattr(ready, "watch", lambda _: None)
    got = await ready.wait_for_ready("segment", [a.id, b.id], ctx)
    assert [job.id for job in got] == [a.id]
    assert not await events(ctx.session_id)
    # A later form revision must not start work on a yet-unread older take.
    await finish(b, at)
    async with get_db_session() as db:
        row = await db.get(Session, ctx.session_id)
        row.light_production = {"run":{"action":"render", "job_prefix":key + "new"}}
    assert await review._claim(at + review.WINDOW, 10) == []
    assert (await review.owned(ctx, jobs=[b.id]))[b.id].state == "skipped"


async def test_only_own_current_render_can_enable_background_reading():
    ctx, key = await production()
    reserved = await reserve(ctx, key, 1)
    early = await footage.execute(footage.FootageAnalyzeArgs(asset_ids=[reserved.output_asset_id]), ctx)
    assert early.metadata["error"] and reserved.id in early.output and not await events(ctx.session_id)
    async with get_db_session() as db:
        assert await review.policy(db, ctx, key + ":shot1") == {"group":key, "model":"qa-vision"}
        assert await review.policy(db, ctx, key + "0:shot1") is None
        assert await review.policy(db, ctx, key + ":final") is None
        ctx.workspace_id = "other-workspace"
        assert await review.policy(db, ctx, key + ":shot1") is None


async def test_concurrent_settlement_and_retry_are_atomic(monkeypatch, concurrent_review_db):
    monkeypatch.setattr(media, "billing_mode", lambda: "enforce")
    ctx, key = await production()
    at = datetime.now(timezone.utc)
    for i in range(3):
        await finish(await reserve(ctx, key, i + 1), at)
    left, right = await asyncio.gather(review._claim(at + review.WINDOW, 2), review._claim(at + review.WINDOW, 2))
    items = (left or []) + (right or [])
    if len(items) < 3:
        items += await review._claim(at + review.WINDOW, 3)
    assert len({row.job_id for row, _ in items}) == len(items) == 3
    settle = media.settle_locked
    async def crash(*a, **k):
        await settle(*a, **k)
        raise RuntimeError("interrupted before commit")
    monkeypatch.setattr(media, "settle_locked", crash)
    row, owner = items[0]
    with pytest.raises(RuntimeError, match="interrupted"):
        await review._finish(row, owner, report(row.asset_id, 23), None)
    assert not await events(ctx.session_id)
    monkeypatch.setattr(media, "settle_locked", settle)
    completed = await asyncio.gather(*(review._finish(row, owner, report(row.asset_id, 23), None)
                                       for row, owner in items * 2))
    assert sum(completed) == 3
    fees = await events(ctx.session_id)
    assert sum(e.credits for e in fees) == 2
    rows = await review.owned(ctx, jobs=[row.job_id for row, _ in items])
    assert all(r.state == "ready" and r.started_at and r.finished_at for r in rows.values())
