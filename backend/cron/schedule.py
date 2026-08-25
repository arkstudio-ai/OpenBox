"""Schedule computation — compute next/previous run times for cron jobs.

Uses `croniter` for cron expression parsing (Python equivalent of OpenClaw's `croner`).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from core.log import create_logger

if TYPE_CHECKING:
    from cron.types import CronSchedule

log = create_logger("cron.schedule")


def compute_next_run_at(schedule: CronSchedule, after: datetime) -> datetime | None:
    """Compute the next run time after the given datetime.

    Returns None if no next run is possible (e.g., one-shot in the past).
    """
    if schedule.kind == "at":
        at = _parse_iso(schedule.at)
        if at is None:
            return None
        return at if at > after else None

    if schedule.kind == "every":
        every_ms = max(1, schedule.every_ms)
        every_td = timedelta(milliseconds=every_ms)
        anchor = datetime.fromtimestamp(
            (schedule.anchor_ms or 0) / 1000, tz=timezone.utc
        ) if schedule.anchor_ms else after

        if after < anchor:
            return anchor

        elapsed = (after - anchor).total_seconds() * 1000
        steps = max(1, int((elapsed + every_ms - 1) // every_ms))
        return anchor + timedelta(milliseconds=steps * every_ms)

    if schedule.kind == "cron":
        try:
            from croniter import croniter
            import zoneinfo

            tz = zoneinfo.ZoneInfo(schedule.tz) if schedule.tz else timezone.utc
            # croniter works with local times in the given tz
            after_local = after.astimezone(tz)
            cron = croniter(schedule.expr, after_local)
            next_dt = cron.get_next(datetime)
            # Convert back to UTC
            return next_dt.astimezone(timezone.utc)
        except Exception as e:
            log.error(f"Failed to compute next cron run: {e}")
            return None

    return None


def compute_previous_run_at(schedule: CronSchedule, before: datetime) -> datetime | None:
    """Compute the most recent run time before the given datetime.

    Used for missed job detection on startup.
    """
    if schedule.kind == "at":
        at = _parse_iso(schedule.at)
        if at is None:
            return None
        return at if at < before else None

    if schedule.kind == "every":
        every_ms = max(1, schedule.every_ms)
        anchor = datetime.fromtimestamp(
            (schedule.anchor_ms or 0) / 1000, tz=timezone.utc
        ) if schedule.anchor_ms else before

        if before <= anchor:
            return None

        elapsed = (before - anchor).total_seconds() * 1000
        steps = int(elapsed // every_ms)
        return anchor + timedelta(milliseconds=steps * every_ms)

    if schedule.kind == "cron":
        try:
            from croniter import croniter
            import zoneinfo

            tz = zoneinfo.ZoneInfo(schedule.tz) if schedule.tz else timezone.utc
            before_local = before.astimezone(tz)
            cron = croniter(schedule.expr, before_local)
            prev_dt = cron.get_prev(datetime)
            return prev_dt.astimezone(timezone.utc)
        except Exception as e:
            log.error(f"Failed to compute previous cron run: {e}")
            return None

    return None


def compute_job_interval_ms(schedule: CronSchedule) -> int | None:
    """Estimate the average interval between runs in milliseconds.

    Used for container keepalive decisions.
    Returns None if interval cannot be determined (one-shot jobs).
    """
    if schedule.kind == "at":
        return None

    if schedule.kind == "every":
        return max(1, schedule.every_ms)

    if schedule.kind == "cron":
        try:
            from croniter import croniter
            now = datetime.now(timezone.utc)
            cron = croniter(schedule.expr, now)
            t1 = cron.get_next(datetime)
            t2 = cron.get_next(datetime)
            return int((t2 - t1).total_seconds() * 1000)
        except Exception:
            return None

    return None


def as_aware_utc(dt: datetime | None) -> datetime | None:
    """Normalize a DB-read datetime to aware UTC.

    The cron columns are TIMESTAMP WITHOUT TIME ZONE, so both asyncpg and
    sqlite hand back naive values even though every write is UTC-aware;
    comparing one against datetime.now(timezone.utc) raises TypeError.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def schedule_from_dict(sched: dict | None) -> "CronSchedule | None":
    """Rebuild a schedule object from its stored JSON dict."""
    from cron.types import CronScheduleAt, CronScheduleCron, CronScheduleEvery

    if not isinstance(sched, dict):
        return None
    kind = sched.get("kind")
    try:
        if kind == "cron":
            return CronScheduleCron(expr=sched["expr"], tz=sched.get("tz", "UTC"))
        if kind == "every":
            return CronScheduleEvery(
                every_ms=sched["every_ms"], anchor_ms=sched.get("anchor_ms")
            )
        if kind == "at":
            return CronScheduleAt(at=sched["at"])
    except (KeyError, ValueError):
        return None
    return None


# Top-of-hour cron jobs ("0 9 * * *", "0 * * * *") all fire in the same
# second across every user, which stampedes the shared Wuying desktop and its
# warmup path. A deterministic per-job offset inside this window spreads them
# out; "every" schedules are naturally spread by their creation-time anchor.
STAGGER_WINDOW_MS = 5 * 60 * 1000


def min_gap_ms(schedule: CronSchedule) -> int | None:
    """Smallest gap between two consecutive fires, for rate limiting.

    Unlike compute_job_interval_ms this takes the MINIMUM over a few fires:
    "0,1 * * * *" averages 30 minutes but actually fires 60s apart.
    Returns None for one-shot schedules.
    """
    if schedule.kind == "at":
        return None

    if schedule.kind == "every":
        return max(1, schedule.every_ms)

    if schedule.kind == "cron":
        try:
            from croniter import croniter
            import zoneinfo

            tz = zoneinfo.ZoneInfo(schedule.tz) if schedule.tz else timezone.utc
            cron = croniter(schedule.expr, datetime.now(timezone.utc).astimezone(tz))
            fires = [cron.get_next(datetime) for _ in range(4)]
            gaps = [
                (b - a).total_seconds() * 1000
                for a, b in zip(fires, fires[1:])
            ]
            return int(min(gaps)) if gaps else None
        except Exception:
            return None

    return None


def stagger_ms_for(job_id: str) -> int:
    """Deterministic per-job offset in [0, STAGGER_WINDOW_MS)."""
    import hashlib

    digest = hashlib.md5(job_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % STAGGER_WINDOW_MS


def apply_stagger(
    next_run: datetime | None, schedule: CronSchedule, job_id: str
) -> datetime | None:
    """Shift a top-of-hour cron fire by the job's deterministic offset.

    Only cron expressions whose minute field is exactly "0" are shifted —
    those are the ones that pile up. One-shots and intervals pass through.
    """
    if next_run is None or schedule.kind != "cron":
        return next_run
    fields = schedule.expr.split()
    if not fields or fields[0] != "0":
        return next_run
    return next_run + timedelta(milliseconds=stagger_ms_for(job_id))


def _parse_iso(s: str) -> datetime | None:
    """Parse an ISO 8601 datetime string to UTC datetime."""
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None
