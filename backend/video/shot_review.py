"""Read finished Light video shots in the background and meter their combined duration."""
from __future__ import annotations

import asyncio
import contextvars
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import func, or_, select, update

from core.log import create_logger
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.session import Session
from db.models.video_job import VideoJob, VideoShotReview as Review
from tool.tool import ToolContext

log = create_logger("video.shot_review")
WINDOW = timedelta(seconds=5)
READ_TIMEOUT = 120
LEASE = timedelta(seconds=READ_TIMEOUT + 60)
LIMIT = 10
PENDING = ("queued", "reading")
_worker: asyncio.Task | None = None
_event: asyncio.Event | None = None


def _group(session, *, previously_queued: bool = False) -> str | None:
    if not session or session.is_deleted or session.light_entry != "video":
        return None
    run = (session.light_production or {}).get("run") or {}
    enabled = run.get("perfect_film", previously_queued)
    if enabled is True and run.get("action") == "render" and not run.get("remake_version"):
        return run.get("job_prefix")
    return None


async def policy(db, ctx: ToolContext, key: str) -> dict | None:
    """Opt in at reservation, only for a shot of the owner's confirmed App render."""
    if not ctx.run_id or not key.startswith(f"light:{ctx.session_id}:"):
        return None
    session = await db.get(Session, ctx.session_id)
    group = _group(session)
    if (not group or session.user_id != ctx.user_id or session.workspace_id != ctx.workspace_id
            or not key.startswith(group + ":shot")):
        return None
    from video.vision_contract import selected_model
    return {"group": group, "model": selected_model(ctx.model_id or session.model)}


def enqueue(db, job, config: dict) -> None:
    """Called in the transaction that first marks the generated file complete."""
    db.add(Review(job_id=job.id, asset_id=job.output_asset_id, session_id=job.session_id,
                  group_key=config["group"], model=config["model"], state="queued",
                  available_at=job.completed_at + WINDOW))


def notify() -> None:
    if _event is not None:
        _event.set()


def start() -> None:
    global _worker, _event
    if _worker is None or _worker.done():
        _event = asyncio.Event()
        _worker = asyncio.create_task(_run(_event), name="video-shot-reviews", context=contextvars.Context())


async def stop() -> None:
    global _worker, _event
    if _worker is not None:
        _worker.cancel()
        await asyncio.gather(_worker, return_exceptions=True)
    _worker = _event = None


def _eligible(now):
    # Once the oldest has waited five seconds, include newer arrivals in its group.
    return or_((Review.state == "queued") & (Review.available_at <= now + WINDOW),
               (Review.state == "reading") & (Review.available_at <= now))


async def _claim(now: datetime, capacity: int):
    async with get_db_session() as db:
        first = await db.scalar(select(Review).where(Review.state.in_(PENDING), Review.available_at <= now)
                                .order_by(Review.available_at, Review.job_id).limit(1).with_for_update(skip_locked=True))
        if first is None:
            return None
        session = await db.get(Session, first.session_id)
        scope = (Review.session_id == first.session_id, Review.group_key == first.group_key)
        due = _eligible(now)
        ids = (await db.scalars(select(Review.job_id).where(*scope, due)
                               .order_by(Review.available_at, Review.job_id).limit(capacity)
                               .with_for_update(skip_locked=True))).all()
        if _group(session, previously_queued=True) != first.group_key:
            await db.execute(update(Review).where(*scope, Review.state.in_(PENDING)).values(
                state="skipped", claim=None, finished_at=now, report={"error": "This production version is no longer active."}))
            return []
        claim = uuid4().hex
        rows = (await db.scalars(update(Review).where(Review.job_id.in_(ids), due).values(
            state="reading", claim=claim, attempts=Review.attempts + 1,
            started_at=func.coalesce(Review.started_at, now), available_at=now + LEASE
        ).returning(Review).execution_options(synchronize_session=False, populate_existing=True))).all()
        return [(row, ToolContext(user_id=session.user_id, workspace_id=session.workspace_id,
                                  session_id=session.id, model_id=row.model)) for row in rows]


async def _finish(row: Review, ctx: ToolContext, clip: dict | None, error: str | None) -> bool:
    """Commit the evidence, the duration increment and any debit in one transaction."""
    from billing import media
    from billing.footage import ANALYZE_KIND, quote_review_increment

    async with get_db_session() as db:
        # The same lock used by media.settle serializes all reviewers of a workspace,
        # including a recovered lease on another process. Re-check the claim under it.
        account = await media.lock_balance(db, ctx.workspace_id) if media.billing_mode() != "off" else None
        current = await db.scalar(select(Review).where(Review.job_id == row.job_id,
                                  Review.state == "reading", Review.claim == row.claim).with_for_update())
        if current is None:
            return False
        if error:
            current.state = "queued" if current.attempts < 2 else "failed"
            current.report = {"error": error}
            current.available_at = datetime.now(timezone.utc) + WINDOW
            if current.state == "failed":
                current.finished_at = datetime.now(timezone.utc)
            return current.state == "failed"
        ms = round(clip["duration_sec"] * 1000)
        if account is not None:
            previous = await db.scalar(select(func.sum(Review.duration_ms)).where(
                Review.session_id == row.session_id, Review.group_key == row.group_key, Review.state == "ready")) or 0
            price = quote_review_increment(previous, ms)
            if price.minutes_billed:
                group_id = sha256((row.session_id + "|" + row.group_key).encode()).hexdigest()[:32]
                await media.settle_locked(db, account, key=f"shot-review:{group_id}:{price.snapshot['cumulative_minutes']}",
                    workspace_id=ctx.workspace_id, user_id=ctx.user_id, session_id=row.session_id, price=price,
                    kind=ANALYZE_KIND, quantity_known=True, tokens={"duration_sec": ms / 1000,
                        "cumulative_duration_sec": (previous + ms) / 1000, "minutes_billed": price.minutes_billed},
                    default_title="素材分析", title="素材分析")
        current.state, current.report, current.duration_ms = "ready", clip, ms
        current.finished_at = datetime.now(timezone.utc)
    return True


async def _read(row, ctx):
    from assistant.scheduling import require_runnable
    from core.oss import get_oss
    from tool.footage_analyze import _one

    clip = error = None
    try:
        await require_runnable(ctx.session_id, ctx.user_id)
        clip = await asyncio.wait_for(_one(0, row.asset_id, ctx, get_oss(), transcribe=True,
                                           word_times=True, precise_duration=True), READ_TIMEOUT)
        if clip.get("note") and not clip.get("segments") and not clip.get("transcript"):
            error = "Picture and speech could not be read. Check this shot separately."
    except Exception as exc:
        log.warning("Shot review %s did not finish (%s)", row.job_id, type(exc).__name__)
        error = "The shot could not be read. Check this shot separately."
    if await _finish(row, ctx, clip, error):
        from video.job_recovery import _load, resume_conversation
        job = await _load(row.job_id)
        if job is not None:
            await resume_conversation(job)


async def _run(event: asyncio.Event) -> None:
    running: dict[asyncio.Task, str] = {}

    def finished(task):
        running.pop(task, None)
        if not task.cancelled() and task.exception():
            log.warning("Shot review worker: %s", type(task.exception()).__name__)
        event.set()

    try:
        while True:
            event.clear()
            try:
                while len(running) < LIMIT:
                    claimed = await _claim(datetime.now(timezone.utc), LIMIT - len(running))
                    if claimed is None:
                        break
                    for row, ctx in claimed:
                        task = asyncio.create_task(_read(row, ctx))
                        running[task] = row.claim
                        task.add_done_callback(finished)
                delay = 30.
                if len(running) < LIMIT:
                    async with get_db_session() as db:
                        due = await db.scalar(select(func.min(Review.available_at)).where(Review.state.in_(PENDING)))
                    if due:
                        delay = max(.1, min(delay, (due.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()))
            except Exception as exc:
                log.warning("Shot review queue unavailable (%s)", type(exc).__name__)
                delay = 5.
            try:
                await asyncio.wait_for(event.wait(), timeout=delay)
            except TimeoutError:
                pass
    finally:
        tasks = dict(running)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if tasks:
            # These tasks have stopped in this process. Other workers' leases stay intact.
            async with get_db_session() as db:
                await db.execute(update(Review).where(Review.claim.in_(set(tasks.values())), Review.state == "reading")
                                 .values(state="queued", claim=None, available_at=datetime.now(timezone.utc)))


async def group_complete(job) -> bool:
    """Raw render completions and partial review batches never start agent turns."""
    config = (job.request_data or {}).get("background_review")
    if not config:
        return True
    async with get_db_session() as db:
        scope = (Review.session_id == job.session_id, Review.group_key == config["group"])
        pending_review = await db.scalar(select(Review.job_id).where(*scope, Review.state.in_(PENDING)).limit(1))
        if pending_review:
            return False
        # Use an escaped literal prefix: session IDs contain underscores.
        pending_job = await db.scalar(select(VideoJob.id).where(VideoJob.user_id == job.user_id,
            VideoJob.session_id == job.session_id, VideoJob.kind == "segment",
            VideoJob.idempotency_key.startswith(config["group"] + ":shot", autoescape=True),
            VideoJob.status.notin_(("completed", "failed", "cancelled"))).limit(1))
        return pending_job is None


def _owned(ctx):
    return select(Review).join(FileAsset, FileAsset.id == Review.asset_id).where(
        Review.session_id == ctx.session_id, FileAsset.session_id == ctx.session_id, FileAsset.user_id == ctx.user_id,
        FileAsset.workspace_id == ctx.workspace_id, FileAsset.status == "ready", FileAsset.is_deleted.is_(False))


async def states(ctx, jobs) -> dict[str, str]:
    async with get_db_session() as db:
        return dict((await db.execute(_owned(ctx).with_only_columns(Review.job_id, Review.state)
                                      .where(Review.job_id.in_(jobs)))).all())


async def owned(ctx, *, assets=(), jobs=()) -> dict[str, Review]:
    """Only return reports for live assets belonging to this account, workspace and chat."""
    key = Review.asset_id if assets else Review.job_id
    async with get_db_session() as db:
        rows = (await db.scalars(_owned(ctx).where(key.in_(assets or jobs)))).all()
    return {(row.asset_id if assets else row.job_id): row for row in rows}


async def reserved_assets(ctx, ids: list[str]) -> dict[str, str]:
    """A just-uploaded render already belongs to its review even before finalization commits."""
    async with get_db_session() as db:
        rows = (await db.execute(select(FileAsset.id, VideoJob.id, VideoJob.request_data)
            .join(VideoJob, VideoJob.output_asset_id == FileAsset.id).where(
                FileAsset.id.in_(ids), FileAsset.user_id == ctx.user_id, FileAsset.workspace_id == ctx.workspace_id,
                FileAsset.session_id == ctx.session_id, FileAsset.is_deleted.is_(False),
                VideoJob.kind == "segment", VideoJob.session_id == ctx.session_id, VideoJob.user_id == ctx.user_id))).all()
    return {asset_id: job_id for asset_id, job_id, request in rows if (request or {}).get("background_review")}
