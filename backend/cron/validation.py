"""Input validation for cron job creation and update.

Every write path — REST API, the in-chat cron tool, anything future — funnels
through CronService, and CronService funnels through here. The chat tool used
to carry its own caps while the REST API had none, which meant the API could
create a job firing every two seconds against the shared Wuying desktop.

All failures raise ValueError with a user-presentable message (the API maps
ValueError to HTTP 400, the tool prints it verbatim).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from core.log import create_logger
from cron.types import CronJobCreate, CronJobUpdate, CronSchedule

log = create_logger("cron.validation")

MAX_NAME_LENGTH = 256          # cron_jobs.name column width
MAX_DESCRIPTION_LENGTH = 2000


async def validate_create(user_id: str, create: CronJobCreate) -> None:
    """Validate a new job. Raises ValueError on the first violation."""
    from core.config import get_config

    config = get_config()

    _check_name(create.name)
    if len(create.description or "") > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"Description must be under {MAX_DESCRIPTION_LENGTH} characters")
    _check_task_prompt(create.task_prompt, config)
    _check_timeout(create.timeout_seconds, config)
    _check_schedule(create.schedule, config)
    validate_delivery(create.delivery.model_dump() if create.delivery else {})

    await _check_project(user_id, create.project_id)
    if create.session_id:
        await _check_session(user_id, create.session_id)
    await _check_quotas(user_id, create.project_id, config)


async def validate_update(user_id: str, job_id: str, patch: CronJobUpdate) -> None:
    """Validate a job patch. Only the provided fields are checked."""
    from core.config import get_config

    config = get_config()

    if patch.name is not None:
        _check_name(patch.name)
    if patch.description is not None and len(patch.description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"Description must be under {MAX_DESCRIPTION_LENGTH} characters")
    if patch.task_prompt is not None:
        _check_task_prompt(patch.task_prompt, config)
    if patch.timeout_seconds is not None:
        _check_timeout(patch.timeout_seconds, config)
    if patch.schedule is not None:
        _check_schedule(patch.schedule, config)
    if patch.delivery is not None:
        validate_delivery(patch.delivery.model_dump())


async def ensure_not_cron_session(session_id: str) -> None:
    """Refuse scheduling from inside a cron run's temp session.

    A scheduled task's agent gets the same cron tool as any other agent, and
    each run is a fresh session whose per-session quota starts at zero — so a
    looping or prompt-injected task could mint jobs without bound. Jobs are
    created from real conversations only.
    """
    from db.base import get_db_session
    from db.models.cron import CronRun
    from sqlalchemy import select

    async with get_db_session() as db:
        result = await db.execute(
            select(CronRun.id).where(CronRun.temp_session_id == session_id).limit(1)
        )
        if result.scalar_one_or_none() is not None:
            raise ValueError(
                "Scheduled tasks cannot create other scheduled tasks. "
                "Create jobs from a regular conversation instead."
            )


def validate_delivery(delivery: dict | None) -> None:
    """Delivery config must be one we can actually honor."""
    if not delivery:
        return
    mode = delivery.get("mode", "none")
    if mode == "none":
        return
    if mode == "webhook":
        url = delivery.get("webhook_url")
        if not url:
            raise ValueError("Webhook delivery requires webhook_url")
        check_webhook_url(url)
        return
    if mode == "channel":
        raise ValueError("Channel delivery is not implemented yet; use webhook or none")
    raise ValueError(f"Unknown delivery mode: {mode}")


def check_webhook_url(url: str) -> None:
    """Refuse webhook targets inside our own network (SSRF).

    The backend, Redis, Postgres, and the Wuying tunnel all listen on
    loopback/private addresses; a user-supplied webhook must never be able to
    reach them. Hostnames are resolved so DNS names for private IPs are caught
    too. Unresolvable hosts are allowed through — they fail at delivery time
    without touching anything internal.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Webhook URL must use http or https")
    host = parsed.hostname
    if not host:
        raise ValueError("Webhook URL has no host")

    if host.lower() in ("localhost",) or host.lower().endswith(".localhost"):
        raise ValueError("Webhook URL must not target the local machine")

    candidates: list[str] = []
    try:
        candidates.append(str(ipaddress.ip_address(host)))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
            candidates.extend(info[4][0] for info in infos)
        except OSError:
            return  # unresolvable now; delivery will fail harmlessly later

    for addr in candidates:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                "Webhook URL resolves to a private or local address, which is not allowed"
            )


# ---------------------------------------------------------------------------
# Field checks
# ---------------------------------------------------------------------------

def _check_name(name: str) -> None:
    if not name or not name.strip():
        raise ValueError("Job name is required")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"Job name must be under {MAX_NAME_LENGTH} characters")


def _check_task_prompt(task_prompt: str, config) -> None:
    if not task_prompt or not task_prompt.strip():
        raise ValueError("Task prompt is required")
    limit = config.cron_max_task_prompt_length
    if len(task_prompt) > limit:
        raise ValueError(f"Task prompt must be under {limit} characters")


def _check_timeout(timeout_seconds: int, config) -> None:
    lo, hi = config.cron_timeout_seconds_min, config.cron_timeout_seconds_max
    if not (lo <= timeout_seconds <= hi):
        raise ValueError(f"timeout_seconds must be between {lo} and {hi}")


def _check_schedule(schedule: CronSchedule, config) -> None:
    from datetime import datetime, timezone

    from cron.schedule import compute_next_run_at, min_gap_ms

    if schedule.kind == "at":
        now = datetime.now(timezone.utc)
        if compute_next_run_at(schedule, now) is None:
            raise ValueError("One-shot schedule time must be a valid future time")
        return

    if schedule.kind == "cron":
        # Surface a broken expression at creation, not first fire.
        now = datetime.now(timezone.utc)
        if compute_next_run_at(schedule, now) is None:
            raise ValueError(f"Invalid cron expression: '{schedule.expr}'")

    gap = min_gap_ms(schedule)
    min_allowed = config.cron_min_interval_seconds * 1000
    if gap is not None and gap < min_allowed:
        raise ValueError(
            f"Recurring jobs may not fire more often than every "
            f"{config.cron_min_interval_seconds // 60} minutes"
        )


async def _check_project(user_id: str, project_id: str) -> None:
    if not project_id:
        raise ValueError("project_id is required")

    from project.workspace import get_project

    project = await get_project(project_id, user_id)
    if project is None:
        raise ValueError(f"Project {project_id} not found")


async def _check_session(user_id: str, session_id: str) -> None:
    from session.session import get_session

    session = await get_session(session_id, user_id=user_id)
    if session is None:
        raise ValueError(f"Session {session_id} not found")

    await ensure_not_cron_session(session_id)


async def _check_quotas(user_id: str, project_id: str, config) -> None:
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import func, select

    async with get_db_session() as db:
        per_user = await db.execute(
            select(func.count())
            .select_from(CronJob)
            .where(CronJob.user_id == user_id, CronJob.is_deleted == False)  # noqa: E712
        )
        if (per_user.scalar() or 0) >= config.cron_max_jobs_per_user:
            raise ValueError(
                f"Maximum {config.cron_max_jobs_per_user} cron jobs per user. "
                "Remove some first."
            )

        per_project = await db.execute(
            select(func.count())
            .select_from(CronJob)
            .where(
                CronJob.project_id == project_id,
                CronJob.is_deleted == False,  # noqa: E712
            )
        )
        if (per_project.scalar() or 0) >= config.cron_max_jobs_per_project:
            raise ValueError(
                f"Maximum {config.cron_max_jobs_per_project} cron jobs per project. "
                "Remove some first."
            )
