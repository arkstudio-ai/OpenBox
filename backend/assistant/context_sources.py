"""Bind replayed assistant evidence to the exact checkpointed provider request."""
from copy import deepcopy

from sqlalchemy import select

from assistant.policy import AssistantError
from assistant.command_sources import command_validation
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from session.agent_event_log import append_agent_event_locked, prepare_agent_event_write

CONTEXT_VERSION = 2
MAX_CONTEXT_SOURCES = 200


@command_validation
async def checked_context_locked(db, main, context, *, fresh=False, snapshot_checks=None):
    from assistant.evidence import validate_business_reads, validate_source_ref
    if (not isinstance(context, dict) or context.get("version") != CONTEXT_VERSION
            or context.get("mode") not in {"ordinary", "report_only", "coordination"}
            or not isinstance(context.get("messages_digest"), str)
            or len(context["messages_digest"]) != 64
            or not isinstance(context.get("source_refs"), list)
            or not isinstance(context.get("business_reads"), list)
            or len(context["source_refs"]) > MAX_CONTEXT_SOURCES):
        raise AssistantError(409, "ASSISTANT_CONTEXT_UNVERIFIED", "The provider context has no complete source projection")
    # A historical projection may reuse independent reads within its snapshot.
    # The provider dispatch checkpoint always rechecks current sources.
    snapshot_checks = None if fresh else snapshot_checks
    validation = {"messages": set(), "refs": {}, "snapshot_checks": snapshot_checks}
    for ref in context["source_refs"]:
        await validate_source_ref(db, ref, user_id=main.user_id, workspace_id=main.workspace_id, main_id=main.id, validation=validation)
    await validate_business_reads(db, context["business_reads"], user_id=main.user_id,
                                  workspace_id=main.workspace_id, main_id=main.id, fresh=fresh,
                                  snapshot_checks=snapshot_checks)
    from assistant.decisions import validate_decision_refs
    await validate_decision_refs(db, main, context.get("decision_refs", []), validation=validation)
    from assistant.task_context import validate_task_snapshots
    if context["mode"] in {"report_only", "coordination"} and context.get("task_snapshots"):
        raise AssistantError(403, "ASSISTANT_REPORT_SCOPE", "Report-only context cannot include other task snapshots")
    await validate_task_snapshots(db, main, context.get("task_snapshots", []), fresh=fresh,
                                  snapshot_checks=snapshot_checks)
    extra = {}
    if context["mode"] == "coordination":
        from assistant.continuation import validate_reference
        await validate_reference(db, main, context.get("continuation_ref"), snapshot_checks=snapshot_checks)
        extra["continuation_ref"] = context["continuation_ref"]
    return deepcopy({**{key: context[key] for key in ("version", "mode", "source_refs", "business_reads", "messages_digest")},
                     "decision_refs": context.get("decision_refs", []), "task_snapshots": context.get("task_snapshots", []), **extra})


async def record_provider_context(ctx, messages):
    """A preflight checkpoint alone does not prove the provider consumed it."""
    from assistant.evidence import projection_digest
    async with get_db_session() as db:
        main = await prepare_agent_event_write(db, session_id=ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        request = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.user_id == ctx.user_id, AgentEvent.run_id == ctx.run_id,
            AgentEvent.generation == ctx.run_generation, AgentEvent.message_id == ctx.message_id,
            AgentEvent.kind == "model.requested").order_by(AgentEvent.sequence.desc()).limit(1))
        context = (request.payload.get("assistant_context") or {}) if request else {}
        if (context.get("version") != CONTEXT_VERSION or context.get("messages_digest") != projection_digest(messages)):
            raise AssistantError(409, "ASSISTANT_CONTEXT_UNVERIFIED", "The provider response does not match its source checkpoint")
        await append_agent_event_locked(db, main, kind="assistant.context.consumed", payload={
            "request_sequence": request.sequence, "request_id": request.payload["request_id"],
        }, run_fence=ctx.run_fence, message_id=ctx.message_id,
            idempotency_key=f"assistant-context:{request.sequence}")


async def consumed_contexts(db, main, message, *, run_fence):
    rows = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
        AgentEvent.user_id == main.user_id, AgentEvent.run_id == run_fence[1], AgentEvent.generation == run_fence[2],
        AgentEvent.kind.in_(("model.requested", "assistant.context.consumed"))).order_by(AgentEvent.sequence).limit(1001))).all())
    requests = {row.sequence: row for row in rows if row.kind == "model.requested"}
    contexts, final_seen = [], message is None
    if len(rows) > 1000:
        return [], False
    for row in rows:
        if row.kind != "assistant.context.consumed":
            continue
        request = requests.get(row.payload.get("request_sequence"))
        context = (request.payload.get("assistant_context") or {}) if request else {}
        if (context.get("version") != CONTEXT_VERSION or request.message_id != row.message_id
                or request.payload.get("request_id") != row.payload.get("request_id") or request.sequence >= row.sequence):
            return [], False
        contexts.append(context)
        final_seen |= message is not None and row.message_id == message.id
    return contexts, final_seen
