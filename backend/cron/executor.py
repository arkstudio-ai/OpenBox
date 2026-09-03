"""Cron job executor — runs agent in a temporary session.

Flow: summary(main session) → create temp session → run_loop → extract result → write cron_runs.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from core.log import create_logger
from core.identifier import ascending

log = create_logger("cron.executor")


async def execute_cron_job(job: dict) -> dict:
    """Execute a single cron job. Called by the timer.

    Returns {"status": "ok"|"error"|"skipped", "error"?: str, "summary_text"?: str, ...}
    """
    job_id = job["id"]
    if not job.get("workspace_id"):
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import select

        async with get_db_session() as db:
            workspace_id = (
                await db.execute(
                    select(CronJob.workspace_id).where(CronJob.id == job_id)
                )
            ).scalar_one_or_none()
        if workspace_id:
            job = {**job, "workspace_id": workspace_id}
    user_id = job["user_id"]
    session_id = job.get("session_id")
    job_name = job.get("name", "unnamed")

    log.info(f"Executing cron job {job_id} ({job_name}) for session {session_id}")

    # Publish start event
    from bus import bus
    from bus.events import CRON_JOB_STARTED
    bus.publish(CRON_JOB_STARTED, {
        "userId": user_id,
        "jobId": job_id,
        "sessionId": session_id,
        "jobName": job_name,
    })

    started_at = datetime.now(timezone.utc)
    run_id = ascending("cron_run")

    # Create cron_runs entry with status=running
    await _create_run_entry(run_id, job, started_at)

    from cron.i18n import is_silent, resolve_locale

    locale = await resolve_locale(user_id)
    temp_session_id = None
    try:
        # A missing per-desktop route is a scheduling skip, not an agent
        # failure. Check it before summary generation so the skip spends no
        # model tokens and reaches no retry path.
        from sandbox import provider

        if provider.routes_per_user:
            from sandbox.ownership import owner_for

            await provider.resolve_user_container(await owner_for(user_id))

        # 1. Generate session summary (or reuse cache)
        context_summary = await _get_session_summary(job)

        # 2. Create temporary session
        temp_session_id = await _create_temp_session(job, locale)

        # 3. Build prompt and inject into temp session
        prompt = _build_cron_prompt(job, context_summary, locale)
        await _inject_prompt(temp_session_id, user_id, prompt)

        # 4. Acquire sandbox and run agent loop
        result_text = await _run_agent_loop(temp_session_id, user_id, job, locale)

        # 5. Extract result
        ended_at = datetime.now(timezone.utc)
        duration_ms = int((ended_at - started_at).total_seconds() * 1000)
        tokens = await _collect_token_usage(temp_session_id, user_id)
        silent = is_silent(result_text)

        # Update cron_runs. A silent run is marked injected up front — there
        # is nothing to flush into the main session later.
        await _update_run_entry(
            run_id, job_id, temp_session_id,
            status="ok",
            summary_text=result_text,
            context_summary=context_summary,
            ended_at=ended_at,
            duration_ms=duration_ms,
            tokens=tokens,
            injected=True if silent else None,
        )

        # Publish success event
        from bus.events import CRON_JOB_COMPLETED
        bus.publish(CRON_JOB_COMPLETED, {
            "userId": user_id,
            "jobId": job_id,
            "sessionId": session_id,
            "jobName": job_name,
            "runId": run_id,
            "durationMs": duration_ms,
            "silent": silent,
        })

        # 6. Append to the project's daily run log (every run, silent included)
        from cron.runlog import append_run_log, build_log_entry
        await append_run_log(
            temp_session_id, user_id, job,
            build_log_entry(
                job, "ok", result_text, duration_ms,
                tokens.get("total_tokens", 0), silent, locale,
            ),
            locale,
        )

        if silent:
            # NO_REPLY: keep the run history, spare the conversation and the
            # delivery channel.
            log.info(f"Cron job {job_id} completed silently in {duration_ms}ms")
            return {"status": "ok", "summary_text": result_text, "run_id": run_id}

        # 7. Post the result into the notify conversation, when the job has
        # one; page-created jobs live entirely in run history + the daily log.
        if job.get("session_id"):
            from cron.injector import try_inject_result
            await try_inject_result(run_id, job, result_text)
        else:
            await _update_run_entry(
                run_id, job_id, temp_session_id, status="ok", injected=True,
                ended_at=ended_at, duration_ms=duration_ms,
            )

        # 8. External delivery (webhook), best-effort
        await _dispatch_delivery(job, "ok", result_text, duration_ms)

        log.info(f"Cron job {job_id} completed in {duration_ms}ms")
        return {"status": "ok", "summary_text": result_text, "run_id": run_id}

    except asyncio.CancelledError:
        # The timer's wait_for timed out (or shutdown cancelled us). Close the
        # run record here — the except-Exception path never sees CancelledError,
        # which used to leave the row in status=running until the next restart.
        ended_at = datetime.now(timezone.utc)
        duration_ms = int((ended_at - started_at).total_seconds() * 1000)
        await _update_run_entry(
            run_id, job_id, temp_session_id,
            status="error",
            error_message="Job execution timed out or was cancelled",
            ended_at=ended_at,
            duration_ms=duration_ms,
        )
        from bus.events import CRON_JOB_FAILED
        bus.publish(CRON_JOB_FAILED, {
            "userId": user_id,
            "jobId": job_id,
            "sessionId": session_id,
            "jobName": job_name,
            "error": "timeout",
        })
        raise

    except Exception as e:
        from sandbox.wuying_desktop_service import DesktopNotReady

        ended_at = datetime.now(timezone.utc)
        duration_ms = int((ended_at - started_at).total_seconds() * 1000)
        if isinstance(e, DesktopNotReady):
            reason = f"DESKTOP_NOT_READY: {e.payload.get('state', 'not_ready')}"
            await _update_run_entry(
                run_id,
                job_id,
                temp_session_id,
                status="skipped",
                error_message=reason,
                ended_at=ended_at,
                duration_ms=duration_ms,
            )
            log.info("Cron job %s skipped without retry: %s", job_id, reason)
            return {"status": "skipped", "error": reason, "run_id": run_id}
        error_msg = str(e)

        await _update_run_entry(
            run_id, job_id, temp_session_id,
            status="error",
            error_message=error_msg,
            ended_at=ended_at,
            duration_ms=duration_ms,
        )

        from bus.events import CRON_JOB_FAILED
        bus.publish(CRON_JOB_FAILED, {
            "userId": user_id,
            "jobId": job_id,
            "sessionId": session_id,
            "jobName": job_name,
            "error": error_msg,
        })

        # Failures always deliver (a broken monitor is itself an alert).
        await _dispatch_delivery(job, "error", error_msg, duration_ms)

        # Failed runs go into the daily log too — the file is the audit trail.
        if temp_session_id:
            from cron.runlog import append_run_log, build_log_entry
            await append_run_log(
                temp_session_id, user_id, job,
                build_log_entry(job, "error", error_msg, duration_ms, 0, False, locale),
                locale,
            )

        log.error(f"Cron job {job_id} failed after {duration_ms}ms: {error_msg}")
        return {"status": "error", "error": error_msg, "run_id": run_id}

    finally:
        # Drop only this process's temp-session binding. Global sandbox
        # lifetime is decided by the database-guarded idle reaper, not a local
        # session reference count that cannot see worker/API replicas.
        if temp_session_id:
            try:
                from sandbox import sandbox_manager
                await sandbox_manager.release(temp_session_id, user_id=user_id)
            except Exception as e:
                log.debug(f"Sandbox release for {temp_session_id} failed: {e}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_session_summary(job: dict) -> str:
    """Get or generate a summary of the notify session's conversation history.

    Jobs created from the management page have no session — they run on the
    project alone, with the cron/ run log as their cross-run context.
    """
    session_id = job.get("session_id")
    if not session_id:
        return ""
    cached_summary = job.get("summary_cache")
    cached_msg_id = job.get("summary_cache_msg_id")

    # Check if cache is still valid
    from session.session import get_messages
    messages = await get_messages(session_id, user_id=job["user_id"])
    if not messages:
        return ""

    latest_msg_id = messages[-1].id if messages else None

    # Cache hit
    if cached_summary and cached_msg_id == latest_msg_id:
        log.debug(f"Summary cache hit for session {session_id}")
        return cached_summary

    # Check if there's a recent compaction summary we can reuse
    for msg in reversed(messages):
        if getattr(msg, "summary", False) and msg.role == "assistant":
            for part in (msg.parts or []):
                p = part if isinstance(part, dict) else (part.model_dump() if hasattr(part, "model_dump") else {})
                text = p.get("text", "")
                if text and len(text) > 50:
                    log.debug(f"Reusing compaction summary for session {session_id}")
                    # Update cache
                    await _update_summary_cache(job["id"], text, latest_msg_id)
                    return text

    # Generate new summary using LLM (small/cheap model)
    try:
        summary = await _generate_summary(messages, job)
        if summary:
            await _update_summary_cache(job["id"], summary, latest_msg_id)
            return summary
    except Exception as e:
        log.warning(f"Summary generation failed for session {session_id}: {e}")

    # Fallback: no summary
    return ""


async def _generate_summary(messages, job: dict) -> str:
    """Generate a summary of conversation history using LLM."""
    from agent.loop import _to_llm_messages
    from agent.llm import stream_llm

    llm_messages = _to_llm_messages(messages)

    # Truncate to last ~20 messages to keep summary request small
    if len(llm_messages) > 20:
        llm_messages = llm_messages[-20:]

    prompt = (
        "Summarize the conversation above in 200 words or less. "
        "Focus on: 1) What the user asked for, 2) What was accomplished, "
        "3) Key files or resources involved, 4) Any pending tasks."
    )
    llm_messages.append({"role": "user", "content": prompt})

    # Summary model: deployment override > job model > deployment default.
    # (The old hardcoded "openai/gpt-4o-mini" broke on deployments that never
    # configured an OpenAI provider.)
    from core.config import get_config
    config = get_config()
    model_id = config.cron_summary_model or job.get("model") or config.model

    summary = ""
    try:
        from tool.tool import ToolContext

        # Same shape as compaction's summarizer call: a bare context is enough
        # for a tool-less LLM turn. (This call used to omit ctx entirely, so
        # summary generation had never actually succeeded.)
        ctx = ToolContext(session_id=job["session_id"], user_id=job["user_id"])
        async for event in stream_llm(
            agent_def=None,
            system=[],
            messages=llm_messages,
            tools={},
            model_id=model_id,
            ctx=ctx,
        ):
            if event["type"] == "text_delta":
                summary += event["text"]
            elif event["type"] == "error":
                break
    except Exception as e:
        log.warning(f"Summary LLM call failed: {e}")

    return summary


async def _update_summary_cache(job_id: str, summary: str, msg_id: str | None) -> None:
    """Update the summary cache on the cron job."""
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import update

    async with get_db_session() as db:
        await db.execute(
            update(CronJob)
            .where(CronJob.id == job_id)
            .values(summary_cache=summary, summary_cache_msg_id=msg_id)
        )


async def _create_temp_session(job: dict, locale: str = "zh-CN") -> str:
    """Create a temporary session for cron execution."""
    from session.session import create_session
    from core.config import get_config

    # Resolve model: job override > notify session model > config default
    model = job.get("model") or ""
    if not model and job.get("session_id"):
        from session.session import get_session
        main_session = await get_session(job["session_id"], user_id=job["user_id"])
        if main_session and main_session.model:
            model = main_session.model
    if not model:
        config = get_config()
        model = config.model or ""

    # Jobs are project-scoped; the run executes in the owning project's
    # directory regardless of which conversation (if any) gets the result.
    project_id = job.get("project_id") or None

    from cron.i18n import text

    session = await create_session(
        user_id=job["user_id"],
        workspace_id=job.get("workspace_id", ""),
        agent=job.get("agent", "build"),
        model=model,
        title=text(locale, "temp_title", name=job.get("name", "task")),
        parent_id=job["session_id"],
        project_id=project_id,
        kind="cron",
    )
    return session.id


def _build_cron_prompt(job: dict, context_summary: str, locale: str = "zh-CN") -> str:
    """Build the prompt for cron execution."""
    from cron.i18n import text

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Instruction first, background last. With the summary leading, its
    # narrative ("task created, runs already reported X, no action required")
    # framed the whole prompt as a status update — models replied with an
    # acknowledgment or a bare NO_REPLY in ~6s without a single tool call.
    parts = [
        f"[{text(locale, 'scheduled_task')}: {job.get('name', 'unnamed')} | "
        f"job_id: {job['id']} | {now}]\n"
        f"{text(locale, 'execute_now')}\n{job['task_prompt']}",
        text(locale, "execute_first"),
    ]

    if context_summary:
        parts.append(
            f"[{text(locale, 'context_summary')}]\n"
            f"{text(locale, 'context_note')}\n{context_summary}"
        )

    parts.append(text(locale, "runlog_hint"))
    parts.append(text(locale, "silent_instruction"))

    return "\n\n".join(parts)


async def _inject_prompt(temp_session_id: str, user_id: str, prompt: str) -> None:
    """Inject the cron prompt as a user message in the temp session."""
    from session.session import create_user_message
    await create_user_message(
        session_id=temp_session_id,
        text=prompt,
        synthetic=True,
        user_id=user_id,
    )


async def _run_agent_loop(temp_session_id: str, user_id: str, job: dict, locale: str = "zh-CN") -> str:
    """Run the agent loop in the temp session and extract the result."""
    from sandbox import sandbox_manager

    # Ensure sandbox is available (may create container)
    try:
        sandbox = await sandbox_manager.get_client(temp_session_id, user_id=user_id)
    except Exception as e:
        log.warning(f"Sandbox acquisition failed for cron job {job['id']}: {e}")
        # Try to continue without sandbox (some tools may not work)
        sandbox = None

    # Run the agent loop
    from agent.loop import run_loop
    result_msg = await run_loop(temp_session_id, user_id)

    log.info(f"run_loop completed for temp session {temp_session_id}")

    # Check if the last assistant message has an error
    from session.session import get_messages as _get_temp_msgs
    temp_msgs = await _get_temp_msgs(temp_session_id)
    for msg in reversed(temp_msgs):
        role = msg.role if isinstance(msg.role, str) else msg.role.value
        if role == "assistant" and getattr(msg, "error", None):
            error_info = msg.error
            error_msg = error_info.get("message", str(error_info)) if isinstance(error_info, dict) else str(error_info)
            raise RuntimeError(f"Agent error: {error_msg}")

    # Extract all assistant text parts as the result
    # Agent may produce text across multiple assistant messages (between tool calls)
    from session.session import get_messages
    messages = await get_messages(temp_session_id, user_id=user_id)

    text_parts = []
    for msg in messages:
        role = msg.role if isinstance(msg.role, str) else msg.role.value
        if role != "assistant":
            continue
        for part in (msg.parts or []):
            p = part if isinstance(part, dict) else (part.model_dump() if hasattr(part, "model_dump") else {})
            if p.get("type") == "text" and p.get("text", "").strip():
                text_parts.append(p["text"].strip())

    if text_parts:
        # Use the last substantial text part as the primary result
        return text_parts[-1]

    # Fallback: check if there are any tool outputs we can summarize
    tool_outputs = []
    for msg in messages:
        role = msg.role if isinstance(msg.role, str) else msg.role.value
        if role != "assistant":
            continue
        for part in (msg.parts or []):
            p = part if isinstance(part, dict) else (part.model_dump() if hasattr(part, "model_dump") else {})
            if p.get("type") == "tool" and p.get("output"):
                output = p["output"]
                if isinstance(output, str) and len(output) > 10:
                    tool_outputs.append(output[:200])

    if tool_outputs:
        return "Tool execution completed:\n" + "\n---\n".join(tool_outputs[-3:])

    # Nothing textual at all: treated as a silent run (recorded, not injected).
    return ""


async def _collect_token_usage(temp_session_id: str, user_id: str) -> dict:
    """Cumulative token usage of the run's temp session.

    The temp session is fresh per run, so its session-level counters — which
    the agent loop accumulates per step — are exactly this run's spend.
    """
    try:
        from session.session import get_session

        session = await get_session(temp_session_id, user_id=user_id)
        usage = getattr(session, "token_usage", None) if session else None
        if not usage:
            return {}
        return {
            "input_tokens": usage.input or 0,
            "output_tokens": usage.output or 0,
            "total_tokens": usage.total or (usage.input or 0) + (usage.output or 0),
        }
    except Exception as e:
        log.warning(f"Token usage collection failed for {temp_session_id}: {e}")
        return {}


async def _dispatch_delivery(job: dict, status: str, summary_text: str | None, duration_ms: int) -> None:
    """Send the run result to the job's external delivery target, if any."""
    delivery = job.get("delivery") or {}
    if not delivery or delivery.get("mode", "none") == "none":
        return
    try:
        from cron.delivery import dispatch_delivery

        result = await dispatch_delivery(
            delivery,
            job_name=job.get("name", "unnamed"),
            job_id=job["id"],
            status=status,
            summary_text=summary_text,
            duration_ms=duration_ms,
        )
        if not result.success:
            log.warning(f"Delivery failed for cron job {job['id']}: {result.error}")
    except Exception as e:
        log.warning(f"Delivery dispatch error for cron job {job['id']}: {e}")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _create_run_entry(run_id: str, job: dict, started_at: datetime) -> None:
    """Create a cron_runs entry with status=running."""
    from db.base import get_db_session
    from db.models.cron import CronRun

    async with get_db_session() as db:
        row = CronRun(
            id=run_id,
            job_id=job["id"],
            user_id=job["user_id"],
            project_id=job.get("project_id"),
            session_id=job.get("session_id"),
            status="running",
            task_prompt=job.get("task_prompt"),
            started_at=started_at,
        )
        db.add(row)


async def _update_run_entry(
    run_id: str,
    job_id: str,
    temp_session_id: str | None,
    status: str,
    summary_text: str | None = None,
    context_summary: str | None = None,
    error_message: str | None = None,
    ended_at: datetime | None = None,
    duration_ms: int = 0,
    tokens: dict | None = None,
    injected: bool | None = None,
) -> None:
    """Update a cron_runs entry with final results."""
    from db.base import get_db_session
    from db.models.cron import CronRun
    from sqlalchemy import update

    values: dict = {
        "status": status,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
    }
    if temp_session_id:
        values["temp_session_id"] = temp_session_id
    if summary_text is not None:
        values["summary_text"] = summary_text
    if context_summary is not None:
        values["context_summary"] = context_summary
    if error_message is not None:
        values["error_message"] = error_message
    if tokens:
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            if tokens.get(key):
                values[key] = tokens[key]
    if injected is not None:
        values["injected"] = injected
        if injected:
            values["injected_at"] = datetime.now(timezone.utc)

    async with get_db_session() as db:
        await db.execute(
            update(CronRun).where(CronRun.id == run_id).values(**values)
        )
