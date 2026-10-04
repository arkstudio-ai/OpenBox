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
ASSISTANT_TOOLS = REPORT_TOOLS | {"projects.list", "sessions.list", "tasks.submit", "tasks.followup", "tasks.list", "decisions.propose", "assets.list", "assets.attach",
                                "tasks.pause", "tasks.resume", "tasks.cancel", "tasks.link_existing", "requests.list", "requests.get", "requests.reply"}
MAX_REPORT_ATTEMPTS = 3
EVIDENCE_PROJECTION_VERSION = "credentials-v1"


@dataclass(frozen=True)
class ReportBinding:
    result: TaskResult
    inbox: AgentInboxItem
    parts: tuple


async def bound_report_locked(db, main, *, run_id: str, generation: int,
                              verify_sources: bool = True) -> ReportBinding | None:
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
        binding = (await bound_report_locked(db, main, run_id=ctx.run_id, generation=ctx.run_generation)
                   if ctx is not None else None)
        if binding is not None and binding.result.id != result_id:
            raise AssistantError(403, "ASSISTANT_REPORT_SCOPE", "Read is outside the bound result")
        result = binding.result if binding else await db.get(TaskResult, result_id)
        if result is None:
            raise AssistantError(404, "ASSISTANT_RESULT_UNAVAILABLE", "Result is unavailable")
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
        if ctx is not None and record:
            await append_agent_event_locked(db, main,
                kind="assistant.report.sources_read" if binding else "assistant.result.sources_read", payload={
                    "result_id": result_id, "report_attempt": result.report_attempt,
                    "inbox_id": binding.inbox.id if binding else None, "source_version": version, "spans": spans,
                    "source_refs": [{k: v for k, v in row.items() if k in {"session_id", "message_id", "part_id", "content_hash"}}
                                    for row in entries],
                }, run_fence=ctx.run_fence, message_id=ctx.message_id,
                part_id=ctx.part_id, idempotency_key=f"report-read:{ctx.part_id}:{offset}:{max_chars}:{version}")
        next_offset = offset + sum(len(row["text"]) for row in entries)
        return {**result_view(result), "result_id": result_id, "task_id": result.task_id, "run_id": result.run_id,
                "generation": result.generation, "result_message_id": result.result_message_id,
                "outcome": result.outcome, "source_version": version, "sources": entries,
                "offset": offset, "next_offset": next_offset if next_offset < total else None,
                "total_chars": total, "truncated": next_offset < total, "untrusted_data": True}


def _covered(spans: list[dict], ref: dict, total: int) -> bool:
    end = 0
    for span in sorted((s for s in spans if s.get("part_id") == ref["part_id"]
                        and s.get("content_hash") == ref["content_hash"] and s.get("total") == total),
                       key=lambda s: s.get("start", -1)):
        if span["start"] > end:
            return False
        end = max(end, span["end"])
    return end >= total


async def record_provider_report_reads(ctx, messages: list[dict]) -> None:
    """Confirm evidence survived paging, context budgeting and serialization.

    The processor calls this upon the provider's first response event. A read
    tool invoked alongside an already-written final answer is insufficient:
    its output must reach a subsequent provider request before processing.
    """
    from agent.llm import ensure_fc_id
    async with get_db_session() as db:
        main = await prepare_agent_event_write(db, session_id=ctx.session_id, user_id=ctx.user_id,
                                               run_fence=ctx.run_fence)
        report = await bound_report_locked(db, main, run_id=ctx.run_id, generation=ctx.run_generation)
        if report is None:
            return
        reads = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.user_id == ctx.user_id, AgentEvent.run_id == ctx.run_id,
            AgentEvent.generation == ctx.run_generation, AgentEvent.kind == "assistant.report.sources_read"))).all())
        parts = list((await db.scalars(select(Part).where(Part.session_id == main.id, Part.user_id == ctx.user_id,
            Part.id.in_([event.part_id for event in reads]), Part.type == "tool"))).all())
        calls = {}
        for part in parts:
            if (part.canonical_tool_id or part.data.get("tool")) not in {"history.read", "results.read"}:
                continue
            if part.data.get("status") != "completed":
                continue
            call_id = str(part.data.get("call_id") or f"call_{part.id}")
            calls[call_id] = calls[ensure_fc_id(call_id)] = part.id
        outputs = [(message.get("tool_call_id"), message.get("content")) for message in messages
                   if message.get("role") == "tool"]
        outputs += [(item.get("call_id"), item.get("output")) for message in messages
                    for item in message.get("_responses_input_items", []) if item.get("type") == "function_call_output"]
        sources = {ref["part_id"]: (ref, _text(part)) for ref, part in report.parts}
        version = command_digest({"refs": report.result.output_refs, "projection": EVIDENCE_PROJECTION_VERSION})
        spans = []
        for call_id, output in outputs:
            call_part = calls.get(call_id)
            if not call_part or not isinstance(output, str):
                continue
            try:
                value = json.loads(output)
            except (ValueError, TypeError):
                continue
            if not isinstance(value, dict):
                continue
            receipts = [span for event in reads if event.part_id == call_part
                        and event.payload.get("result_id") == report.result.id
                        and event.payload.get("report_attempt") == report.result.report_attempt
                        and event.payload.get("source_version") == version for span in event.payload.get("spans", [])]
            for entry in value.get("sources", []) + value.get("items", []):
                ref = entry.get("source_ref", entry)
                source = sources.get(ref.get("part_id"))
                text, start = entry.get("text"), entry.get("offset")
                if (source is None or not isinstance(text, str) or type(start) is not int or start < 0
                        or ref.get("content_hash") != source[0]["content_hash"]
                        or source[1][start:start + len(text)] != text):
                    continue
                end = start + len(text)
                if any(span.get("part_id") == ref["part_id"] and span.get("content_hash") == ref["content_hash"]
                       and span["start"] <= start and span["end"] >= end for span in receipts):
                    spans.append({"part_id": ref["part_id"], "content_hash": ref["content_hash"],
                                  "start": start, "end": end, "total": len(source[1])})
        if spans:
            await append_agent_event_locked(db, main, kind="assistant.report.sources_projected", payload={
                "result_id": report.result.id, "report_attempt": report.result.report_attempt,
                "inbox_id": report.inbox.id, "source_version": version, "spans": spans,
            }, run_fence=ctx.run_fence, message_id=ctx.message_id,
                idempotency_key="report-projection:" + command_digest({"message": ctx.message_id,
                    "run": ctx.run_id, "generation": ctx.run_generation, "version": version, "spans": spans}))


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
    binding = await bound_report_locked(db, main, run_id=run_fence[1], generation=run_fence[2],
                                        verify_sources=False)
    if binding is None:
        return False
    result = await db.scalar(select(TaskResult).where(TaskResult.id == binding.result.id).with_for_update())
    invalid_source = None
    try:
        _, source_parts = await validate_result_source(db, result, user_id=main.user_id,
                                                       workspace_id=main.workspace_id, main_id=main.id)
    except AssistantError as exc:
        source_parts, invalid_source = [], exc.code
    version = command_digest({"refs": result.output_refs, "projection": EVIDENCE_PROJECTION_VERSION})
    reads = list((await db.scalars(select(AgentEvent).where(
        AgentEvent.session_id == main.id, AgentEvent.run_id == run_fence[1],
        AgentEvent.generation == run_fence[2], AgentEvent.kind == "assistant.report.sources_projected",
    ).order_by(AgentEvent.sequence).limit(1000))).all())
    verified = [event for event in reads if event.payload.get("result_id") == result.id
                and event.payload.get("report_attempt") == result.report_attempt
                and event.payload.get("inbox_id") == binding.inbox.id
                and event.payload.get("source_version") == version]
    spans = [span for event in verified for span in event.payload["spans"]]
    complete_reads = not invalid_source and bool(verified) and all(
        _covered(spans, ref, len(_text(part))) for ref, part in source_parts)
    answer_parts = list((await db.scalars(select(Part).where(Part.message_id == message.id,
        Part.session_id == main.id, Part.user_id == main.user_id))).all())
    has_answer = any(p.type == "text" and str(p.data.get("text") or "").strip() for p in answer_parts)
    waiting = any(p.type == "tool" and p.data.get("status") in {"pending", "running", "waiting_input"}
                  for p in answer_parts)
    from assistant.context_sources import consumed_contexts
    contexts, context_complete = await consumed_contexts(db, main, message, run_fence=run_fence)
    context_complete = context_complete and all(item["mode"] == "report_only" for item in contexts)
    successful = message.finish == "stop" and not message.error and has_answer and not waiting and complete_reads and context_complete
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
        stopped = message.finish == "aborted"
        reason = "user_stopped" if stopped else invalid_source or (
            "report_evidence_incomplete" if not complete_reads else "report_context_incomplete" if not context_complete else "report_failed")
        mark_report_failed(result, reason=reason, now=now, blocked=stopped or bool(invalid_source))
        if message.finish == "stop":
            message.finish = "error"
            message.error = {"name": "AssistantReportError", "code": reason,
                             "message": "The execution result is saved, but this report could not be verified."}
        item.outcome, item.error = "aborted" if stopped else "error", message.error
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
