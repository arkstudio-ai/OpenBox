"""Quota enforcement for multi-user mode.

Each check function raises HTTPException(429) if the user has exceeded
their quota.  The *config* parameter is an ``OpenBoxConfig`` instance
that carries the per-user limits.

Every refusal carries a machine-readable ``code`` and the two numbers behind
it. All three quotas used to answer with a bare 429 and prose, so the browser
could not tell "you have too many conversations" from "too many agents are
running" — and neither of those from a model failing. The code picks the copy;
the numbers let it say how far over the line the person is, which is the part
that tells them what to do.
"""
from __future__ import annotations

from fastapi import HTTPException

from core.log import create_logger

log = create_logger("auth.quota")


def _quota_error(code: str, message: str, used: int, limit: int) -> HTTPException:
    """A 429 the frontend can map to specific copy rather than a generic one."""
    return HTTPException(
        status_code=429,
        detail={"code": code, "message": message, "used": used, "limit": limit},
        headers={"X-Error-Code": code},
    )


async def check_container_quota(user_id: str, config) -> None:
    """Raise 429 if the user already owns ``max_containers_per_user`` containers."""
    from db.repository.container_repo import PgContainerRepo

    repo = PgContainerRepo()
    count = await repo.count_by_user(user_id)
    limit = config.max_containers_per_user

    if count >= limit:
        log.warning(f"User {user_id} hit container quota ({count}/{limit})")
        raise _quota_error(
            "CONTAINER_QUOTA_EXCEEDED",
            f"Container quota exceeded: {count}/{limit} containers in use.",
            count, limit,
        )


async def check_session_quota(user_id: str, config) -> None:
    """Raise 429 if the user already owns ``max_sessions_per_user`` sessions."""
    from db.repository.session_repo import PgSessionRepo

    repo = PgSessionRepo()
    count = await repo.count_by_user(user_id)
    limit = config.max_sessions_per_user

    if count >= limit:
        log.warning(f"User {user_id} hit session quota ({count}/{limit})")
        raise _quota_error(
            "SESSION_QUOTA_EXCEEDED",
            f"Session quota exceeded: {count}/{limit} sessions.",
            count, limit,
        )


async def check_concurrent_agents(user_id: str, config) -> None:
    """Raise 429 if the user already has ``max_concurrent_agents`` busy sessions."""
    from db.repository.session_repo import PgSessionRepo

    repo = PgSessionRepo()
    busy = await repo.count_busy(user_id)
    limit = config.max_concurrent_agents

    if busy >= limit:
        log.warning(f"User {user_id} hit concurrent agent quota ({busy}/{limit})")
        raise _quota_error(
            "CONCURRENT_AGENT_QUOTA_EXCEEDED",
            f"Concurrent agent quota exceeded: {busy}/{limit} agents running.",
            busy, limit,
        )


async def check_monthly_cost(user_id: str, config) -> None:
    """Raise 429 if the user has exceeded ``monthly_cost_limit`` this month."""
    from db.repository.message_repo import PgMessageRepo

    repo = PgMessageRepo()
    cost = await repo.sum_cost_this_month(user_id)
    limit = config.monthly_cost_limit

    if cost >= limit:
        log.warning(f"User {user_id} hit monthly cost limit (${cost:.2f}/${limit:.2f})")
        raise HTTPException(
            status_code=429,
            detail=f"Monthly cost limit exceeded: ${cost:.2f}/${limit:.2f}.",
        )
