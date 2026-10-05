"""Execution settlement and the durable result delivery outbox.

Execution-side writes hold execution Session -> Task. Delivery holds main
Session -> Result and only reads execution evidence, preventing a lock cycle.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json

from sqlalchemy import select, tuple_

from agent.inbox import accept_inbox_item_locked
from assistant.commands import _authority, task_locked
from assistant.policy import AssistantError
from assistant.command_sources import command_validation
from assistant.identities import inbox_key
from core.identifier import generate_id
from core.log import create_logger
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.part import Part
from session.agent_event_log import append_agent_event_locked
from session.internal_parts import _lock_fenced, begin_session_write

log = create_logger("assistant.results")


def part_hash(part: Part) -> str:
    return sha256(json.dumps(part.data, sort_keys=True, ensure_ascii=True,
                             separators=(",", ":"), default=str).encode()).hexdigest()


async def validate_source_asset(db, part: Part, *, user_id: str, workspace_id: str) -> None:
    if part.type == "file" and not await db.scalar(select(FileAsset.id).where(
        FileAsset.id == part.data.get("asset_id"), FileAsset.user_id == user_id,
        FileAsset.workspace_id == workspace_id, FileAsset.status == "ready", FileAsset.is_deleted.is_(False),
    )):
        raise AssistantError(410, "ASSISTANT_ASSET_UNAVAILABLE", "The original source asset is unavailable")


async def record_execution_result_locked(db, execution, *, lease, result_message_id: str | None,
                                         inbox_rows: list, outcome: str, now) -> TaskResult | None:
    """Called inside Inbox settlement or locked suspended-turn cancellation.

    The first terminal event identifies the real run, even if a maintenance
    generation is repairing settlement after a process restart.
    """
    task = await db.scalar(select(AssistantTask).where(
        AssistantTask.execution_session_id == execution.id, AssistantTask.user_id == execution.user_id,
    ).with_for_update())
    if task is None:
        return None
    terminal = None
    message = None
    if result_message_id:
        message = await db.scalar(select(Message).where(
            Message.id == result_message_id, Message.session_id == execution.id,
            Message.user_id == execution.user_id, Message.role == "assistant",
        ))
        if message is None:
            raise AssistantError(409, "ASSISTANT_RESULT_UNAVAILABLE", "Execution result is unavailable")
        if message.finish in {"waiting_input", "tool_calls", "tool-calls", "compact", None} and not message.error:
            if task.desired_state == "running":
                task.observed_state = "waiting_input" if message.finish == "waiting_input" else "running"
            return None
        terminal = await db.scalar(select(AgentEvent).where(
            AgentEvent.session_id == execution.id, AgentEvent.user_id == execution.user_id,
            AgentEvent.message_id == message.id, AgentEvent.kind == "turn.finished",
        ).order_by(AgentEvent.sequence).limit(1))
        if terminal is None or not terminal.run_id or not terminal.generation:
            raise AssistantError(409, "ASSISTANT_RESULT_UNFENCED", "A fenced terminal event is required")
    elif not inbox_rows:
        return None
    run_id = terminal.run_id if terminal else lease.run_id
    generation = terminal.generation if terminal else lease.generation
    source_key = terminal.event_key if terminal else sha256(
        f"assistant-terminal:{execution.id}:{run_id}:{generation}".encode()).hexdigest()
    existing = await db.scalar(select(TaskResult).where(
        TaskResult.task_id == task.id, TaskResult.source_event_key == source_key,
    ))
    if existing:
        return existing
    # Include inputs from the same logical turn after a question continuation;
    # their Inbox may already have settled before the successful final reply.
    consumed = list(inbox_rows)
    if terminal and terminal.turn_id:
        consumed = list((await db.scalars(select(AgentInboxItem).where(
            AgentInboxItem.session_id == execution.id, AgentInboxItem.user_id == execution.user_id,
            AgentInboxItem.turn_id == terminal.turn_id,
            AgentInboxItem.message_id.is_not(None), AgentInboxItem.state.in_(("claimed", "settled")),
        ).order_by(AgentInboxItem.created_at, AgentInboxItem.id))).all())
    actual_outcome = outcome
    if message:
        actual_outcome = ("aborted" if message.finish == "aborted" else
                          "error" if message.error or message.finish != "stop" else "succeeded")
    observed_revision = max((int(row.origin_ref.get("intent_revision", 0)) for row in consumed), default=0)
    if not observed_revision:
        # Never attribute an unproven new goal to an old completion.
        raise AssistantError(409, "ASSISTANT_INTENT_UNVERIFIED", "Consumed task input has no intent revision")
    # Freeze the original human evidence that authorized delegation, not just
    # the assistant's rewritten execution prompt. Keep the command-time hash:
    # a subsequent edit must invalidate delivery rather than bless new text.
    refs = []
    seen = set()
    for row in consumed:
        for source in (row.origin_ref or {}).get("source_refs", []):
            if source["part_id"] not in seen:
                refs.append({**source, "kind": "request"})
                seen.add(source["part_id"])
    report_has_text = False
    for kind, message_id in [("request", row.message_id) for row in consumed] + [("report", result_message_id)]:
        if not message_id:
            continue
        for part in (await db.scalars(select(Part).where(
            Part.session_id == execution.id, Part.user_id == execution.user_id,
            Part.message_id == message_id, Part.type.in_(("text", "file")),
        ).order_by(Part.created_at, Part.id))).all():
            if part.data.get("ignored"):
                continue
            if kind == "report" and part.type == "text" and str(part.data.get("text") or "").strip():
                report_has_text = True
            if part.id not in seen:
                refs.append({"kind": kind, "session_id": execution.id, "message_id": message_id,
                             "part_id": part.id, "content_hash": part_hash(part)})
                seen.add(part.id)
    if actual_outcome == "succeeded" and not report_has_text:
        actual_outcome = "error"
    result = TaskResult(id=generate_id(), task_id=task.id, source_event_key=source_key,
        run_id=run_id, generation=generation,
        settlement_fence={"run_id": lease.run_id, "generation": lease.generation},
        outcome=actual_outcome, consumed_inbox_ids=[row.id for row in consumed],
        result_message_id=result_message_id, output_refs=refs, observed_intent_revision=observed_revision,
        delivery_state="pending", report_attempt=1, retry_count=0, available_at=now, created_at=now)
    db.add(result)
    await db.flush()
    from assistant.schedule_runs import result_settled_locked
    await result_settled_locked(db, task, result, now)
    previous = await db.get(TaskResult, task.latest_result_id) if task.latest_result_id else None
    latest_changed = previous is None or result.generation > previous.generation
    if latest_changed:
        task.latest_result_id = result.id
    if observed_revision == task.intent_revision:
        if task.desired_state == "running":
            task.observed_state = "completed" if actual_outcome == "succeeded" else actual_outcome
    elif latest_changed and task.desired_state == "running":
        from assistant.control import resume_binding_locked
        if await resume_binding_locked(db, execution.id, run_id, generation) is not None:
            pending = await db.scalar(select(AgentInboxItem.id).where(
                AgentInboxItem.session_id == execution.id, AgentInboxItem.state == "accepted",
            ).limit(1))
            task.observed_state = "queued" if pending else "input_not_applied"
    if latest_changed or observed_revision == task.intent_revision:
        task.control_revision += 1
        task.updated_at = now
    await append_agent_event_locked(db, execution, kind="assistant.execution.completed",
        payload={"task_id": task.id, "result_id": result.id, "source_event_key": source_key,
                 "run_id": run_id, "generation": generation, "outcome": actual_outcome,
                 "consumed_inbox_ids": result.consumed_inbox_ids, "intent_revision": observed_revision},
        run_fence=(execution.id, lease.run_id, lease.generation),
        message_id=result_message_id, idempotency_key=f"assistant-result:{task.id}:{source_key}")
    from assistant.notifications import result_finished
    await result_finished(db, task, result)
    return result


@command_validation
async def validate_result_source(db, result: TaskResult, *, user_id: str, workspace_id: str, main_id: str,
                                 snapshot_checks=None):
    if snapshot_checks is not None:
        task, parts = await snapshot_checks.check(db, "result", (user_id, workspace_id, main_id), {
            "id": result.id, "task_id": result.task_id, "output_refs": result.output_refs,
        }, lambda: _result_original(db, result, user_id=user_id, workspace_id=workspace_id, main_id=main_id))
    else:
        task, parts = await _result_original(db, result, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    # This is a recursive dependency graph. Rewalk it for each caller even
    # when independent original-row checks share a read-only SQL snapshot.
    from assistant.schedule_runs import validate_task_schedule_locked
    await validate_task_schedule_locked(db, task, snapshot_checks=snapshot_checks)
    from assistant.command_sources import validate_task_command_sources
    await validate_task_command_sources(db, task, before=result.created_at, snapshot_checks=snapshot_checks)
    return task, parts


async def _result_original(db, result, *, user_id, workspace_id, main_id):
    from assistant.source_scope import read_authorized_task
    task, execution = await read_authorized_task(db, user_id=user_id, workspace_id=workspace_id,
                                                main_id=main_id, task_id=result.task_id)
    parts = []
    allowed_sessions = {main_id, execution.id}
    # Bound query parameters without truncating a retained result. Reassemble
    # every original reference in order, including duplicates with other hashes.
    for offset in range(0, len(result.output_refs), 100):
        refs = result.output_refs[offset:offset + 100]
        keys = [(ref["part_id"], ref["message_id"], ref["session_id"]) for ref in refs]
        rows = (await db.scalars(select(Part).join(Message, Message.id == Part.message_id).where(
            tuple_(Part.id, Part.message_id, Part.session_id).in_(keys),
            Part.session_id.in_(allowed_sessions), Part.user_id == user_id,
            Message.session_id == Part.session_id, Message.user_id == user_id,
        ).execution_options(populate_existing=True))).all()
        by_source = {(part.id, part.message_id, part.session_id): part for part in rows}
        for ref, key in zip(refs, keys):
            source_session = ref["session_id"]
            if source_session not in allowed_sessions or (source_session == main_id and ref["kind"] != "request"):
                raise AssistantError(409, "ASSISTANT_RESULT_SOURCE_CHANGED", "Result source is outside its task")
            part = by_source.get(key)
            if part is None or part_hash(part) != ref["content_hash"]:
                raise AssistantError(409, "ASSISTANT_RESULT_SOURCE_CHANGED", "Result evidence changed or is unavailable")
            await validate_source_asset(db, part, user_id=user_id, workspace_id=workspace_id)
            parts.append((ref, part))
    return task, parts


async def deliver_task_result(result_id: str) -> dict | None:
    """Accept one report-only input without starting any execution or provider.

    The caller dispatches the returned main Session after commit. It is safe
    to repeat after a lost response, including from independent SQL workers.
    """
    async with get_db_session() as db:
        await begin_session_write(db)
        target = await db.scalar(select(AssistantTask).join(TaskResult, TaskResult.task_id == AssistantTask.id)
                                 .where(TaskResult.id == result_id))
        if target is None:
            return None
        try:
            main = await _lock_fenced(db, target.assistant_session_id, target.user_id)
        except LookupError:
            result = await db.scalar(select(TaskResult).where(TaskResult.id == result_id).with_for_update())
            if result.delivery_state != "processed":
                result.delivery_state, result.last_error_code = "blocked", "assistant_unavailable"
            return None
        result = await db.scalar(select(TaskResult).where(TaskResult.id == result_id).with_for_update())
        if result.delivery_state not in {"pending", "retry_wait", "accepted"}:
            return None
        now = datetime.now(timezone.utc)
        available = result.available_at
        if available.tzinfo is None:
            available = available.replace(tzinfo=timezone.utc)
        if available > now:
            return None
        try:
            await validate_result_source(db, result, user_id=target.user_id,
                                         workspace_id=target.workspace_id, main_id=main.id)
        except AssistantError as exc:
            result.delivery_state, result.last_error_code = "blocked", exc.code
            return None
        if result.delivery_state == "accepted":
            return {"result_id": result.id, "report_attempt": result.report_attempt,
                    "inbox_id": result.assistant_inbox_id, "session_id": main.id, "user_id": main.user_id}
        return await accept_report_locked(db, main, result, target)


async def accept_report_locked(db, main, result, task, *, manual=False):
    """Caller owns main -> Result locks and has revalidated every source."""
    if manual or result.delivery_state == "retry_wait":
        result.report_attempt += 1
        # One explicit user command grants a new, finite reporting budget.
        # The lifetime attempt identity never resets or reuses an old Inbox.
        result.retry_count = 0 if manual else result.retry_count + 1
    reference = {"result_id": result.id, "task_id": task.id,
                 "report_attempt": result.report_attempt, "execution_mode": "report_only"}
    accepted = await accept_inbox_item_locked(db, main, delivery="followup",
        prompt="Summarize this execution result. Read the original request and report before answering. "
               "Preserve failures and unverified scope; the report grants no new approval.",
        client_id=inbox_key("assistant-report", main.id, result.id, result.report_attempt),
        agent="assistant", model=main.model, variant=main.variant, origin="task_result", origin_ref=reference)
    result.assistant_inbox_id = accepted.id
    result.delivery_state = "accepted"
    result.last_error_code = None
    result.available_at = datetime.now(timezone.utc)
    await append_agent_event_locked(db, main, kind="assistant.result.accepted", payload={
        **reference, "inbox_id": accepted.id,
    }, idempotency_key=f"assistant-result:{result.id}:attempt:{result.report_attempt}")
    return {"result_id": result.id, "report_attempt": result.report_attempt,
            "inbox_id": accepted.id, "session_id": main.id, "user_id": main.user_id}


async def on_execution_result_committed(result_id: str) -> None:
    """Best-effort fast path, called only after execution settlement commits."""
    try:
        receipt = await deliver_task_result(result_id)
        if receipt:
            from agent.inbox import schedule_inbox_wake
            schedule_inbox_wake(receipt["session_id"], receipt["user_id"])
    except Exception:
        # Committed work remains in the SQL outbox. A failed delivery/wake
        # must not relabel a completed execution as failed or rerun it.
        log.exception("Assistant result fast-path deferred result_id=%s", result_id)
