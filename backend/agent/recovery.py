"""Logical tail repair for agent runs whose durable lease expired.

Recovery never replays a tool body.  A pending call is recorded as not
started; a running call is recorded as outcome unknown.  The open assistant
step is then closed as aborted so future context construction has a balanced,
honest tail.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select

from agent.driver import (
    DriverBusyError,
    DriverQuotaExceededError,
    RecoveredDriver,
    StaleRecoveryError,
    reserve_recovered_run,
    reserve_run,
)
from bus import bus
from bus.events import SESSION_STATUS
from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.message import Message as MessageRow
from db.models.part import Part as PartRow
from db.models.session import Session as SessionRow
from models.message import StepFinishPart

log = create_logger("agent.recovery")

_resume_tasks: set[asyncio.Task] = set()
_resume_leases: dict[asyncio.Task, Any] = {}


def _track_resume_task(task: asyncio.Task, lease: Any) -> None:
    _resume_tasks.add(task)
    _resume_leases[task] = lease

    def discard(done: asyncio.Task) -> None:
        _resume_tasks.discard(done)
        _resume_leases.pop(done, None)

    task.add_done_callback(discard)


async def quiesce_recovery_tasks(*, timeout: float) -> None:
    """Persist aborts, then bound-wait recovery-owned child coroutines."""
    tasks = [task for task in _resume_tasks if not task.done()]
    if tasks:
        from agent.driver import request_abort

        await asyncio.gather(
            *(
                request_abort(
                    lease.session_id,
                    lease.user_id,
                    expected_run_id=lease.run_id,
                    expected_generation=lease.generation,
                )
                for task, lease in list(_resume_leases.items())
                if task in tasks
            ),
            return_exceptions=True,
        )
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    from agent.inbox import quiesce_inbox_tasks

    await quiesce_inbox_tasks(timeout=timeout)


TOOL_NOT_STARTED = "tool_not_started"
TOOL_OUTCOME_UNKNOWN = "tool_outcome_unknown"

_RECOVERY_ERRORS = {
    TOOL_NOT_STARTED: ("Tool was not started before recovery. It was not executed."),
    TOOL_OUTCOME_UNKNOWN: (
        "Tool outcome is unknown after recovery. Do not retry automatically; "
        "inspect external state or ask the user."
    ),
}


def _publish_recovery_status(lease, status: str) -> None:
    bus.publish(
        SESSION_STATUS,
        {
            "userId": lease.user_id,
            "sessionId": lease.session_id,
            "status": status,
            "generation": lease.generation,
        },
    )


async def _release_recovery_status(lease, status: str) -> bool:
    """Atomically settle a maintenance generation, then announce its revision."""
    matched = await lease.release(session_status=status)
    if matched:
        _publish_recovery_status(lease, status)
    return matched


async def _preserve_recovery_status(lease, status: str = "error") -> bool:
    """Publish only after the exact maintenance marker/status transaction commits."""
    matched = await lease.preserve_for_recovery(session_status=status)
    if matched:
        _publish_recovery_status(lease, status)
    return matched


@dataclass(frozen=True, slots=True)
class RepairResult:
    session_id: str
    repaired_tools: int = 0
    rejoined_tasks: int = 0
    closed_steps: int = 0
    closed_messages: int = 0
    skipped: bool = False


async def _trigger_state(
    record: RecoveredDriver,
) -> tuple[bool, str | None, list[str]]:
    """Return (valid user wake, exact answer id, ordered attachment ids)."""
    if not record.trigger_message_id:
        return False, None, []
    async with get_db_session() as db:
        trigger = (
            await db.execute(
                select(MessageRow).where(
                    MessageRow.id == record.trigger_message_id,
                    MessageRow.session_id == record.session_id,
                    MessageRow.user_id == record.user_id,
                    MessageRow.role == "user",
                )
            )
        ).scalar_one_or_none()
        if trigger is None:
            return False, None, []
        # An Inbox boundary can atomically materialize several next-step rows
        # before its one next-turn row.  The Driver trigger names the logical
        # turn, while attachment delivery must cover every exact Message claimed
        # by that generation after a crash between claim commit and wake.
        inbox_message_ids = list(
            (
                await db.execute(
                    select(AgentInboxItem.message_id)
                    .where(
                        AgentInboxItem.session_id == record.session_id,
                        AgentInboxItem.user_id == record.user_id,
                        AgentInboxItem.run_id == record.run_id,
                        AgentInboxItem.generation == record.generation,
                        AgentInboxItem.state == "claimed",
                        AgentInboxItem.message_id.is_not(None),
                    )
                    .order_by(AgentInboxItem.created_at, AgentInboxItem.id)
                )
            )
            .scalars()
            .all()
        )
        if inbox_message_ids:
            claimed_messages = list(
                (
                    await db.execute(
                        select(MessageRow)
                        .where(
                            MessageRow.id.in_(inbox_message_ids),
                            MessageRow.session_id == record.session_id,
                            MessageRow.user_id == record.user_id,
                            MessageRow.role == "user",
                        )
                        .order_by(MessageRow.created_at, MessageRow.id)
                    )
                )
                .scalars()
                .all()
            )
            if len(claimed_messages) != len(set(inbox_message_ids)):
                return False, None, []
            trigger_message_ids = [message.id for message in claimed_messages]
        else:
            trigger_message_ids = [record.trigger_message_id]
        answer_parent_id = trigger_message_ids[-1]
        answered = (
            await db.execute(
                select(MessageRow.id)
                .where(
                    MessageRow.session_id == record.session_id,
                    MessageRow.user_id == record.user_id,
                    MessageRow.role == "assistant",
                    MessageRow.parent_id == answer_parent_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        parts = list(
            (
                await db.execute(
                    select(PartRow)
                    .where(
                        PartRow.message_id.in_(trigger_message_ids),
                        PartRow.session_id == record.session_id,
                        PartRow.user_id == record.user_id,
                        PartRow.type == "file",
                    )
                    .order_by(PartRow.created_at, PartRow.id)
                )
            )
            .scalars()
            .all()
        )
    asset_ids = [
        str(part.data["asset_id"])
        for part in parts
        if isinstance(part.data, dict) and part.data.get("asset_id")
    ]
    return True, answered, asset_ids


async def _recovered_terminal_message_id(
    record: RecoveredDriver,
) -> str | None:
    if not record.trigger_message_id:
        return None
    async with get_db_session() as db:
        claimed_ids = list(
            (
                await db.execute(
                    select(AgentInboxItem.message_id).where(
                        AgentInboxItem.session_id == record.session_id,
                        AgentInboxItem.user_id == record.user_id,
                        AgentInboxItem.state == "claimed",
                        AgentInboxItem.message_id.is_not(None),
                        or_(
                            and_(
                                AgentInboxItem.run_id == record.run_id,
                                AgentInboxItem.generation == record.generation,
                            ),
                            AgentInboxItem.turn_id == record.trigger_message_id,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        parent_id = record.trigger_message_id
        if claimed_ids:
            claimed_messages = list(
                (
                    await db.execute(
                        select(MessageRow)
                        .where(
                            MessageRow.id.in_(claimed_ids),
                            MessageRow.session_id == record.session_id,
                            MessageRow.user_id == record.user_id,
                            MessageRow.role == "user",
                        )
                        .order_by(MessageRow.created_at, MessageRow.id)
                    )
                )
                .scalars()
                .all()
            )
            if len(claimed_messages) != len(set(claimed_ids)):
                raise RuntimeError("recovered Inbox boundary Message set drifted")
            parent_id = claimed_messages[-1].id
        terminal = (
            await db.execute(
                select(MessageRow)
                .where(
                    MessageRow.session_id == record.session_id,
                    MessageRow.user_id == record.user_id,
                    MessageRow.role == "assistant",
                    MessageRow.parent_id == parent_id,
                )
                .order_by(
                    MessageRow.created_at.desc(),
                    MessageRow.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        terminal_id = None
        if terminal is not None and (
            terminal.error
            or (
                terminal.finish is not None
                and terminal.finish not in {"tool_calls", "tool-calls", "compact"}
            )
        ):
            terminal_id = terminal.id
        if claimed_ids and terminal_id is None:
            # Never acknowledge a claimed late boundary using an earlier
            # Assistant or a null result. Keep the maintenance marker
            # recoverable until canonical tail repair produces its closure.
            raise RuntimeError("recovered Inbox boundary has no terminal Assistant")
        return terminal_id


async def _run_recovered_prompt(lease, asset_ids: list[str]) -> None:
    """Re-drive only a wake proven not to have crossed the running boundary."""
    delivery_preserved = False
    try:
        if asset_ids:
            try:
                from agent.inbox import deliver_claimed_attachments

                delivery = await deliver_claimed_attachments(
                    lease,
                    expected_asset_ids=asset_ids,
                )
                if not delivery.should_run_provider:
                    delivery_preserved = True
                    await _release_recovery_status(lease, "error")
                    return
            except Exception:
                log.exception(
                    "Recovered prompt strict attachment delivery failed session=%s",
                    lease.session_id,
                )
                # Keep the exact reserved trigger discoverable. Releasing it to
                # idle here would strand claimed Inbox rows and let the API call
                # appear successful even though the model never saw its files.
                delivery_preserved = True
                await _preserve_recovery_status(lease, "error")
                return
        from agent.loop import run_loop

        await run_loop(
            lease.session_id,
            user_id=lease.user_id,
            lease=lease,
        )
        # A recovered Task child no longer has its original parent coroutine.
        # Materialize its transcript into the durable outbox, then let a
        # maintenance generation rejoin/close the parent without replaying it.
        from agent.task_handoff import complete_task_handoff_for_child

        handoff_id = await complete_task_handoff_for_child(
            lease.session_id,
            child_run_id=lease.run_id,
            child_generation=lease.generation,
        )
        if handoff_id is not None:
            await reconcile_completed_task_handoffs()
        from agent.subagent_runtime import complete_activation_for_child

        activation_id = await complete_activation_for_child(
            lease.session_id,
            child_run_id=lease.run_id,
            child_generation=lease.generation,
        )
        if activation_id is not None:
            await reconcile_completed_task_handoffs()
    except asyncio.CancelledError:
        # Shutdown may interrupt strict delivery before run_loop owns its
        # normal finalizer. Keep the claimed trigger as an expired exact marker
        # instead of releasing it idle and stranding the durable Inbox row.
        delivery_preserved = True
        await _preserve_recovery_status(lease, "error")
        raise
    except Exception:
        log.exception("Recovered prompt failed session=%s", lease.session_id)
        delivery_preserved = True
        await _preserve_recovery_status(lease, "error")
    finally:
        # Idempotent after run_loop's normal release. This also covers a
        # failure before run_loop establishes its own try/finally boundary.
        if not delivery_preserved:
            await _release_recovery_status(lease, "error")


async def resume_reserved_prompts(
    records: list[RecoveredDriver],
) -> tuple[list[str], list[RecoveredDriver]]:
    """Restart durable accepted wakes that never entered provider/tool execution.

    ``reserved`` is the only replay-safe phase. A ``running`` or ``finalizing``
    generation is repaired as interrupted instead because its external outcome
    may be unknown.
    """
    resumed: list[str] = []
    invalid: list[RecoveredDriver] = []
    for record in records:
        if record.phase != "reserved" or not record.trigger_message_id:
            invalid.append(record)
            continue
        from agent.subagent_runtime import activation_blocks_reserved_replay

        if await activation_blocks_reserved_replay(record):
            invalid.append(record)
            continue
        # Validate the accepted wake before claiming its marker. Invalid
        # records remain discoverable for deterministic tail repair.
        valid, answer_id, asset_ids = await _trigger_state(record)
        if not valid:
            invalid.append(record)
            continue
        # A committed generation-scoped interrupt is consumed before recovery
        # is allowed to reserve or wake this accepted child trigger.
        from agent.subagent_runtime import activation_interrupt_pending

        if await activation_interrupt_pending(record):
            invalid.append(record)
            continue
        try:
            lease = await reserve_recovered_run(
                record,
                initial_phase="reserved",
            )
        except DriverQuotaExceededError:
            # A hard cluster slot is not an invalid wake. Leave the exact
            # expired marker untouched so the next periodic pass can retry it.
            continue
        except StaleRecoveryError:
            # A prompt or another reaper advanced the exact marker after this
            # sweep took its snapshot. Never replay the old accepted wake.
            continue
        except LookupError:
            invalid.append(record)
            continue

        try:
            # Inbox rows, trigger Message and driver marker crossed the original
            # acceptance transaction together. Move their claim to this exact
            # takeover generation before either replay or terminal settlement.
            from agent.inbox import rebind_recovered_claims

            await rebind_recovered_claims(record, lease)
            from agent.task_handoff import bind_task_handoff_for_recovered_child

            handoff_id = await bind_task_handoff_for_recovered_child(lease, record)
            from agent.subagent_runtime import bind_recovered_activation

            activation_binding = await bind_recovered_activation(lease, record)
        except Exception:
            await _release_recovery_status(lease, "error")
            invalid.append(record)
            continue
        if activation_binding.state == "terminal":
            # The activation crossed to terminal after the pre-takeover scan.
            # Preserve the generation we just claimed as an immediately
            # repairable marker; never treat a terminal activation as an
            # ordinary prompt merely because active binding returned no id.
            repair_record = RecoveredDriver(
                session_id=lease.session_id,
                user_id=lease.user_id,
                run_id=lease.run_id,
                generation=lease.generation,
                phase="reserved",
                trigger_message_id=record.trigger_message_id,
            )
            try:
                await _preserve_recovery_status(lease, "error")
            except Exception:
                log.exception(
                    "Could not preserve terminal activation marker session=%s",
                    lease.session_id,
                )
            invalid.append(repair_record)
            continue
        activation_id = activation_binding.activation_id
        if answer_id is not None:
            from agent.inbox import settle_claimed_inbox_items

            await settle_claimed_inbox_items(
                lease,
                result_message_id=answer_id,
                outcome="recovered",
            )
            await _release_recovery_status(lease, "idle")
            if handoff_id is not None:
                from agent.task_handoff import complete_task_handoff_for_child

                await complete_task_handoff_for_child(
                    lease.session_id,
                    child_run_id=lease.run_id,
                    child_generation=lease.generation,
                )
                await reconcile_completed_task_handoffs()
            if activation_id is not None:
                from agent.subagent_runtime import complete_activation_for_child

                await complete_activation_for_child(
                    lease.session_id,
                    child_run_id=lease.run_id,
                    child_generation=lease.generation,
                )
                await reconcile_completed_task_handoffs()
            continue

        task = asyncio.create_task(
            _run_recovered_prompt(lease, asset_ids),
            name=f"agent-resume:{record.session_id}:{lease.generation}",
        )
        _track_resume_task(task, lease)
        resumed.append(record.session_id)
    return resumed, invalid


def is_recovered_tool_part(data: dict) -> bool:
    return (data.get("metadata") or {}).get("recovery_code") in {
        TOOL_NOT_STARTED,
        TOOL_OUTCOME_UNKNOWN,
    }


async def repair_interrupted_session(
    session_id: str,
    user_id: str,
    *,
    recovered: RecoveredDriver | None = None,
    rejoin_only: bool = False,
    restore_status: str | None = None,
) -> RepairResult:
    """Repair one tail while holding a fresh maintenance generation."""
    if recovered is not None and (
        recovered.session_id != session_id or recovered.user_id != user_id
    ):
        raise ValueError("recovered driver does not match repair target")
    try:
        if recovered is not None:
            lease = await reserve_recovered_run(
                recovered,
                initial_phase="finalizing",
            )
        else:
            lease = await reserve_run(
                session_id,
                user_id,
                initial_phase="finalizing",
            )
    except (DriverBusyError, StaleRecoveryError, LookupError):
        # A prompt won the race after expiry recovery.  Its live driver owns
        # any cleanup; never mutate beneath it.
        return RepairResult(session_id=session_id, skipped=True)

    repaired_tools = 0
    rejoined_tasks = 0
    closed_steps = 0
    closed_messages = 0
    release_status = "error"
    repair_completed = False
    try:
        if recovered is not None:
            from agent.inbox import rebind_recovered_claims

            await rebind_recovered_claims(recovered, lease)
        await lease.assert_current()
        now = datetime.now(timezone.utc)
        async with get_db_session() as db:
            session = (
                await db.execute(
                    select(SessionRow)
                    .where(
                        SessionRow.id == session_id,
                        SessionRow.user_id == user_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if session is None:
                # Early returns occur inside the managed transaction. Commit
                # explicitly before declaring the maintenance claim complete;
                # otherwise a __aexit__ commit failure could clear its marker
                # even though the transcript transaction never committed.
                await db.commit()
                repair_completed = True
                return RepairResult(session_id=session_id, skipped=True)

            # The maintenance claim is checked inside the same transaction as
            # every transcript repair and canonical shadow event. A takeover
            # cannot interleave between the read-model write and its append.
            from agent.driver import assert_run_fence_locked
            from session.agent_event_log import ensure_surface_seed_locked

            await assert_run_fence_locked(
                db,
                session_id=session_id,
                user_id=user_id,
                run_id=lease.run_id,
                generation=lease.generation,
            )
            await ensure_surface_seed_locked(db, session)
            maintenance_fence = (session_id, lease.run_id, lease.generation)
            # The maintenance generation authorizes this transaction, but the
            # lifecycle events close the logical run that actually started the
            # turn.  Keeping those identities separate lets strict event
            # verification pair turn.started/turn.finished after a crash.
            logical_run_fence = maintenance_fence
            if recovered is not None and recovered.run_id:
                logical_run_fence = (
                    session_id,
                    recovered.run_id,
                    recovered.generation,
                )

            # Apply a child result before generic tool repair sees the Task
            # card. The maintenance generation plus exact descriptor/tenant
            # identity fences the write; later messages make it fail closed.
            from agent.task_handoff import apply_completed_task_handoffs_locked

            applied_handoffs = await apply_completed_task_handoffs_locked(
                db,
                parent_session_id=session_id,
                user_id=user_id,
                maintenance_run_id=lease.run_id,
                maintenance_generation=lease.generation,
            )
            from agent.subagent_runtime import apply_ready_subagent_outboxes_locked

            applied_subagents = await apply_ready_subagent_outboxes_locked(
                db,
                parent_session_id=session_id,
                user_id=user_id,
                maintenance_run_id=lease.run_id,
                maintenance_generation=lease.generation,
            )
            rejoined_tasks = applied_handoffs.rejoined + applied_subagents.rejoined
            updated_message_ids = {
                str(data.get("message_id"))
                for data in (*applied_handoffs.updates, *applied_subagents.updates)
                if data.get("message_id")
            }
            delivery_message_ids = {
                *applied_handoffs.message_ids,
                *applied_subagents.message_ids,
            }
            if rejoin_only and rejoined_tasks == 0:
                # The tail fence was lost after candidate selection. Do not
                # scan historical tools or poison a healthy Session merely
                # because an old outbox still exists.
                release_status = restore_status or "idle"
                await db.commit()
                repair_completed = True
                return RepairResult(
                    session_id=session_id,
                    skipped=True,
                )

            if not rejoin_only:
                # Cold loads and periodic recovery share one canonical tail
                # repair primitive. The current maintenance fence authorizes
                # the transaction; the helper preserves the interrupted run's
                # logical identity on turn/tool/step closure events.
                from session.agent_event_log import repair_canonical_tail_locked

                repaired = await repair_canonical_tail_locked(
                    db,
                    session,
                    run_fence=maintenance_fence,
                    target_user_message_id=(
                        recovered.trigger_message_id if recovered is not None else None
                    ),
                    allow_unanchored_assistant=True,
                )
                repaired_tools = repaired.repaired_tools
                closed_steps = repaired.closed_steps
                closed_messages = repaired.closed_messages
                await db.commit()
                repair_completed = True
                return RepairResult(
                    session_id=session_id,
                    repaired_tools=repaired_tools,
                    rejoined_tasks=rejoined_tasks,
                    closed_steps=closed_steps,
                    closed_messages=closed_messages,
                )

            message_query = select(MessageRow).where(
                MessageRow.session_id == session_id,
                MessageRow.user_id == user_id,
                MessageRow.role == "assistant",
            )
            if rejoin_only:
                # Rejoin-only repair owns exactly the Task delivery tail. It
                # must never sweep unrelated historical pending cards.
                message_query = message_query.where(
                    MessageRow.id.in_(delivery_message_ids)
                )
            elif recovered is not None and recovered.trigger_message_id:
                # The accepted wake is the durable ownership boundary. Never
                # let recovery for one expired generation sweep pending cards
                # from an unrelated historical turn in the same Session.
                message_query = message_query.where(
                    MessageRow.parent_id == recovered.trigger_message_id
                )
            messages = list(
                (
                    await db.execute(
                        message_query.order_by(
                            MessageRow.created_at,
                            MessageRow.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            synthesized_terminal = False
            if (
                not rejoin_only
                and recovered is not None
                and recovered.trigger_message_id
                and not any(
                    message.parent_id == recovered.trigger_message_id
                    for message in messages
                )
            ):
                # A process can die after accepting/binding the User Message
                # but before creating its Assistant row.  Materialize an
                # explicit aborted reply so the accepted turn is not left open
                # forever and a later prompt can safely follow it.
                trigger = (
                    await db.execute(
                        select(MessageRow).where(
                            MessageRow.id == recovered.trigger_message_id,
                            MessageRow.session_id == session_id,
                            MessageRow.user_id == user_id,
                            MessageRow.role == "user",
                        )
                    )
                ).scalar_one_or_none()
                if trigger is not None:
                    from session.agent_event_log import append_message_events_locked

                    terminal = MessageRow(
                        id=ascending("message"),
                        session_id=session_id,
                        user_id=user_id,
                        role="assistant",
                        agent=trigger.agent,
                        model_id=trigger.model,
                        parent_id=trigger.id,
                        created_at=now,
                    )
                    db.add(terminal)
                    await db.flush()
                    await append_message_events_locked(
                        db,
                        session,
                        terminal,
                        operation="created",
                        run_fence=logical_run_fence,
                    )
                    terminal.finish = "aborted"
                    await append_message_events_locked(
                        db,
                        session,
                        terminal,
                        operation="updated",
                        run_fence=logical_run_fence,
                    )
                    messages.append(terminal)
                    closed_messages += 1
                    synthesized_terminal = True
            if not messages:
                if rejoin_only:
                    # Foreign keys normally make this impossible, but a
                    # descriptor-only convergence must still fail closed if a
                    # manually repaired/imported database has lost the parent
                    # message. Do not convert the surrounding Session to error.
                    release_status = restore_status or "idle"
                    await db.commit()
                    repair_completed = True
                    return RepairResult(
                        session_id=session_id,
                        rejoined_tasks=rejoined_tasks,
                        skipped=True,
                    )
                await db.commit()
                repair_completed = True
                return RepairResult(session_id=session_id)

            message_ids = [message.id for message in messages]
            parts = list(
                (
                    await db.execute(
                        select(PartRow)
                        .where(PartRow.message_id.in_(message_ids))
                        .order_by(PartRow.created_at, PartRow.id)
                    )
                )
                .scalars()
                .all()
            )
            by_message: dict[str, list[PartRow]] = {}
            for part in parts:
                by_message.setdefault(part.message_id, []).append(part)

            transcript_mutated = (
                bool(applied_handoffs.updates or applied_subagents.updates)
                or synthesized_terminal
            )
            for message in messages:
                message_parts = by_message.get(message.id, [])
                owns_recovered_turn = bool(
                    not rejoin_only
                    and recovered is not None
                    and recovered.trigger_message_id
                    and message.parent_id == recovered.trigger_message_id
                    and message.finish not in {"stop", "aborted"}
                    and not message.error
                )
                touched = (
                    message.id in updated_message_ids
                    or owns_recovered_turn
                    or (
                        rejoin_only
                        and message.id in delivery_message_ids
                        and message.finish not in {"stop", "aborted"}
                    )
                )
                step_starts: list[dict] = []
                step_finishes = 0
                for part in message_parts:
                    data = dict(part.data or {})
                    part_type = data.get("type") or part.type
                    if part_type == "step-start":
                        step_starts.append(data)
                    elif part_type == "step-finish":
                        step_finishes += 1
                    elif part_type == "tool":
                        status = getattr(
                            data.get("status"), "value", data.get("status")
                        )
                        if status not in {"pending", "running"}:
                            continue
                        code = (
                            TOOL_NOT_STARTED
                            if status == "pending"
                            else TOOL_OUTCOME_UNKNOWN
                        )
                        metadata = dict(data.get("metadata") or {})
                        metadata.update(
                            {
                                "recovery_code": code,
                                "recovered_at": now.isoformat(),
                            }
                        )
                        data.update(
                            {
                                "status": "error",
                                "error": _RECOVERY_ERRORS[code],
                                "metadata": metadata,
                            }
                        )
                        part.data = data
                        from session.agent_event_log import append_part_event_locked

                        await append_part_event_locked(
                            db,
                            session,
                            part,
                            message,
                            operation="updated",
                            run_fence=logical_run_fence,
                        )
                        repaired_tools += 1
                        touched = True
                        transcript_mutated = True

                # One assistant message normally owns one step, but close every
                # unmatched start deterministically if an older build emitted
                # more than one.
                for start in step_starts[step_finishes:]:
                    finish = StepFinishPart(
                        id=ascending("part"),
                        step=int(start.get("step") or 0),
                        session_id=session_id,
                        message_id=message.id,
                        duration=0.0,
                        snapshot=start.get("snapshot"),
                    )
                    finish_row = PartRow(
                        id=finish.id,
                        message_id=message.id,
                        session_id=session_id,
                        user_id=user_id,
                        type="step-finish",
                        data=finish.model_dump(),
                        created_at=now,
                    )
                    db.add(finish_row)
                    await db.flush()
                    from session.agent_event_log import append_part_event_locked

                    await append_part_event_locked(
                        db,
                        session,
                        finish_row,
                        message,
                        operation="created",
                        run_fence=logical_run_fence,
                    )
                    closed_steps += 1
                    touched = True
                    transcript_mutated = True

                if touched and message.finish not in {"stop", "aborted"}:
                    message.finish = "aborted"
                    from session.agent_event_log import append_message_events_locked

                    await append_message_events_locked(
                        db,
                        session,
                        message,
                        operation="updated",
                        run_fence=logical_run_fence,
                    )
                    closed_messages += 1
                    transcript_mutated = True

            if rejoin_only and not transcript_mutated:
                # The exact outbox was already committed and its message/step
                # tail is balanced. Only the descriptor needed convergence.
                release_status = restore_status or "idle"
                await db.commit()
                repair_completed = True
                return RepairResult(
                    session_id=session_id,
                    rejoined_tasks=rejoined_tasks,
                )
            # Do not expose a terminal Session status while this maintenance
            # generation is still live. The transcript commit and exact lease
            # assertion happen first; release(session_status=...) settles the
            # public status and Driver identity atomically below.

        await lease.assert_current()
        repair_completed = True
        return RepairResult(
            session_id=session_id,
            repaired_tools=repaired_tools,
            rejoined_tasks=rejoined_tasks,
            closed_steps=closed_steps,
            closed_messages=closed_messages,
        )
    finally:
        if repair_completed:
            try:
                if recovered is not None:
                    from agent.inbox import settle_claimed_inbox_items

                    await settle_claimed_inbox_items(
                        lease,
                        result_message_id=(
                            await _recovered_terminal_message_id(recovered)
                        ),
                        outcome="recovered",
                        error={
                            "code": "DRIVER_RECOVERED",
                            "message": "The interrupted Agent run was recovered.",
                        },
                    )
            except Exception:
                # Keep the maintenance identity discoverable. Releasing it
                # here would strand a claimed item beside an idle Driver.
                repair_completed = False
                log.exception(
                    "Could not settle recovered inbox session=%s",
                    session_id,
                )
        if repair_completed:
            try:
                matched = await _release_recovery_status(lease, release_status)
                if not matched:
                    log.warning(
                        "Recovery lease changed before settle session=%s",
                        session_id,
                    )
            except Exception:
                # The repaired transcript is durable but its maintenance
                # identity was not cleared. Expire that exact marker and let a
                # later pass re-check the tail rather than leaving Session
                # error/idle beside a live finalizing lease.
                log.exception(
                    "Could not settle completed repair session=%s",
                    session_id,
                )
                try:
                    await _preserve_recovery_status(lease)
                except Exception:
                    log.exception(
                        "Could not yield completed repair marker session=%s",
                        session_id,
                    )
        else:
            try:
                await _preserve_recovery_status(lease)
            except Exception:
                # The identity was never cleared. Even if the database is
                # unavailable here, its existing lease expires naturally and
                # a later periodic pass can retry the repair.
                log.exception(
                    "Could not yield failed repair marker session=%s",
                    session_id,
                )


async def repair_expired_sessions(
    records: list[RecoveredDriver],
) -> list[RepairResult]:
    """Repair recovered sessions one-by-one without replaying side effects."""
    results: list[RepairResult] = []
    for record in records:
        try:
            result = await repair_interrupted_session(
                record.session_id,
                record.user_id,
                recovered=record,
            )
            results.append(result)
        except Exception:
            log.exception("Agent tail repair failed session=%s", record.session_id)
    return results


async def reconcile_completed_task_handoffs(
    recovered_records: list[RecoveredDriver] | None = None,
    *,
    include_subagents: bool = True,
) -> list[str]:
    """Rejoin all ready outboxes through fenced transcript repair.

    A live parent generation makes ``repair_interrupted_session`` skip, so the
    original coroutine keeps priority. The completed outbox remains durable
    for a later recovery pass.
    """
    from agent.task_handoff import completed_task_parent_sessions
    from agent.subagent_runtime import ready_subagent_parent_sessions

    recovered_by_session = {
        record.session_id: record for record in (recovered_records or [])
    }
    repaired: list[str] = []
    candidates: dict[tuple[str, str], str] = {
        (session_id, user_id): status
        for session_id, user_id, status in await completed_task_parent_sessions()
    }
    if include_subagents:
        candidates.update(
            {
                (session_id, user_id): status
                for session_id, user_id, status in await ready_subagent_parent_sessions()
            }
        )
    for (session_id, user_id), previous_status in candidates.items():
        result = await repair_interrupted_session(
            session_id,
            user_id,
            recovered=recovered_by_session.get(session_id),
            rejoin_only=True,
            restore_status=previous_status,
        )
        if not result.skipped:
            repaired.append(session_id)
    return repaired


async def resume_claimable_subagent_activations() -> list[str]:
    """Claim, reserve, bind and cold-wake durable activation inbox rows."""
    from agent.driver import DriverBusyError, DriverRecoveryRequiredError
    from agent.subagent_runtime import (
        abandon_claim,
        bind_claimed_activation,
        claim_activation,
        claim_is_dispatchable,
        claimable_activation_ids,
    )

    resumed: list[str] = []
    for activation_id in await claimable_activation_ids():
        claim = await claim_activation(activation_id)
        if claim is None:
            continue
        if not await claim_is_dispatchable(claim):
            await abandon_claim(claim)
            continue
        try:
            lease = await reserve_run(
                claim.child_session_id,
                claim.user_id,
                trigger_message_id=claim.child_trigger_message_id,
            )
        except (DriverBusyError, DriverRecoveryRequiredError, LookupError):
            # Another exact Driver marker is still authoritative. Yield this
            # short activation claim; expired-driver recovery will bind/take it.
            await abandon_claim(claim)
            continue
        try:
            bound = await bind_claimed_activation(claim, lease)
            if not bound:
                await _release_recovery_status(lease, "error")
                continue
        except Exception:
            await _release_recovery_status(lease, "error")
            await abandon_claim(claim)
            continue
        task = asyncio.create_task(
            _run_recovered_prompt(lease, []),
            name=f"subagent-cold-resume:{lease.session_id}:{lease.generation}",
        )
        _track_resume_task(task, lease)
        resumed.append(activation_id)
    return resumed


async def resume_unbound_task_children() -> list[str]:
    """Wake descriptors persisted before a process died ahead of reserve."""
    from agent.task_handoff import (
        bind_task_handoff_child,
        unbound_task_handoffs,
    )

    resumed: list[str] = []
    for handoff in await unbound_task_handoffs():
        try:
            lease = await reserve_run(
                handoff.child_session_id,
                handoff.user_id,
                trigger_message_id=handoff.child_trigger_message_id,
            )
        except (DriverBusyError, LookupError):
            continue
        try:
            bound = await bind_task_handoff_child(
                handoff.id,
                lease,
                mode="unbound_recovery",
            )
            if not bound:
                raise RuntimeError("Task handoff stopped before recovery bind")
        except Exception:
            await _release_recovery_status(lease, "error")
            continue
        task = asyncio.create_task(
            _run_recovered_prompt(lease, []),
            name=f"task-orphan-resume:{lease.session_id}:{lease.generation}",
        )
        _track_resume_task(task, lease)
        resumed.append(lease.session_id)
    return resumed
