"""Execution limits shared by frozen Agent definitions and model adapters."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps
from inspect import signature
from weakref import WeakValueDictionary

from team.errors import TeamError, TeamExecutionPaused


_model_slots: WeakValueDictionary = WeakValueDictionary()
_execution_window: ContextVar[tuple | None] = ContextVar("team_execution_window", default=None)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def expired_members(db, state, drivers, now) -> set[str]:
    """Recovery also enforces time while a member has yielded its Driver."""
    from sqlalchemy import select
    from db.models.team import TeamEvent
    active = {attempt["member_id"]: _aware(datetime.fromisoformat(attempt["started_at"]))
        for attempt in state["attempts"].values() if attempt["state"] == "running"}
    for member_id, driver in drivers.items():
        if driver.phase != "idle" and driver.started_at and driver.lease_expires_at and _aware(driver.lease_expires_at) > now:
            active.setdefault(member_id, _aware(driver.started_at))
    if not active:
        return set()
    rows = (await db.scalars(select(TeamEvent).where(TeamEvent.team_run_id == state["id"],
        TeamEvent.kind == "team.member.admitted", TeamEvent.entity_id.in_(active)))).all()
    return {row.entity_id for row in rows if
        now >= active[row.entity_id] + timedelta(seconds=row.payload["data"].get("spec", {})
            .get("execution_policy", {}).get("max_wall_time_seconds", 1800))}


async def start_deadline_watch(lease):
    """Bound one work attempt (including waits), or one coordinator/trial turn.

    Task start times are durable, so cold wakes cannot reset an attempt's
    allowance. Expiry uses normal interruption/reconciliation: unknown paid
    work retains its reservation and is never retried automatically.
    """
    from team.runtime_binding import current_binding
    from team.journal import Actor, command, read_state, utcnow
    from db.base import get_db_session
    from db.models.agent_driver import AgentDriverState
    from db.models.team import TeamRun
    binding = current_binding()
    _execution_window.set(None)
    if binding is None:
        return None
    async with get_db_session() as db:
        driver = await db.get(AgentDriverState, binding.member_id)
        started_at = _aware(driver.started_at) if driver and driver.started_at else utcnow()
        if binding.run_id:
            run = await db.get(TeamRun, binding.run_id)
            state = await read_state(db, run)
            attempt = state["attempts"].get(state["members"][binding.member_id]["current_attempt"])
            if attempt and attempt["state"] == "running":
                started_at = _aware(datetime.fromisoformat(attempt["started_at"]))
    cap = binding.spec.get("execution_policy", {}).get("max_wall_time_seconds", 1800)
    deadline = started_at + timedelta(seconds=cap)

    async def expire():
        await lease.assert_current()
        lease.abort.set()
        if binding.run_id:
            from team.commands import member_status, run_status
            actor = Actor(binding.owner_user_id, binding.workspace_id, "server")
            key = f"agent-deadline:{binding.member_id}:{lease.generation}"
            async def pause(writer):
                if writer.state["run"]["state"] not in {"running", "waiting"}:
                    return {"paused": False}
                writer.append("team.notice", "notice", {"id": key, "code": "AGENT_TIME_LIMIT",
                    "member_id": binding.member_id, "message": "The Agent execution time limit was reached; review the interrupted task before retrying."})
                member_status(writer, binding.member_id, execution_state="stopped", interrupt_requested=True,
                    wait_after_seq=None, wait_deadline=None)
                run_status(writer, "pausing", pause_reason="agent_wall_time_exceeded")
                return {"paused": True}
            await command(binding.run_id, actor, key, {}, pause)
            from team.scheduler import schedule
            schedule(binding.run_id, actor)
        else:
            from bus import bus
            from bus.events import SESSION_ERROR
            bus.publish(SESSION_ERROR, {"userId": binding.owner_user_id, "sessionId": binding.member_id,
                "error": {"code": "AGENT_TIME_LIMIT", "message": "The Agent execution time limit was reached."}})

    _execution_window.set((binding.member_id, deadline, expire))
    async def watch():
        await asyncio.sleep(max(0, (deadline - utcnow()).total_seconds()))
        from agent.driver import LeaseLostError
        try:
            await expire()
        except LeaseLostError:
            return  # A newer Driver owns this session; it computes its own fence.
    return asyncio.create_task(watch(), name=f"agent-deadline:{binding.member_id}")


async def assert_execution_time(ctx):
    from team.journal import utcnow
    window = _execution_window.get()
    if window and window[0] == ctx.session_id and utcnow() >= window[1]:
        await window[2]()
        from team.runtime_binding import current_binding
        binding = current_binding()
        error_type = TeamExecutionPaused if binding and binding.run_id else TeamError
        raise error_type("AGENT_TIME_LIMIT", "The Agent execution time limit was reached. Review the interrupted task before retrying.")


@asynccontextmanager
async def model_request_slot(ctx):
    """One unsettled model request per bound Driver, including auxiliary calls.

    Title, compaction and tool completions share a settlement boundary with
    streaming requests, preserving one exact live billing identity.
    Durable Driver fencing remains the cross-process owner; the weak map does
    not retain completed sessions or locks from a closed event loop.
    """
    from team.runtime_binding import current_binding
    binding = current_binding()
    if binding is None or binding.member_id != ctx.session_id:
        yield
        return
    key = (asyncio.get_running_loop(), binding.member_id)
    lock = _model_slots.setdefault(key, asyncio.Lock())
    async with lock:
        await assert_execution_time(ctx)
        yield


def serialized_stream(function):
    parameters = signature(function)

    @wraps(function)
    async def wrapped(*args, **kwargs):
        ctx = parameters.bind(*args, **kwargs).arguments["ctx"]
        async with model_request_slot(ctx):
            stream = function(*args, **kwargs)
            try:
                async for event in stream:
                    yield event
            finally:
                await stream.aclose()
    return wrapped


def serialized_completion(function):
    @wraps(function)
    async def wrapped(*, ctx, **kwargs):
        async with model_request_slot(ctx):
            return await function(ctx=ctx, **kwargs)
    return wrapped


def output_limit(spec: dict, model_id: str, variant: str | None, requested: int | None) -> int | None:
    cap = spec.get("generation_options", {}).get("max_output_tokens")
    if cap is None:
        return requested
    from agent.llm import request_output_tokens
    effective = min(cap, requested or request_output_tokens(model_id, variant))
    if request_output_tokens(model_id, variant, effective) > cap:
        raise TeamError("CAPABILITY_UNSUPPORTED",
            "max_output_tokens is smaller than this model's reasoning allowance. Increase the cap or select a supported lower reasoning setting.",
            current={"model": model_id, "max_output_tokens": cap}, status=422)
    return effective


def current_output_limit(ctx, model_id: str, variant: str | None, requested: int | None) -> int | None:
    from team.runtime_binding import current_binding
    binding = current_binding()
    if binding is None or ctx.session_id != binding.member_id:
        return requested
    return output_limit(binding.spec, model_id, variant, requested)
