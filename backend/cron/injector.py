"""Result injection — inject cron results into the main session.

Handles overflow checking, compaction before injection, and BUSY queueing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import asynccontextmanager
from contextvars import ContextVar

_injection_run: ContextVar[str | None] = ContextVar("cron_injection_run", default=None)

from core.log import create_logger

log = create_logger("cron.injector")


async def try_inject_result(run_id: str, job: dict, result_text: str) -> bool:
    """Try to inject a cron result into the main session.

    If session is BUSY, the result stays in cron_runs with injected=False.
    It will be flushed when the session becomes IDLE (via flush_pending_cron_results).

    Returns True if injected immediately, False if queued.
    """
    session_id = job["session_id"]
    user_id = job["user_id"]

    # Check session status
    from session.session import get_session
    session = await get_session(session_id, user_id=user_id)
    if not session:
        log.warning(f"Cannot inject cron result: session {session_id} not found")
        return False

    status = session.status if isinstance(session.status, str) else session.status.value
    if status == "busy":
        log.info(f"Session {session_id} is BUSY, queueing cron result {run_id}")
        return False

    # Session is IDLE, inject directly
    async with _result_trace(run_id, session_id, user_id):
        return await _do_inject(run_id, job, result_text, session_id, user_id)


async def flush_pending_cron_results(session_id: str, user_id: str) -> int:
    """Flush all pending cron results into the session.

    Called from run_loop's finally block, BEFORE setting session to IDLE.
    Returns number of results injected.
    """
    from db.base import get_db_session
    from db.models.cron import CronRun, CronJob
    from sqlalchemy import select

    async with get_db_session() as db:
        result = await db.execute(
            select(CronRun)
            .where(
                CronRun.session_id == session_id,
                CronRun.status == "ok",
                CronRun.injected == False,
            )
            .order_by(CronRun.started_at.asc())
        )
        pending = result.scalars().all()

    if not pending:
        return 0

    log.info(f"Flushing {len(pending)} pending cron result(s) for session {session_id}")

    injected_count = 0
    from cron.i18n import is_silent

    for run in pending:
        try:
            # A silent result (NO_REPLY / empty) stays in run history only;
            # mark it consumed so it stops matching this query.
            if is_silent(run.summary_text):
                await _mark_injected(run.id)
                continue

            # Get job info for the name
            async with get_db_session() as db:
                job_result = await db.execute(
                    select(CronJob).where(CronJob.id == run.job_id)
                )
                job = job_result.scalar_one_or_none()

            job_name = job.name if job else "unknown"
            summary = run.summary_text

            async with _result_trace(run.id, session_id, user_id):
                # Check overflow before injection
                await _check_and_compact_if_needed(session_id, user_id, job_name, summary)

                # Inject the messages
                await _inject_messages(session_id, user_id, run.job_id, job_name, run.task_prompt or "", summary)

                # Mark as injected
                await _mark_injected(run.id)
                injected_count += 1

                # Publish injection event
                from bus import bus
                from bus.events import CRON_JOB_INJECTED
                bus.publish(CRON_JOB_INJECTED, {
                    "userId": user_id,
                    "sessionId": session_id,
                    "jobId": run.job_id,
                    "runId": run.id,
                    "jobName": job_name,
                })

        except Exception as e:
            from trajectory import TrajectoryError
            if isinstance(e, TrajectoryError):
                raise
            log.error(f"Failed to inject cron result {run.id}: {e}")

    return injected_count


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _do_inject(run_id: str, job: dict, result_text: str, session_id: str, user_id: str) -> bool:
    """Actually inject the result messages into the session."""
    job_name = job.get("name", "unnamed")
    task_prompt = job.get("task_prompt", "")

    try:
        # Check overflow
        await _check_and_compact_if_needed(session_id, user_id, job_name, result_text)

        # Inject messages
        await _inject_messages(session_id, user_id, job["id"], job_name, task_prompt, result_text)

        # Mark as injected
        await _mark_injected(run_id)

        # Publish event
        from bus import bus
        from bus.events import CRON_JOB_INJECTED
        bus.publish(CRON_JOB_INJECTED, {
            "userId": user_id,
            "sessionId": session_id,
            "jobId": job["id"],
            "runId": run_id,
            "jobName": job_name,
        })

        log.info(f"Injected cron result {run_id} into session {session_id}")
        return True

    except Exception as e:
        from trajectory import TrajectoryError
        if isinstance(e, TrajectoryError):
            raise
        log.error(f"Failed to inject cron result {run_id}: {e}")
        return False


async def _check_and_compact_if_needed(
    session_id: str, user_id: str, job_name: str, result_text: str
) -> None:
    """Check if injecting would overflow context, compact if needed."""
    from session.session import get_session
    from agent.compaction import is_overflow, get_model_context_limit
    from models.message import TokenUsage

    session = await get_session(session_id, user_id=user_id)
    if not session or not session.token_usage:
        return

    # Rough estimate: 1 token ≈ 4 chars
    inject_tokens = (len(job_name) + len(result_text)) // 4 + 100
    current_context = session.token_usage.context or 0
    limit = session.token_usage.limit or get_model_context_limit(session.model or "")

    # If injection would push past 90% of limit, compact first
    if current_context + inject_tokens > limit * 0.9:
        log.info(f"Compacting session {session_id} before cron injection (context={current_context}, inject~{inject_tokens}, limit={limit})")
        try:
            from agent.compaction import create_compaction, process_compaction
            from core.config import get_config
            from session.session import get_messages
            await create_compaction(session_id, auto=True, user_id=user_id)
            messages = await get_messages(session_id, user_id=user_id)
            model_id = session.model or get_config().model
            await process_compaction(session_id, messages, model_id, auto=True, user_id=user_id)
        except Exception as e:
            from trajectory import TrajectoryError
            if isinstance(e, TrajectoryError):
                raise
            log.warning(f"Pre-injection compaction failed: {e}")


async def _inject_messages(
    session_id: str, user_id: str, job_id: str,
    job_name: str, task_prompt: str, result_text: str
) -> None:
    """Create the synthetic user + assistant message pair in the session."""
    from session.session import create_user_message, create_assistant_message, save_part
    from models.message import TextPart
    from core.identifier import ascending
    from cron.i18n import resolve_locale, text

    locale = await resolve_locale(user_id)
    from trajectory import current, enabled
    if enabled(user_id) and current() is not None and _injection_run.get() is not None:
        await _inject_recorded_messages(session_id, user_id, job_id, job_name, task_prompt, result_text, locale)
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Synthetic user message: the task
    user_text = f"[{text(locale, 'scheduled_task')}: {job_name} | job_id: {job_id} | {now}]\n{task_prompt}"
    user_msg = await create_user_message(
        session_id=session_id,
        text=user_text,
        synthetic=True,
        user_id=user_id,
    )

    # Assistant message: the result
    assistant_info = await create_assistant_message(
        session_id=session_id,
        parent_id=user_msg.id,
        agent="cron",
        user_id=user_id,
    )

    # Add text part with the result
    text_part = TextPart(
        id=ascending("part"),
        text=result_text,
        channel="final",
        session_id=session_id,
        message_id=assistant_info.id,
    )
    await save_part(text_part, is_new=True, user_id=user_id)

    # Mark assistant as finished
    from session.session import update_message_info
    assistant_info.finish = "stop"
    await update_message_info(assistant_info, user_id=user_id)


async def _mark_injected(run_id: str) -> None:
    """Mark a cron_runs entry as injected."""
    from db.base import get_db_session
    from db.models.cron import CronRun
    from sqlalchemy import update

    now = datetime.now(timezone.utc)

    async with get_db_session() as db:
        run = await db.get(CronRun, run_id)
        if run is None or run.injected:
            return
        run.injected = True
        run.injected_at = now
        if run.trace_context:
            from trajectory import TraceContext, record
            await record("job.progress", {"job_id": run_id, "stage": "result_injected", "callback_index": 1},
                context=TraceContext.from_dict(run.trace_context), db=db, event_id=f"cron:{run_id}:injected")


@asynccontextmanager
async def _result_trace(run_id: str, session_id: str, user_id: str):
    """A queued callback always restores its persisted owner/turn, not its caller's."""
    from trajectory import TraceContext, bind, context_for_session, enabled
    from db.base import get_db_session
    from db.models.cron import CronRun
    from db.models.session import Session
    trace = None
    if enabled(user_id):
        with bind(None):
            from session.session import capture_trajectory_baseline_in_tx, prepare_trajectory_baseline_assets
            prepared_assets = await prepare_trajectory_baseline_assets(session_id, user_id,
                                                                         root_session_id=session_id)
            async with get_db_session() as db:
                run = await db.get(CronRun, run_id)
                if run is None or run.user_id != user_id or run.session_id != session_id:
                    raise ValueError("Cron callback does not match its recorded owner/session")
                if run.trace_context:
                    trace = TraceContext.from_dict(run.trace_context)
                    if trace.user_id != user_id or trace.session_id != session_id:
                        raise ValueError("Cron callback trajectory ownership mismatch")
                    trace = trace.derive(source_session_id=session_id, run_id=None, generation=None,
                                         step_id=None, request_id=None, call_id=None, part_id=None, message_id=None)
                else:
                    trace = (await context_for_session(db, user_id, session_id)).derive(turn_id=run_id)
                    await capture_trajectory_baseline_in_tx(db, trace, await db.get(Session, session_id),
                                                              prepared_assets=prepared_assets)
                    run.trace_context = trace.to_dict()
    with bind(trace):
        token = _injection_run.set(run_id)
        try:
            yield trace
        finally:
            _injection_run.reset(token)


async def _inject_recorded_messages(session_id, user_id, job_id, job_name, task_prompt, result_text, locale):
    """Commit callback consumption, both chat messages and facts atomically."""
    from cron.i18n import text
    from core.identifier import ascending
    from question import runtime
    from db.models.cron import CronRun
    from db.models.message import Message as MessageORM
    from db.models.part import Part as PartORM
    from models.message import TextPart, MessageWithParts, id_to_iso
    from session.session import record_projection_in_tx
    from trajectory import current, record
    from bus import bus
    from bus.events import MESSAGE_CREATED

    run_id = _injection_run.get()
    stamp = datetime.now(timezone.utc)
    user_text = f"[{text(locale, 'scheduled_task')}: {job_name} | job_id: {job_id} | {stamp:%Y-%m-%d %H:%M UTC}]\n{task_prompt}"
    user_message_id = ascending("message")
    assistant_message_id = ascending("message")
    user_part = TextPart(text=user_text, synthetic=True, session_id=session_id, message_id=user_message_id)
    answer_part = TextPart(text=result_text, channel="final", session_id=session_id, message_id=assistant_message_id)
    async with runtime.transaction(session_id, user_id) as (db, _, _execution):
        run = await db.get(CronRun, run_id)
        if run is None or run.user_id != user_id or run.session_id != session_id:
            raise ValueError("Cron callback owner changed")
        if run.injected:
            return
        db.add(MessageORM(id=user_message_id, session_id=session_id, user_id=user_id, role="user",
                          summary=False, client_message_id=f"cron:{run_id}", created_at=stamp))
        db.add(MessageORM(id=assistant_message_id, session_id=session_id, user_id=user_id, role="assistant",
                          summary=False, parent_id=user_message_id, agent="cron", finish="stop", created_at=stamp))
        for part in (user_part, answer_part):
            db.add(PartORM(id=part.id, message_id=part.message_id, session_id=session_id, user_id=user_id,
                          type="text", data=part.model_dump(), created_at=stamp))
        await record("input.injected", {"text": user_text, "synthetic": True, "source": "cron",
            "job_id": run_id}, context=current(), db=db, message_id=user_message_id, part_id=user_part.id,
            event_id=f"cron:{run_id}:input")
        for message_id, role, parent_id, part in ((user_message_id, "user", None, user_part),
                (assistant_message_id, "assistant", user_message_id, answer_part)):
            await record_projection_in_tx(db, session_id, user_id, "message.committed", {"message": {
                "id": message_id, "role": role, "parent_id": parent_id, "finish": "stop" if role == "assistant" else None},
                "operation": "created"}, message_id=message_id)
            await record_projection_in_tx(db, session_id, user_id, "part.committed", {
                "part": part.model_dump(), "operation": "created"}, message_id=message_id, part_id=part.id)
        run.injected = True
        run.injected_at = stamp
        await record("job.progress", {"job_id": run_id, "stage": "result_injected", "callback_index": 1,
            "message_id": assistant_message_id}, context=current(), db=db, event_id=f"cron:{run_id}:injected")
    for message_id, role, parent_id, part in ((user_message_id, "user", None, user_part),
            (assistant_message_id, "assistant", user_message_id, answer_part)):
        message = MessageWithParts(id=message_id, session_id=session_id, role=role, parts=[part],
            created_at=id_to_iso(message_id), parent_id=parent_id, agent="cron" if role == "assistant" else None,
            finish="stop" if role == "assistant" else None)
        bus.publish(MESSAGE_CREATED, {"userId": user_id, "sessionId": session_id, "message": message.model_dump()})
