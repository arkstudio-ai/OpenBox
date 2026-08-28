"""The single generic agent surface for skill jobs (§8).

`start` succeeds when the job is durably admitted — never when the external
work finishes. The agent reports the job id and ends its turn; the job card
keeps updating without it. `wait` is bounded and budgeted per turn so a model
cannot fall back into poll loops.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from core.log import create_logger
from tool.tool import ToolContext, ToolInfo, ToolResult, define_tool

log = create_logger("tool.skill_job")

WAIT_BUDGET_PER_TURN = 2
WAIT_MAX_SECONDS = 15
_WAIT_POLL_SECONDS = 0.5

#: (session_id, message_id) -> wait calls spent this turn. Advisory in-process
#: state: a lost entry only re-opens the budget, never breaks correctness.
_wait_budget: dict[tuple[str, str], int] = {}


def _tool_idempotency_key(call_key: str, domain_key: str) -> str:
    """Use a declared domain identity across tool calls, else the call id.

    Retrying the same persisted tool call already reuses ``part_id``. A Skill
    that supplies a stable domain key needs the stronger property that a second
    tool call cannot create another platform Job for the same logical work.
    """
    if domain_key:
        digest = hashlib.sha256(domain_key.encode("utf-8")).hexdigest()[:32]
        return f"domain:{digest}"
    return f"toolcall:{call_key}"


class SkillJobParams(BaseModel):
    action: Literal["start", "get", "wait", "cancel", "resume", "result"]
    skill: str = ""
    operation: str = ""
    input: dict = Field(default_factory=dict)
    #: Optional stable domain identity. When present it deduplicates the same
    #: logical operation across distinct tool calls; otherwise the persisted
    #: tool-call id makes retries of this one call idempotent.
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
    from skill_runtime.embedded import notify_worker

    notify_worker()


#: String-level terminal check: a rolling deploy may surface a status value
#: this process's enum does not know yet; treat it as non-terminal instead of
#: crashing the tool with a ValueError.
_TERMINAL_VALUES = frozenset({"succeeded", "failed", "cancelled"})


def _is_terminal(status: str) -> bool:
    return status in _TERMINAL_VALUES


async def _execute(args: SkillJobParams, ctx: ToolContext) -> ToolResult:
    from skill_runtime import repository as repo, service
    from skill_runtime.manifest import ManifestError
    from skill_runtime.repository import IdempotencyConflict, InputNotAllowed, JobNotFound
    from skill_runtime.types import JobStatus

    user_id = ctx.user_id or "default"
    call_key = ctx.part_id or uuid.uuid4().hex

    if args.action == "start":
        if args.skill not in ctx.active_skills:
            return ToolResult(
                title="Skill not activated",
                output=(
                    f"Load the instruction skill that declares {args.skill!r} in this "
                    "agent run before starting its background operation."
                ),
            )
        derived_key = _tool_idempotency_key(call_key, args.idempotency_key)
        try:
            job, created = await service.start_job(
                user_id=user_id,
                skill_key=args.skill,
                operation=args.operation,
                input_data=args.input,
                idempotency_key=derived_key,
                session_id=ctx.session_id or None,
                project_id=ctx.project_id or None,
            )
        except service.UnknownSkill:
            return ToolResult(title="Unknown skill", output=f"No skill named {args.skill!r} is available.")
        except service.UnknownOperation as e:
            return ToolResult(title="Unknown operation", output=str(e))
        except service.SkillDisabled as e:
            return ToolResult(title="Skill disabled", output=str(e))
        except service.InvalidScope as e:
            return ToolResult(title="Invalid job scope", output=str(e))
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
        resume_key = _tool_idempotency_key(call_key, args.idempotency_key)
        try:
            row, created = await repo.add_input(
                args.job_id,
                user_id,
                kind="user_answer",
                payload=args.input,
                idempotency_key=resume_key,
                source_event_id=call_key,
            )
        except JobNotFound:
            return ToolResult(title="Job not found", output="No owned job has that job_id.")
        except InputNotAllowed as e:
            return ToolResult(title="Input not accepted", output=str(e))
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
        budget_key = (
            ctx.session_id or "",
            ctx.run_id or ctx.message_id or ctx.part_id or call_key,
        )
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
            not _is_terminal(job.status)
            and job.status != JobStatus.WAITING_USER.value
            and asyncio.get_running_loop().time() < deadline
            and not ctx.abort.is_set()
        ):
            await asyncio.sleep(_WAIT_POLL_SECONDS)
            job = await repo.get_job(args.job_id, user_id)

    snapshot = service.job_snapshot(job)
    lines = _job_lines(snapshot, background=not _is_terminal(job.status))

    if args.action == "result" or _is_terminal(job.status):
        if args.action == "result" and not _is_terminal(job.status):
            lines.append("result_pending=true")
            lines.append("The job has not finished; this is a progress snapshot, not the result.")
        result = snapshot.get("result") or {}
        if result:
            lines.append(f"result={json.dumps(result, ensure_ascii=False)}")
        artifacts = await repo.list_artifacts(job.id, user_id)
        for artifact in artifacts:
            lines.append(
                f"artifact={artifact['assetId']} role={artifact['role']} name={artifact['name']}"
            )
    if job.status == JobStatus.WAITING_USER.value:
        # The row snapshot is authoritative. Event reads are paginated from the
        # oldest sequence, so deriving the current question from them can show a
        # stale prompt after a long-running job has emitted many transitions.
        waiting_progress = job.progress_data or {}
        prompt = waiting_progress.get("prompt")
        if prompt:
            lines.append(f"waiting_user_prompt={prompt}")
            input_schema = waiting_progress.get("input_schema") or {}
            if input_schema.get("x-operator-only") is True:
                lines.append("operator_review_required=true")
                lines.append(
                    "This is a platform-operator hold. Do not ask the user for a provider "
                    "task id and do not call action=resume."
                )
            else:
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
