"""Durable navigation notes backed by authenticated human input, never approvals."""
from datetime import datetime, timezone

from sqlalchemy import select

from assistant.commands import _authority, command_digest, task_locked
from assistant.policy import AssistantError
from core.identifier import generate_id
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.message import Message
from db.models.part import Part
from memory.redaction import redact_credentials
from session.agent_event_log import append_agent_event_locked, prepare_agent_event_write

# The one assistant conversation lasts for good, so decision notes accumulate.
# Only the newest records are read, and only the newest effective notes ride
# in each request; older ones remain in history (V2 8.6).
MAX_DECISION_EVENTS = 2000
MAX_CONTEXT_DECISIONS = 40
PROPOSED = "assistant.decision.proposed"
RECORDED = "assistant.decision.recorded"


async def _human_sources(db, main, refs, *, validation=None, depth=0):
    from assistant.evidence import validate_source_ref
    if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
        raise AssistantError(400, "ASSISTANT_DECISION_SOURCE", "A decision needs original human evidence")
    parts = []
    audience = {"visibility": "private", "user_id": main.user_id, "workspace_id": main.workspace_id}
    for ref in refs:
        part = await validate_source_ref(db, ref, user_id=main.user_id,
            workspace_id=main.workspace_id, main_id=main.id, validation=validation, depth=depth)
        message = await db.get(Message, part.message_id)
        item = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.session_id == part.session_id,
            AgentInboxItem.user_id == main.user_id, AgentInboxItem.message_id == part.message_id,
            AgentInboxItem.origin == "human"))
        origin = part.data.get("origin_ref") or {}
        quote = ref.get("quote")
        if (ref.get("audience", audience) != audience or part.type != "text" or message.role != "user"
                or part.data.get("origin") != "human" or part.data.get("ignored")
                or item is None or origin.get("actor_user_id") != main.user_id
                or origin.get("inbox_id") != item.id or part.data.get("text") != item.prompt
                or not isinstance(quote, str) or not quote.strip() or len(quote) > 4000
                or quote not in redact_credentials(item.prompt)):
            raise AssistantError(403, "ASSISTANT_DECISION_SOURCE", "A decision must quote authenticated human input")
        parts.append(part)
    return parts


async def _records(db, main):
    """The newest recorded notes, oldest first."""
    rows = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
        AgentEvent.user_id == main.user_id, AgentEvent.kind == RECORDED)
        .order_by(AgentEvent.sequence.desc()).limit(MAX_DECISION_EVENTS))).all())
    rows.reverse()
    return rows


def _effective(rows):
    # Revoking a correction cannot silently resurrect its superseded approval.
    superseded = {identity for row in rows for identity in row.payload.get("supersedes", [])}
    return {row.payload["decision_id"]: row for row in rows if row.payload["decision_id"] not in superseded}


async def _validate_original_scope(db, main, payload, *, validation=None, depth=0):
    task_id = payload.get("task_id")
    if task_id:
        await task_locked(db, user_id=main.user_id, workspace_id=main.workspace_id,
                          main_id=main.id, task_id=task_id)
    if payload.get("audience") != {"visibility": "private", "user_id": main.user_id, "workspace_id": main.workspace_id}:
        raise AssistantError(403, "ASSISTANT_DECISION_SCOPE", "Decision audience is unavailable")
    return await _human_sources(db, main, payload["source_refs"], validation=validation, depth=depth)


async def _validate_scope(db, main, payload, *, validation=None, depth=0):
    """A note is checked once, against the user's own quoted words (V2)."""
    return await _validate_original_scope(db, main, payload, validation=validation, depth=depth)


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def _validate_replacement(db, main, payload, *, records=None):
    parts = await _validate_scope(db, main, payload)
    effective = _effective(await _records(db, main) if records is None else records)
    for identity in payload["supersedes"]:
        old = effective.get(identity)
        if old is None or old.payload.get("task_id") != payload.get("task_id"):
            raise AssistantError(409, "ASSISTANT_DECISION_CONFLICT", "A replaced decision must be current and in the same task scope")
        # A newer explicit correction may replace an unavailable derived note
        # when its original human evidence and scope still verify. This does
        # not revive or certify that note's unavailable derivation.
        old_parts = await _validate_original_scope(db, main, old.payload)
        if max(_utc(part.created_at) for part in parts) <= max(_utc(part.created_at) for part in old_parts):
            raise AssistantError(409, "ASSISTANT_DECISION_CONFLICT", "A correction needs newer original human evidence")


async def propose_decision(*, ctx, summary, source_refs, task_id=None, supersedes=()):
    """The pending receipt is not a committed decision or new permission."""
    from assistant.reporting import _read_call, bound_report_locked
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 1000 or len(supersedes) > 8:
        raise AssistantError(400, "ASSISTANT_DECISION_INVALID", "Invalid decision note")
    async with get_db_session() as db:
        main = await prepare_agent_event_write(db, session_id=ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        if await bound_report_locked(db, main, run_id=ctx.run_id, generation=ctx.run_generation):
            raise AssistantError(403, "ASSISTANT_REPORT_READ_ONLY", "Report-only turns cannot record decisions")
        await _read_call(db, main, ctx, "decisions.propose")
        audience = {"visibility": "private", "user_id": main.user_id, "workspace_id": main.workspace_id}
        payload = {"summary": redact_credentials(summary.strip()), "task_id": task_id,
            "source_refs": [{**ref, "origin": "human", "audience": audience} for ref in source_refs],
            "supersedes": list(dict.fromkeys(supersedes)),
            "audience": audience}
        # Do not let a replay of one persisted call propose different text.
        digest = command_digest(payload)
        old = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.user_id == main.user_id, AgentEvent.part_id == ctx.part_id, AgentEvent.kind == PROPOSED))
        if old:
            if old.payload.get("proposal_digest") != digest:
                raise AssistantError(409, "ASSISTANT_DECISION_CONFLICT", "Decision call was used for different input")
            await _validate_scope(db, main, old.payload)
            return {"decision_id": old.payload["decision_id"], "state": "pending_answer_commit", "grants_authority": False}
        call = await db.get(Part, ctx.part_id)
        if call.data.get("status") not in {"pending", "running"}:
            raise AssistantError(403, "ASSISTANT_CALL_UNVERIFIED", "A running decision call is required")
        await _validate_replacement(db, main, payload)
        pending = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.user_id == main.user_id, AgentEvent.kind == PROPOSED,
            AgentEvent.run_id == ctx.run_id, AgentEvent.generation == ctx.run_generation).limit(201))).all())
        if len(pending) >= 200:
            raise AssistantError(409, "ASSISTANT_CONTEXT_BUDGET", "Too many pending decision notes")
        if any(set(row.payload["supersedes"]) & set(payload["supersedes"]) for row in pending):
            raise AssistantError(409, "ASSISTANT_DECISION_CONFLICT", "This decision already has a pending correction in the current turn")
        payload.update(decision_id=generate_id(), proposal_digest=digest,
                       created_at=datetime.now(timezone.utc).isoformat())
        await append_agent_event_locked(db, main, kind=PROPOSED, payload=payload, run_fence=ctx.run_fence,
            message_id=ctx.message_id, part_id=ctx.part_id, idempotency_key=f"assistant-decision-proposal:{ctx.part_id}")
        return {"decision_id": payload["decision_id"], "state": "pending_answer_commit", "grants_authority": False}


async def decision_context(db, main, *, run_fence):
    """Effective notes and this run's drafts, read from SQL without re-validation."""
    recorded = await _records(db, main)
    committed = {row.payload["decision_id"] for row in recorded}
    effective = list(_effective(recorded).values())
    rows = effective[-MAX_CONTEXT_DECISIONS:]
    proposals = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
        AgentEvent.user_id == main.user_id, AgentEvent.kind == PROPOSED,
        AgentEvent.run_id == run_fence[1], AgentEvent.generation == run_fence[2])
        .order_by(AgentEvent.sequence).limit(201))).all())
    proposals = [row for row in proposals if row.payload["decision_id"] not in committed]
    entries = []
    for row in rows + proposals:
        entry = {key: row.payload.get(key) for key in ("decision_id", "task_id", "summary", "supersedes", "created_at")}
        entry["state"] = "pending_answer_commit" if row.kind == PROPOSED else "effective"
        entries.append(entry)
    context = {"decisions": entries, "untrusted_data": True, "grants_authority": False}
    if len(effective) > len(rows):
        context["older_notes"] = len(effective) - len(rows)
    return context


async def record_decisions_locked(db, main, message, *, run_fence):
    """Commit proposals with the verified successful ordinary answer, atomically."""
    if message.finish != "stop" or message.error or message.summary:
        return
    # Report and coordination turns never record decisions. Read the run's own
    # inputs directly: finalization may already have settled its report.
    inputs = list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == main.id,
        AgentInboxItem.user_id == main.user_id, AgentInboxItem.run_id == run_fence[1],
        AgentInboxItem.generation == run_fence[2]))).all())
    if any(item.origin == "task_result" or (item.origin_ref or {}).get("execution_mode") in {"report_only", "coordination"}
           for item in inputs):
        return
    proposals = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
        AgentEvent.user_id == main.user_id, AgentEvent.kind == PROPOSED,
        AgentEvent.run_id == run_fence[1], AgentEvent.generation == run_fence[2])
        .order_by(AgentEvent.sequence).limit(201))).all())
    if not proposals:
        return
    answer_parts = list((await db.scalars(select(Part).where(Part.message_id == message.id,
        Part.user_id == main.user_id, Part.session_id == main.id))).all())
    if (not any(part.type == "text" and str(part.data.get("text") or "").strip() for part in answer_parts)
            or any(part.type == "tool" and part.data.get("status") in {"pending", "running", "waiting_input"} for part in answer_parts)):
        raise AssistantError(409, "ASSISTANT_DECISION_UNVERIFIED", "A completed non-empty answer is required to commit decisions")
    if len(proposals) > 200:
        raise AssistantError(409, "ASSISTANT_CONTEXT_BUDGET", "Too many proposed decisions")
    recorded = await _records(db, main)
    identities = {row.payload["decision_id"] for row in recorded}
    for proposal in proposals:
        if proposal.payload["decision_id"] in identities:
            continue
        await _validate_replacement(db, main, proposal.payload, records=recorded)
        row = await append_agent_event_locked(db, main, kind=RECORDED, payload={**proposal.payload,
            "created_at": datetime.now(timezone.utc).isoformat(), "proposal_sequence": proposal.sequence,
            "committed_message_id": message.id}, run_fence=run_fence, message_id=message.id,
            idempotency_key=f"assistant-decision:{proposal.payload['decision_id']}")
        recorded.append(row)
        identities.add(proposal.payload["decision_id"])
