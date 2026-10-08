"""Server-bound, read-only result reports and atomic processing receipts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import select

from assistant.commands import _authority, command_digest
from assistant.policy import AssistantError
from assistant.results import validate_result_source
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult
from db.models.part import Part
from memory.redaction import redact_credentials
from session.agent_event_log import append_agent_event_locked, prepare_agent_event_write

REPORT_TOOLS = frozenset({"tasks.get", "results.read", "history.read"})
ASSISTANT_TOOLS = REPORT_TOOLS | {"projects.list", "sessions.list", "tasks.submit", "tasks.followup", "tasks.list", "decisions.propose", "assets.list", "assets.attach", "schedules.list", "schedules.create", "schedules.update", "schedules.run",
                                "tasks.pause", "tasks.resume", "tasks.cancel", "tasks.link_existing", "tasks.archive", "sessions.rename", "requests.list", "requests.get", "requests.reply", "knowledge.directory", "knowledge.read", "memory.search", "memory.read",
                                "memory.remember", "memory.update", "memory.forget", "assistant.identity",
                                "projects.brief.read", "projects.brief.update", "requests.answer",
                                "projects.create", "projects.delete", "sessions.delete", "tasks.delete",
                                "status.credits", "status.resources", "status.skills", "status.publishing",
                                "status.briefing", "briefing.configure"}
MAX_REPORT_ATTEMPTS = 3
EVIDENCE_PROJECTION_VERSION = "credentials-v1"


@dataclass(frozen=True)
class ReportBinding:
    result: TaskResult
    inbox: AgentInboxItem
    parts: tuple


async def bound_report_locked(db, main, *, run_id: str, generation: int,
                              verify_sources: bool = False, snapshot_checks=None) -> ReportBinding | None:
    if main.kind != "assistant":
        return None
    claimed = list((await db.scalars(select(AgentInboxItem).where(
        AgentInboxItem.session_id == main.id, AgentInboxItem.user_id == main.user_id,
        AgentInboxItem.run_id == run_id, AgentInboxItem.generation == generation,
        AgentInboxItem.state.in_(("claimed", "settled")),
    ))).all())
    reports = [row for row in claimed if row.origin == "task_result"]
    if not reports:
        return None
    if len(reports) != 1 or len(claimed) != 1:
        raise AssistantError(409, "ASSISTANT_REPORT_MIXED_INPUT", "A report turn must bind exactly one result")
    item = reports[0]
    reference = item.origin_ref or {}
    result = await db.scalar(select(TaskResult).where(TaskResult.id == reference.get("result_id")))
    if (reference.get("execution_mode") != "report_only" or result is None
            or result.report_attempt != reference.get("report_attempt")
            or result.assistant_inbox_id != item.id or result.delivery_state != "accepted"
            or result.task_id != reference.get("task_id")):
        raise AssistantError(409, "ASSISTANT_REPORT_STALE", "The report attempt is no longer current")
    parts = []
    if verify_sources:
        # Current ownership and the parts still present (no re-hashing, V2).
        _, parts = await validate_result_source(db, result, user_id=main.user_id,
                                               workspace_id=main.workspace_id, main_id=main.id)
    return ReportBinding(result, item, tuple(parts))


def _text(part: Part) -> str:
    if part.type == "text":
        text = str(part.data.get("text") or "")
    else:
        relation = part.data.get("relation") or {}
        text = json.dumps({"asset_id": part.data.get("asset_id"), "name": relation.get("label"),
                           "mime_type": part.data.get("mime_type")}, ensure_ascii=False)
    # Redaction happens before paging. Receipts refer to this versioned safe
    # projection, never claim that omitted credentials were read by the model.
    return redact_credentials(text)


async def _read_call(db, main, ctx, tool_id: str):
    call = await db.scalar(select(Part).where(Part.id == ctx.part_id, Part.session_id == main.id,
        Part.user_id == main.user_id, Part.message_id == ctx.message_id, Part.type == "tool"))
    if call is None or (call.canonical_tool_id or call.data.get("tool")) != tool_id:
        raise AssistantError(403, "ASSISTANT_READ_UNVERIFIED", "A persisted read call is required")
    if not await db.scalar(select(AgentEvent.id).where(AgentEvent.part_id == call.id,
        AgentEvent.session_id == main.id, AgentEvent.run_id == ctx.run_id,
        AgentEvent.generation == ctx.run_generation).limit(1)):
        raise AssistantError(403, "ASSISTANT_READ_UNVERIFIED", "Read call does not belong to this run")


async def read_report_sources(*, ctx, result_id: str, offset: int = 0,
                               max_chars: int = 8000, source_version: str | None = None) -> dict:
    return await read_result_sources(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
        main_id=ctx.session_id, ctx=ctx, result_id=result_id, offset=offset,
        max_chars=max_chars, source_version=source_version)


async def read_result_sources(*, user_id: str, workspace_id: str, main_id: str, result_id: str,
                              offset: int = 0, max_chars: int = 8000, source_version: str | None = None,
                              ctx=None, record: bool = True, summary: bool = False) -> dict:
    """Read original input and report with server-recorded, bounded coverage.

    Every page verifies current audience and all source hashes. Pagination
    cannot be continued against changed or revoked evidence.
    """
    if type(offset) is not int or offset < 0 or type(max_chars) is not int or not 1 <= max_chars <= 16000:
        raise ValueError("Invalid report page budget")
    async with get_db_session() as db:
        if ctx is not None:
            if (ctx.session_id, ctx.user_id, ctx.workspace_id) != (main_id, user_id, workspace_id):
                raise AssistantError(403, "ASSISTANT_REPORT_SCOPE", "Read context does not match its actor")
            await prepare_agent_event_write(db, session_id=main_id, user_id=user_id, run_fence=ctx.run_fence)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        binding = (await bound_report_locked(db, main, run_id=ctx.run_id, generation=ctx.run_generation,
                                             verify_sources=True)
                   if ctx is not None else None)
        coordination = None
        if binding is None and ctx is not None:
            from assistant.continuation import bound_coordination_locked
            coordination = await bound_coordination_locked(db, main, run_id=ctx.run_id, generation=ctx.run_generation)
            if coordination is not None and coordination.result.id != result_id:
                raise AssistantError(403, "ASSISTANT_REPORT_SCOPE", "Read is outside the bound result")
        if binding is not None and binding.result.id != result_id:
            raise AssistantError(403, "ASSISTANT_REPORT_SCOPE", "Read is outside the bound result")
        verified = binding or coordination
        result = verified.result if verified else await db.get(TaskResult, result_id)
        if result is None:
            raise AssistantError(404, "ASSISTANT_RESULT_UNAVAILABLE", "Result is unavailable")
        if verified is not None:
            # Binding just validated these exact sources in this transaction.
            parts = verified.parts
        else:
            _, parts = await validate_result_source(db, result, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        from assistant.reads import result_view
        if summary:
            return {**result_view(result), "task_id": result.task_id, "untrusted_data": True}
        if ctx is not None and record:
            await _read_call(db, main, ctx, "results.read")
        version = command_digest({"refs": result.output_refs, "projection": EVIDENCE_PROJECTION_VERSION})
        if (offset and source_version != version) or (source_version is not None and source_version != version):
            raise AssistantError(409, "ASSISTANT_REPORT_VERSION", "Reload the result's original sources")
        entries, spans = [], []
        position, remaining = 0, min(max_chars, 5000)
        total = sum(len(_text(part)) for _, part in parts)
        if offset > total:
            raise ValueError("Report offset is beyond its sources")
        for ref, part in parts:
            text = _text(part)
            end = position + len(text)
            if end > offset and remaining and len(entries) < 10:
                start = max(0, offset - position)
                body = text[start:start + remaining]
                entries.append({**ref, "text": body, "offset": start, "total_chars": len(text),
                                "untrusted_data": True, "redacted": part.type == "text" and text != part.data.get("text", "")})
                spans.append({"part_id": part.id, "content_hash": ref["content_hash"],
                              "start": start, "end": start + len(body), "total": len(text)})
                remaining -= len(body)
            position = end
        next_offset = offset + sum(len(row["text"]) for row in entries)
        return {**result_view(result), "result_id": result_id, "task_id": result.task_id, "run_id": result.run_id,
                "generation": result.generation, "result_message_id": result.result_message_id,
                "outcome": result.outcome, "source_version": version, "sources": entries,
                "offset": offset, "next_offset": next_offset if next_offset < total else None,
                "total_chars": total, "truncated": next_offset < total, "untrusted_data": True}


def mark_report_failed(result, *, reason: str, now, blocked: bool = False) -> None:
    """The same retry policy applies to finalization and crash reconciliation."""
    exhausted = result.retry_count >= MAX_REPORT_ATTEMPTS - 1
    result.delivery_state = "blocked" if blocked or exhausted else "retry_wait"
    result.last_error_code = reason if blocked or not exhausted else "retry_exhausted"
    result.available_at = now + timedelta(seconds=min(60, 5 * 2 ** min(result.retry_count, 4)))


async def finalize_report_locked(db, main, message, *, run_fence) -> bool:
    """Called before the canonical final message event, in that transaction.

    A generated claim of success is insufficient: only actual read receipts
    for this exact attempt can produce a processed receipt. Failure also
    settles this attempt, so retries cannot overlap the old generation.
    """
    if main.kind != "assistant" or run_fence is None or message.summary:
        return False
    if message.finish in {None, "tool_calls", "tool-calls", "compact"} and not message.error:
        return False
    already_processed = await db.scalar(select(TaskResult).join(AssistantTask, AssistantTask.id == TaskResult.task_id).where(
        TaskResult.processed_message_id == message.id, TaskResult.delivery_state == "processed",
        AssistantTask.assistant_session_id == main.id, AssistantTask.user_id == main.user_id,
    ).with_for_update(of=TaskResult))
    if already_processed:
        if message.finish != "stop" or message.error:
            raise AssistantError(409, "ASSISTANT_REPORT_IMMUTABLE", "A processed report cannot be changed to a failure")
        return False
    previous_failure = await db.scalar(select(AgentInboxItem).where(
        AgentInboxItem.session_id == main.id, AgentInboxItem.user_id == main.user_id,
        AgentInboxItem.run_id == run_fence[1], AgentInboxItem.generation == run_fence[2],
        AgentInboxItem.origin == "task_result", AgentInboxItem.state == "settled",
        AgentInboxItem.result_message_id == message.id, AgentInboxItem.outcome.in_(("error", "aborted")),
    ))
    if previous_failure:
        # The processor and loop both checkpoint terminal metadata. Repeating
        # that write cannot turn an already failed report back into success.
        message.finish = "aborted" if previous_failure.outcome == "aborted" else "error"
        message.error = previous_failure.error
        return True
    binding = await bound_report_locked(db, main, run_id=run_fence[1], generation=run_fence[2])
    if binding is None:
        return False
    result = await db.scalar(select(TaskResult).where(TaskResult.id == binding.result.id).with_for_update())
    invalid_source = None
    task = await db.scalar(select(AssistantTask.id).where(AssistantTask.id == result.task_id,
        AssistantTask.assistant_session_id == main.id, AssistantTask.user_id == main.user_id))
    if task is None:
        invalid_source = "ASSISTANT_TASK_UNAVAILABLE"
    version = command_digest({"refs": result.output_refs, "projection": EVIDENCE_PROJECTION_VERSION})
    answer_parts = list((await db.scalars(select(Part).where(Part.message_id == message.id,
        Part.session_id == main.id, Part.user_id == main.user_id))).all())
    has_answer = any(p.type == "text" and str(p.data.get("text") or "").strip() for p in answer_parts)
    waiting = any(p.type == "tool" and p.data.get("status") in {"pending", "running", "waiting_input"}
                  for p in answer_parts)
    # V2: a non-empty, finished answer settles the report. The model is not
    # required to page through every source first (PERSONAL_ASSISTANT_DESIGN_V2.md 7.2).
    successful = message.finish == "stop" and not message.error and has_answer and not waiting and not invalid_source
    now = datetime.now(timezone.utc)
    item = binding.inbox
    item.state, item.result_message_id, item.settled_at = "settled", message.id, now
    item.claim_expires_at, item.updated_at = None, now
    if successful:
        result.delivery_state, result.processed_message_id, result.last_error_code = "processed", message.id, None
        item.outcome, item.error = "succeeded", None
        await append_agent_event_locked(db, main, kind="assistant.result.processed", payload={
            "result_id": result.id, "task_id": result.task_id, "report_attempt": result.report_attempt,
            "inbox_id": item.id, "processed_message_id": message.id,
            "original_report_message_id": result.result_message_id, "source_version": version,
        }, run_fence=run_fence, message_id=message.id, idempotency_key=f"result-processed:{result.id}")
    else:
        # An aborted provider or a cooperative server shutdown is not human
        # intent. Explicit user stops already settle this exact Inbox/Result
        # atomically in stop_report_locked, before revoking its runtime fence.
        aborted = message.finish == "aborted"
        reason = invalid_source or ("report_interrupted" if aborted else "report_failed")
        mark_report_failed(result, reason=reason, now=now, blocked=bool(invalid_source))
        if message.finish == "stop":
            message.finish = "error"
            message.error = {"name": "AssistantReportError", "code": reason,
                             "message": "The execution result is saved, but this report did not finish."}
        item.outcome, item.error = "aborted" if aborted else "error", message.error
        await append_agent_event_locked(db, main, kind="assistant.report.failed", payload={
            "result_id": result.id, "report_attempt": result.report_attempt,
            "inbox_id": item.id, "reason": result.last_error_code, "state": result.delivery_state,
        }, run_fence=run_fence, message_id=message.id,
            idempotency_key=f"report-failed:{result.id}:{result.report_attempt}")
    await append_agent_event_locked(db, main, kind="inbox.settled", payload={
        "item_id": item.id, "state": item.state, "outcome": item.outcome,
        "result_message_id": message.id, "error": item.error,
    }, run_fence=run_fence, turn_id=item.turn_id, step_id=item.step_id, message_id=item.message_id,
        idempotency_key=f"inbox:{item.id}:settled:{run_fence[1]}:{run_fence[2]}")
    return True
