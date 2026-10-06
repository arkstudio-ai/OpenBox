"""Current-source projections for legacy transcript and synchronous responses.

The stored transcript is evidence, not a permission cache. Do not modify it
when a source becomes unavailable; omit its public content on every read.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import aliased

from assistant.commands import _authority
from assistant.evidence import validate_message_sources
from assistant.policy import AssistantError
from assistant.results import validate_result_source, validate_source_asset
from assistant.transactions import source_snapshot
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from db.models.message import Message
from db.models.part import Part


def _unavailable(message, status="unavailable"):
    # No error, structured output, tool input, reasoning or attachment metadata
    # can retain content which was removed from the ordinary text projection.
    return {key: getattr(message, key) for key in (
        "id", "session_id", "role", "created_at", "client_message_id",
    )} | {"parts": [], "source_status": status}


async def _run_answers(db, main, ids):
    if len(ids) > 200:
        bindings, answers = {}, {}
        for start in range(0, len(ids), 200):
            more_bindings, more_answers = await _run_answers(db, main, ids[start:start + 200])
            bindings.update(more_bindings)
            answers.update(more_answers)
        return bindings, answers
    events = list((await db.execute(select(AgentEvent.message_id, AgentEvent.run_id, AgentEvent.generation).where(
        AgentEvent.session_id == main.id, AgentEvent.user_id == main.user_id,
        AgentEvent.message_id.in_(ids), AgentEvent.kind == "message.created",
        AgentEvent.run_id.is_not(None),
    ))).all())
    bindings = {row.message_id: (row.run_id, row.generation) for row in events}
    if not bindings:
        return bindings, {}
    terminals = (await db.execute(select(AgentEvent.run_id, AgentEvent.generation, Message).join(
        Message, Message.id == AgentEvent.message_id,
    ).where(AgentEvent.session_id == main.id, AgentEvent.user_id == main.user_id,
        AgentEvent.kind == "turn.finished", tuple_(AgentEvent.run_id, AgentEvent.generation).in_(set(bindings.values())),
        Message.session_id == main.id, Message.user_id == main.user_id,
        Message.finish == "stop", Message.summary.is_not(True),
    ).order_by(AgentEvent.sequence))).all()
    return bindings, {(run, generation): message for run, generation, message in terminals}


async def _budget_failures(db, main, ids):
    """Only a settled, budgeted input can expose this content-free receipt.

    Failed provider output remains unverified. Never publish its error text,
    parts or structured output, even when the error uses a recognized code.
    """
    from assistant.budget import CODE
    failures = {}
    inbox_id = (func.jsonb_extract_path_text(AgentEvent.payload, "inbox_id")
                if db.get_bind().dialect.name == "postgresql"
                else func.json_extract(AgentEvent.payload, "$.inbox_id"))
    for offset in range(0, len(ids), 200):
        rows = (await db.execute(select(AgentInboxItem.result_message_id,
            AgentInboxItem.run_id, AgentInboxItem.generation, AgentInboxItem.error).join(AgentEvent,
                (AgentEvent.session_id == AgentInboxItem.session_id)
                & (AgentEvent.user_id == AgentInboxItem.user_id)
                & (AgentEvent.turn_id == AgentInboxItem.message_id)
                & (AgentEvent.kind == "assistant.budget.started")
                & (inbox_id == AgentInboxItem.id),
            ).where(AgentInboxItem.session_id == main.id, AgentInboxItem.user_id == main.user_id,
                AgentInboxItem.result_message_id.in_(ids[offset:offset + 200]),
                AgentInboxItem.state == "settled", AgentInboxItem.outcome == "error"))).all()
        failures.update({message_id: (run, generation) for message_id, run, generation, error in rows
                         if (error or {}).get("code") == CODE})
    return failures


async def _reply_timings(db, main, ids):
    """Elapsed time belongs to one settled Inbox answer, not a visual turn.

    Main turns claim exactly one input. Match both immutable message/terminal
    boundaries before trusting that input's mutable result reference. Read in
    batches; missing or ambiguous bindings have no inferred duration.
    """
    created, finished = aliased(AgentEvent), aliased(AgentEvent)
    found = {}
    for offset in range(0, len(ids), 200):
        rows = (await db.execute(select(AgentInboxItem.result_message_id,
            AgentInboxItem.accepted_at, AgentInboxItem.settled_at).join(created,
                (created.session_id == AgentInboxItem.session_id)
                & (created.user_id == AgentInboxItem.user_id)
                & (created.message_id == AgentInboxItem.result_message_id)
                & (created.run_id == AgentInboxItem.run_id)
                & (created.generation == AgentInboxItem.generation)
                & (created.turn_id == AgentInboxItem.turn_id)
                & (created.kind == "message.created"),
            ).join(finished,
                (finished.session_id == created.session_id) & (finished.user_id == created.user_id)
                & (finished.message_id == created.message_id) & (finished.run_id == created.run_id)
                & (finished.generation == created.generation) & (finished.turn_id == created.turn_id)
                & (finished.kind == "turn.finished"),
            ).where(AgentInboxItem.session_id == main.id, AgentInboxItem.user_id == main.user_id,
                AgentInboxItem.result_message_id.in_(ids[offset:offset + 200]),
                AgentInboxItem.state == "settled", AgentInboxItem.outcome == "succeeded",
                AgentInboxItem.delivery == "followup", AgentInboxItem.target == "next-turn",
                AgentInboxItem.message_id == AgentInboxItem.turn_id,
                AgentInboxItem.settled_at.is_not(None)))).all()
        for message_id, accepted, settled in rows:
            accepted = accepted.replace(tzinfo=accepted.tzinfo or timezone.utc).astimezone(timezone.utc)
            settled = settled.replace(tzinfo=settled.tzinfo or timezone.utc).astimezone(timezone.utc)
            value = ({"accepted_at": accepted.isoformat(), "settled_at": settled.isoformat()}
                     if settled >= accepted else None)
            found[message_id] = None if message_id in found else value
    return {key: value for key, value in found.items() if value is not None}


async def public_messages(session, messages, *, actor_user_id):
    """Return a fresh projection; an older loaded page cannot bypass this check.

    Legacy callers keep their response envelope and pagination. Main-assistant
    reads rehydrate the selected IDs in one consistent authorization snapshot.
    Provider context uses its own projection and never calls this UI adapter.
    """
    if getattr(session, "kind", None) != "assistant":
        if getattr(session, "memory_policy", None) == "assistant_isolated":
            return await _execution_messages(session, messages, actor_user_id=actor_user_id)
        return [message.model_dump() for message in messages]
    from session.session import _assemble

    async with source_snapshot(reuse_task_facts=True) as (db, snapshot_checks):
        checked_at = ((await db.scalar(select(func.current_timestamp()))).astimezone(timezone.utc) if
            db.get_bind().dialect.name == "postgresql" else datetime.now(timezone.utc)).isoformat(timespec="microseconds")
        main = await _authority(db, user_id=actor_user_id, workspace_id=session.workspace_id, main_id=session.id)
        ids = [message.id for message in messages]
        rows, parts = [], []
        for offset in range(0, len(ids), 500):
            selected = ids[offset:offset + 500]
            rows += list((await db.scalars(select(Message).where(Message.id.in_(selected),
                Message.session_id == main.id, Message.user_id == actor_user_id))).all())
            parts += list((await db.scalars(select(Part).where(Part.message_id.in_(selected),
                Part.session_id == main.id, Part.user_id == actor_user_id).order_by(Part.created_at, Part.id))).all())
        by_id = {row.id: row for row in rows}
        hydrated = {message.id: message for message in _assemble(main.id, rows, parts)}
        bindings, answers = await _run_answers(db, main, ids)
        budget_failures = await _budget_failures(db, main, ids)
        timings = await _reply_timings(db, main, [row.id for row in rows
            if row.role == "assistant" and row.finish == "stop" and not row.summary])
        driver = await db.get(AgentDriverState, main.id)
        await _prove_page(db, main, rows, parts, bindings, answers, actor_user_id=actor_user_id)
        validated = {}
        projected = []
        for original in messages:
            row, message = by_id.get(original.id), hydrated.get(original.id)
            if row is None or message is None:
                projected.append(_unavailable(original))
                continue
            binding = bindings.get(row.id)
            answer = row if row.finish == "stop" else answers.get(binding)
            try:
                if row.role == "assistant":
                    if answer is None:
                        from assistant.budget import CODE, PUBLIC_MESSAGE
                        if (binding is not None and budget_failures.get(row.id) == binding
                                and row.finish == "error" and (row.error or {}).get("code") == CODE):
                            projected.append(_unavailable(message, "available") | {
                                "finish": "error", "error": {"code": CODE, "message": PUBLIC_MESSAGE},
                            })
                            continue
                        pending = (driver is not None and binding == (driver.run_id, driver.generation)
                                   and driver.phase in {"running", "reserved"})
                        projected.append(_unavailable(message, "pending" if pending else "unavailable"))
                        continue
                    if answer.id not in validated:
                        try:
                            await validate_message_sources(db, answer, user_id=actor_user_id,
                                workspace_id=main.workspace_id, main_id=main.id, snapshot_checks=snapshot_checks)
                        except AssistantError:
                            validated[answer.id] = False
                        else:
                            validated[answer.id] = True
                    if not validated[answer.id]:
                        raise AssistantError(410, "ASSISTANT_SOURCE_UNAVAILABLE", "Source is unavailable")
                else:
                    for part in (part for part in parts if part.message_id == row.id):
                        await validate_source_asset(db, part, user_id=actor_user_id, workspace_id=main.workspace_id)
                        if part.data.get("origin") == "task_result":
                            result = await db.get(TaskResult, (part.data.get("origin_ref") or {}).get("result_id"))
                            if result is None:
                                raise AssistantError(410, "ASSISTANT_SOURCE_UNAVAILABLE", "Result is unavailable")
                            await validate_result_source(db, result, user_id=actor_user_id,
                                workspace_id=main.workspace_id, main_id=main.id, snapshot_checks=snapshot_checks)
            except AssistantError:
                projected.append(_unavailable(message))
            else:
                value = message.model_dump() | {"source_status": "available"}
                if row.id in timings:
                    value["assistant_timing"] = timings[row.id]
                projected.append(value)
        return [message | {"source_checked_at": checked_at} for message in projected]


async def _prove_page(db, main, rows, parts, bindings, answers, *, actor_user_id):
    """One read proves every cached answer/result verdict this page will use."""
    from assistant.evidence_cache import prove
    from assistant.verified_units import message_unit, task_graph_unit
    scope = dict(user_id=actor_user_id, workspace_id=main.workspace_id, main_id=main.id)
    units = []
    for row in rows:
        if row.role == "assistant":
            answer = row if row.finish == "stop" else answers.get(bindings.get(row.id))
            if answer is not None:
                units.append(message_unit(answer.id, **scope))
    result_ids = sorted({(part.data.get("origin_ref") or {}).get("result_id") for part in parts
                         if part.data.get("origin") == "task_result"} - {None})
    if result_ids:
        # Loaded once here; the loop's db.get then reads the identity map.
        results = (await db.scalars(select(TaskResult).where(TaskResult.id.in_(result_ids)))).all()
        units += [task_graph_unit(result.task_id, before=result.created_at, **scope) for result in results]
    await prove(db, units)


async def _execution_messages(session, messages, *, actor_user_id):
    from assistant.execution_sources import validate_execution_message
    from session.session import _assemble
    async with source_snapshot(reuse_task_facts=True) as (db, checks):
        checked_at = ((await db.scalar(select(func.current_timestamp()))).astimezone(timezone.utc) if
            db.get_bind().dialect.name == "postgresql" else datetime.now(timezone.utc)).isoformat(timespec="microseconds")
        ids = [message.id for message in messages]
        rows, parts = [], []
        for offset in range(0, len(ids), 500):
            selected = ids[offset:offset + 500]
            rows += list((await db.scalars(select(Message).where(Message.id.in_(selected),
                Message.session_id == session.id, Message.user_id == actor_user_id))).all())
            parts += list((await db.scalars(select(Part).where(Part.message_id.in_(selected),
                Part.session_id == session.id, Part.user_id == actor_user_id)
                .order_by(Part.created_at, Part.id))).all())
        by_id = {row.id: row for row in rows}
        hydrated = {message.id: message for message in _assemble(session.id, rows, parts)}
        from assistant.evidence_cache import prove
        from assistant.verified_units import execution_message_unit
        await prove(db, [execution_message_unit(row.id, user_id=actor_user_id, workspace_id=session.workspace_id,
                                                main_id=None) for row in rows])
        projected = []
        for original in messages:
            row, message = by_id.get(original.id), hydrated.get(original.id)
            try:
                if row is None or message is None:
                    raise AssistantError(410, "ASSISTANT_SOURCE_UNAVAILABLE", "Message is unavailable")
                await validate_execution_message(db, row, user_id=actor_user_id,
                    workspace_id=session.workspace_id, snapshot_checks=checks)
                for part in (part for part in parts if part.message_id == row.id):
                    await validate_source_asset(db, part, user_id=actor_user_id, workspace_id=session.workspace_id)
            except AssistantError:
                projected.append(_unavailable(original))
            else:
                projected.append(message.model_dump() | {"source_status": "available"})
        return [message | {"source_checked_at": checked_at} for message in projected]
