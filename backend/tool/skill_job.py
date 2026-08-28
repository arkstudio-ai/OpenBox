"""The single generic agent surface for skill jobs (§8).

`start` succeeds when the job is durably admitted — never when the external
work finishes. The agent reports the job id and ends its turn; the job card
keeps updating without it. `wait` is bounded and budgeted per turn so a model
cannot fall back into poll loops.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Literal

from pydantic import BaseModel, model_validator

from core.log import create_logger
from tool.tool import ToolContext, ToolInfo, ToolResult, define_tool

log = create_logger("tool.skill_job")

WAIT_BUDGET_PER_TURN = 2
WAIT_MAX_SECONDS = 15
_WAIT_POLL_SECONDS = 0.5

#: (session_id, message_id) -> wait calls spent this turn. Advisory in-process
#: state: a lost entry only re-opens the budget, never breaks correctness.
_wait_budget: dict[tuple[str, str], int] = {}


class SkillJobParams(BaseModel):
    action: Literal["start", "get", "wait", "cancel", "resume", "result"]
    skill: str = ""
    operation: str = ""
    input: dict = {}
    #: Optional explicit domain key ("run it again"); the server always keeps
    #: its own tool-call-derived key, so a model cannot duplicate a job by
    #: retrying the same call.
    idempotency_key: str = ""
    job_id: str = ""
    wait_seconds: int = 10

    @model_validator(mode="after")
    def _check(self):
        if self.action == "start":
            if not self.skill or not self.operation:
                raise ValueError("start requires skill and operation")
        elif not self.job_id:
            raise ValueError(f"{self.action} requires job_id")
        return self


def _job_lines(snapshot: dict, *, background: bool = False) -> list[str]:
    lines = [
        f"job_id={snapshot['jobId']}",
        f"status={snapshot['status']}",
    ]
    if snapshot.get("phase"):
        lines.append(f"phase={snapshot['phase']}")
    progress = snapshot.get("progress") or {}
    if progress:
        lines.append(f"progress={json.dumps(progress, ensure_ascii=False)}")
    if snapshot.get("errorCode"):
        lines.append(f"error_code={snapshot['errorCode']}")
    if snapshot.get("errorMessage"):
        lines.append(f"error={snapshot['errorMessage']}")
    if background:
        lines.append("background=true")
        lines.append("The job continues without you; report the job_id and finish your turn.")
    return lines


def _notify_local_worker() -> None:
    try:
        from skill_runtime.embedded import notify_worker

        notify_worker()
    except Exception:
        pass


async def _execute(args: SkillJobParams, ctx: ToolContext) -> ToolResult:
    from skill_runtime import repository as repo, service
    from skill_runtime.manifest import ManifestError
    from skill_runtime.repository import IdempotencyConflict, JobNotFound
    from skill_runtime.types import TERMINAL_STATUSES, JobStatus

    user_id = ctx.user_id or "default"
    call_key = ctx.part_id or uuid.uuid4().hex

    if args.action == "start":
        try:
            job, created = await service.start_job(
                user_id=user_id,
                skill_key=args.skill,
                operation=args.operation,
                input_data=args.input,
                idempotency_key=args.idempotency_key or f"toolcall:{call_key}",
                session_id=ctx.session_id or None,
                project_id=ctx.project_id or None,
            )
        except service.UnknownSkill:
            return ToolResult(title="Unknown skill", output=f"No skill named {args.skill!r} is available.")
        except service.UnknownOperation as e:
            return ToolResult(title="Unknown operation", output=str(e))
        except service.SkillDisabled as e:
            return ToolResult(title="Skill disabled", output=str(e))
        except ManifestError as e:
            return ToolResult(title="Invalid input", output=str(e))
        except IdempotencyConflict as e:
            return ToolResult(title="Idempotency conflict", output=str(e))
        _notify_local_worker()
        snapshot = service.job_snapshot(job)
        return ToolResult(
            title=("Job admitted" if created else "Job already admitted"),
            output="\n".join(_job_lines(snapshot, background=True)),
            metadata={"job_id": job.id, "status": job.status, "created": created},
        )

    if args.action == "cancel":
        try:
            job = await repo.request_cancel(args.job_id, user_id)
        except JobNotFound:
            return ToolResult(title="Job not found", output="No owned job has that job_id.")
        _notify_local_worker()
        snapshot = service.job_snapshot(job)
        return ToolResult(
            title="Cancel requested",
            output="\n".join(_job_lines(snapshot)),
            metadata={"job_id": job.id, "status": job.status},
        )

    if args.action == "resume":
        try:
            row, created = await repo.add_input(
                args.job_id,
                user_id,
                kind="user_answer",
                payload=args.input,
                idempotency_key=args.idempotency_key or f"toolcall:{call_key}",
                source_event_id=call_key,
            )
        except JobNotFound:
            return ToolResult(title="Job not found", output="No owned job has that job_id.")
        _notify_local_worker()
        job = await repo.get_job(args.job_id, user_id)
        snapshot = service.job_snapshot(job)
        return ToolResult(
            title=("Input delivered" if created else "Input already delivered"),
            output="\n".join(_job_lines(snapshot)),
            metadata={"job_id": args.job_id, "input_id": row.id, "created": created},
        )

    job = await repo.get_job(args.job_id, user_id)
    if job is None:
        return ToolResult(title="Job not found", output="No owned job has that job_id.")

    if args.action == "wait":
        budget_key = (ctx.session_id or "", ctx.message_id or "")
        spent = _wait_budget.get(budget_key, 0)
        if spent >= WAIT_BUDGET_PER_TURN:
            snapshot = service.job_snapshot(job)
            return ToolResult(
                title="Wait budget exhausted",
                output="\n".join(
                    _job_lines(snapshot, background=True)
                    + ["wait_budget_exhausted=true"]
                ),
                metadata={"job_id": job.id, "status": job.status, "wait_budget_exhausted": True},
            )
        _wait_budget[budget_key] = spent + 1
        if len(_wait_budget) > 512:
            for stale in list(_wait_budget)[:256]:
                _wait_budget.pop(stale, None)

        deadline = asyncio.get_running_loop().time() + min(max(args.wait_seconds, 1), WAIT_MAX_SECONDS)
        while (
            JobStatus(job.status) not in TERMINAL_STATUSES
            and job.status != JobStatus.WAITING_USER.value
            and asyncio.get_running_loop().time() < deadline
            and not ctx.abort.is_set()
        ):
            await asyncio.sleep(_WAIT_POLL_SECONDS)
            job = await repo.get_job(args.job_id, user_id)

    snapshot = service.job_snapshot(job)
    lines = _job_lines(snapshot, background=JobStatus(job.status) not in TERMINAL_STATUSES)

    if args.action == "result" or JobStatus(job.status) in TERMINAL_STATUSES:
        result = snapshot.get("result") or {}
        if result:
            lines.append(f"result={json.dumps(result, ensure_ascii=False)}")
        artifacts = await repo.list_artifacts(job.id, user_id)
        for artifact in artifacts:
            lines.append(
                f"artifact={artifact['assetId']} role={artifact['role']} name={artifact['name']}"
            )
    if job.status == JobStatus.WAITING_USER.value:
        events = await repo.get_events(job.id, user_id)
        prompts = [e.payload.get("prompt") for e in events if e.event_type == "job.waiting_user"]
        if prompts and prompts[-1]:
            lines.append(f"waiting_user_prompt={prompts[-1]}")
            lines.append("Relay the question; answers arrive via action=resume or the job card.")

    return ToolResult(
        title=f"Job {job.status}",
        output="\n".join(lines),
        metadata={"job_id": job.id, "status": job.status},
    )


skill_job_tool: ToolInfo = define_tool(
    "skill_job",
    description=(
        "Start and control durable background skill jobs. Actions: "
        "start (admit a job; it runs without you — report the job_id and end your turn), "
        "get (authoritative snapshot), wait (bounded, at most twice per turn), "
        "cancel (request cancellation), resume (deliver the user's answer to a waiting job), "
        "result (final result and artifacts). Never poll in a loop; the platform "
        "delivers progress to the user directly."
    ),
    parameters=SkillJobParams,
    execute=_execute,
    sandbox_required=False,
    skill_only=True,
)
