"""Periodic cleanup, piggybacked on the cron timer tick.

Cron run transcripts are real (kind='cron') sessions the user can open from
run history, so retention is generous but bounded: the newest
cron_transcript_keep_per_job transcripts per job survive, everything is capped
at RETENTION_DAYS, and cron_runs rows die with the cap. Also reclaims project
directories left behind in the sandbox by projects the user deleted — the
sandbox outlives individual sessions, and WUYING's delete_container is a
no-op, so without this sweep a deleted project's files stay on disk
indefinitely.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

from core.log import create_logger

log = create_logger("cron.reaper")

REAPER_INTERVAL_MS = 5 * 60 * 1000   # 5 minutes between sweeps
RETENTION_DAYS = 30                    # Keep cron_runs (and their transcripts) 30 days
ACTIVE_OUTBOX_STATES = ("pending", "processing")

_last_sweep_at_ms: int = 0


async def sweep_if_due() -> None:
    """Run sweep if enough time has passed since the last one.

    Call this from the timer tick's finally block.
    """
    global _last_sweep_at_ms
    now_ms = int(time.time() * 1000)

    if now_ms - _last_sweep_at_ms < REAPER_INTERVAL_MS:
        return

    _last_sweep_at_ms = now_ms

    # Each step is isolated: a sandbox that is unreachable must not stop the
    # database-side cleanup, which is the part that always works.
    for step in (_sweep_temp_sessions, _sweep_old_runs, _sweep_workspace):
        try:
            await step()
        except Exception as e:
            log.error(f"Reaper step {step.__name__} failed: {e}")


async def _sweep_temp_sessions() -> None:
    """Trim run transcripts beyond the per-job keep count.

    For each job, transcripts past the newest N (config
    cron_transcript_keep_per_job) are deleted and their run rows keep only the
    summary (temp_session_id goes NULL, so the UI stops offering "view
    transcript"). Running runs never count against the window.
    """
    from core.config import get_config
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox, CronRun
    from sqlalchemy import func, select, update

    keep = max(0, get_config().cron_transcript_keep_per_job)

    from db.models.session import Session as SessionORM

    rn = (
        func.row_number()
        .over(partition_by=CronRun.job_id, order_by=CronRun.started_at.desc())
        .label("rn")
    )
    ranked = (
        select(CronRun.id, CronRun.temp_session_id, rn)
        .where(
            CronRun.temp_session_id.isnot(None),
            CronRun.status != "running",
            ~select(CronDeliveryOutbox.id).where(
                CronDeliveryOutbox.run_id == CronRun.id,
                CronDeliveryOutbox.state.in_(ACTIVE_OUTBOX_STATES),
            ).exists(),
        )
        .subquery()
    )

    async with get_db_session() as db:
        # The owning user comes along: delete_session scopes its update by
        # user_id, and passing "default" against a real ULID matched no rows,
        # so nothing was ever actually reaped.
        result = await db.execute(
            select(ranked.c.id, ranked.c.temp_session_id, SessionORM.user_id)
            .join(SessionORM, SessionORM.id == ranked.c.temp_session_id)
            .where(ranked.c.rn > keep)
        )
        expired = result.all()

    if not expired:
        return

    from session.session import delete_session

    deleted = 0
    for run_id, sid, uid in expired:
        try:
            if await delete_session(sid, user_id=uid):
                deleted += 1
        except Exception as exc:
            # Keep the only durable link to a still-live transcript. A later
            # sweep can retry after the transient deletion failure clears.
            log.warning(
                "Cron transcript trim deferred run=%s error_type=%s",
                run_id,
                type(exc).__name__,
            )
            continue
        async with get_db_session() as db:
            await db.execute(
                update(CronRun).where(CronRun.id == run_id).values(temp_session_id=None)
            )

    if deleted:
        log.info(f"Trimmed {deleted} cron transcript(s) beyond keep window")


async def _sweep_old_runs() -> None:
    """Delete cron_runs older than RETENTION_DAYS, transcripts first.

    Deleting the run row while its transcript session lives would orphan the
    session forever — nothing else knows it exists.
    """
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox, CronRun
    from sqlalchemy import delete, select

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    from db.models.session import Session as SessionORM

    pending_delivery = select(CronDeliveryOutbox.id).where(
        CronDeliveryOutbox.run_id == CronRun.id,
        CronDeliveryOutbox.state.in_(ACTIVE_OUTBOX_STATES),
    ).exists()

    async with get_db_session() as db:
        result = await db.execute(
            select(CronRun.temp_session_id, SessionORM.user_id)
            .join(SessionORM, SessionORM.id == CronRun.temp_session_id)
            .where(
                CronRun.temp_session_id.isnot(None),
                CronRun.started_at < cutoff,
                ~pending_delivery,
                SessionORM.is_deleted == False,  # noqa: E712
            )
            .distinct()
        )
        stale_sessions = [(sid, uid) for sid, uid in result.all() if sid]

    from session.session import delete_session

    failed_session_ids: set[str] = set()
    for sid, uid in stale_sessions:
        try:
            await delete_session(sid, user_id=uid)
        except Exception as exc:
            failed_session_ids.add(sid)
            log.warning(
                "Cron transcript retention deferred session=%s error_type=%s",
                sid,
                type(exc).__name__,
            )

    async with get_db_session() as db:
        from sqlalchemy import or_

        stale_predicates = [
            CronRun.started_at < cutoff,
            ~select(CronDeliveryOutbox.id).where(
                CronDeliveryOutbox.run_id == CronRun.id,
                CronDeliveryOutbox.state.in_(ACTIVE_OUTBOX_STATES),
            ).exists(),
        ]
        if failed_session_ids:
            stale_predicates.append(or_(
                CronRun.temp_session_id.is_(None),
                ~CronRun.temp_session_id.in_(failed_session_ids),
            ))
        stale_run_ids = select(CronRun.id).where(*stale_predicates)
        await db.execute(
            delete(CronDeliveryOutbox).where(
                CronDeliveryOutbox.run_id.in_(stale_run_ids),
                ~CronDeliveryOutbox.state.in_(ACTIVE_OUTBOX_STATES),
            )
        )
        result = await db.execute(
            delete(CronRun).where(CronRun.id.in_(stale_run_ids))
        )
        if result.rowcount > 0:
            log.info(f"Cleaned up {result.rowcount} old cron run(s)")


async def _sweep_workspace() -> None:
    """Bin directories for deleted projects, and empty stale trash.

    Runs against whichever sandbox is up. Directories the database has never
    heard of are left alone and only logged — an unrecognised directory is far
    more likely to be something worth keeping than something worth deleting.
    """
    from project.reclaim import reclaim

    try:
        from sandbox import sandbox_manager
        client = await sandbox_manager.get_only_client()
    except Exception as e:
        log.debug(f"No sandbox for workspace sweep: {e}")
        return
    if client is None:
        return

    result = await reclaim(client)
    if result.get("binned") or result.get("purged"):
        log.info(
            f"Workspace sweep: binned {len(result['binned'])}, "
            f"purged {len(result['purged'])}"
        )
