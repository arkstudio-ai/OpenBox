"""Team attribution and lifecycle around the existing account credit ledger."""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import JSON, and_, func, select, type_coerce

from db.base import get_db_session
from db.models.billing import UsageEvent
from team.commands import run_status
from team.errors import TeamExecutionPaused
from team.journal import Actor, command, owned_run


def live_model_request():
    """SQL predicate for usage attribution and the attention read model."""
    from db.models.agent_driver import AgentDriverState
    # Pending is covered by live-request headroom only while its exact Driver
    # owns a lease. An idle/expired/replaced Driver cannot make unknown provider
    # cost disappear. Legacy pending rows without a fence also fail closed.
    fence = type_coerce(UsageEvent.pricing, JSON)["request_fence"]
    return and_(UsageEvent.status == "pending", select(AgentDriverState.session_id).where(
        AgentDriverState.session_id == UsageEvent.session_id,
        AgentDriverState.user_id == UsageEvent.user_id,
        AgentDriverState.phase != "idle",
        AgentDriverState.lease_expires_at > func.current_timestamp(),
        AgentDriverState.run_id == fence["run_id"].as_string(),
        AgentDriverState.generation == fence["generation"].as_integer(),
    ).exists())


async def model_usage(db, run, member_ids) -> tuple[Decimal, int]:
    query = select(func.coalesce(func.sum(UsageEvent.credits), 0),
        func.count().filter(UsageEvent.credits.is_(None), ~live_model_request())).where(
        UsageEvent.user_id == run.owner_user_id, UsageEvent.workspace_id == run.workspace_id,
        UsageEvent.session_id.in_(member_ids), UsageEvent.created_at >= run.created_at,
        UsageEvent.kind.in_(["chat", "compaction", "title", "suggestions", "team", "bash_judge"]))
    spent, unpriced = (await db.execute(query)).one()
    return Decimal(spent), int(unpriced)


async def before_model_call(ctx, *, model_id: str, output_tokens: int) -> None:
    """Team lifecycle fence only; UsageMeter owns account credit admission."""
    from team.runtime_binding import current_binding
    binding = current_binding()
    if binding is None or binding.run_id is None or ctx.session_id != binding.member_id:
        return
    async with get_db_session() as db:
        run = await owned_run(db, binding.run_id, Actor(binding.owner_user_id, binding.workspace_id, "server"))
        if run.state not in {"running", "waiting"}:
            raise TeamExecutionPaused("TEAM_PAUSED", "This run is closed to new model requests.")


async def handle_billing_error(ctx, error) -> None:
    """Persist a recoverable team pause for the ordinary account billing gate.

    Do not consume coordinator failure retries or misreport exhausted account
    credits as a provider failure. Other accounts and runs keep their own gate.
    """
    from team.runtime_binding import current_binding
    binding = current_binding()
    reasons = {"INSUFFICIENT_CREDITS": "insufficient_credits", "MODEL_UNPRICED": "model_unpriced"}
    if binding is None or not binding.run_id or ctx.session_id != binding.member_id or error.code not in reasons:
        return
    actor = Actor(binding.owner_user_id, binding.workspace_id, "server")
    async def pause(writer):
        if writer.state["run"]["state"] in {"running", "waiting"}:
            run_status(writer, "pausing", pause_reason=reasons[error.code])
        return {"paused": True}
    await command(binding.run_id, actor, f"billing:{ctx.message_id}:{error.code}", {"code": error.code}, pause)
    from team.scheduler import schedule
    schedule(binding.run_id, actor)
    raise TeamExecutionPaused(error.code, str(error)) from error
