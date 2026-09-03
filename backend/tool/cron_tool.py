"""Cron tool — allows the agent to create/manage cron jobs for the current session."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from tool.tool import ToolContext, ToolResult, ToolInfo, define_tool
from core.log import create_logger

log = create_logger("tool.cron")


class CronToolArgs(BaseModel):
    action: Literal["add", "list", "remove", "enable", "disable"] = Field(
        description="Action to perform: add, list, remove, enable, or disable"
    )
    # For add:
    name: str = Field(default="", description="Name for the cron job")
    schedule: str = Field(default="", description="Cron expression (e.g. '0 9 * * *') or interval like 'every 30m'")
    timezone: str = Field(default="UTC", description="Timezone for cron schedule")
    task: str = Field(default="", description="The task prompt to execute on schedule")
    # For remove/enable/disable:
    job_id: str = Field(default="", description="Job ID to operate on")


async def execute(args: CronToolArgs, ctx: ToolContext) -> ToolResult:
    """Execute the cron tool."""
    from cron.service import cron_service
    from cron.types import CronJobCreate, CronScheduleCron, CronScheduleEvery

    # Jobs are project-scoped; the current conversation (its origin when this
    # runs inside a cron temp session) becomes the notify target.
    notify_session = ctx.origin_session_id or ctx.session_id
    project_id = ctx.project_id
    if not project_id and notify_session:
        from session.session import project_id_for
        project_id = await project_id_for(notify_session)

    if args.action == "list":
        jobs = await cron_service.list_jobs(
            ctx.user_id, project_id=project_id or None,
            workspace_id=ctx.workspace_id or None,
        )
        if not jobs:
            return ToolResult(
                title="No cron jobs",
                output="No scheduled tasks found for this project.",
            )
        lines = []
        for j in jobs:
            status = "enabled" if j["enabled"] else "disabled"
            sched = j.get("schedule", {})
            sched_str = sched.get("expr", "") or f"every {sched.get('every_ms', 0)}ms"
            lines.append(
                f"- **{j['name']}** (id: {j['id'][:12]}...) [{status}]\n"
                f"  Schedule: `{sched_str}`\n"
                f"  Task: {j['task_prompt'][:80]}...\n"
                f"  Last: {j.get('last_status', 'never')} | Next: {j.get('next_run_at', 'N/A')}"
            )
        return ToolResult(
            title=f"{len(jobs)} cron job(s)",
            output="\n\n".join(lines),
        )

    if args.action == "add":
        if not args.name or not args.schedule or not args.task:
            return ToolResult(
                title="Missing parameters",
                output="Required: name, schedule, and task. Example: cron(action='add', name='Daily Report', schedule='0 9 * * *', task='Generate a daily summary report')",
            )

        # Recursion guard on the CALLING session: a scheduled task's agent
        # must not schedule further tasks, even though its tool context may
        # point origin_session_id back at the main session.
        from cron.validation import ensure_not_cron_session
        try:
            await ensure_not_cron_session(ctx.session_id)
        except ValueError as e:
            return ToolResult(title="Not allowed", output=str(e))

        # Parse schedule
        schedule = _parse_schedule(args.schedule, args.timezone)
        if not schedule:
            return ToolResult(
                title="Invalid schedule",
                output=f"Could not parse schedule: '{args.schedule}'. Use cron syntax like '0 9 * * *' or interval like 'every 30m', 'every 1h'.",
            )

        # Quotas, prompt length, minimum interval, session ownership, and
        # future-time checks all live in the service layer now — the REST API
        # and this tool share one rulebook and one set of error messages.
        try:
            create = CronJobCreate(
                project_id=project_id or "",
                session_id=notify_session,
                name=args.name,
                schedule=schedule,
                task_prompt=args.task,
            )
            result = await cron_service.add(
                ctx.user_id, create, workspace_id=ctx.workspace_id or None
            )
            return ToolResult(
                title=f"Created: {args.name}",
                output=f"Cron job created successfully.\nID: {result['id']}\nNext run: {result.get('next_run_at', 'N/A')}",
            )
        except Exception as e:
            return ToolResult(
                title="Failed to create cron job",
                output=str(e),
            )

    if args.action == "remove":
        if not args.job_id:
            return ToolResult(title="Missing job_id", output="Provide the job_id to remove.")
        try:
            await cron_service.remove(
                args.job_id, ctx.user_id, workspace_id=ctx.workspace_id or None
            )
            return ToolResult(title="Job removed", output=f"Cron job {args.job_id} has been deleted.")
        except ValueError as e:
            return ToolResult(title="Not found", output=str(e))

    if args.action in ("enable", "disable"):
        if not args.job_id:
            return ToolResult(title="Missing job_id", output="Provide the job_id.")
        try:
            from cron.types import CronJobUpdate
            enabled = args.action == "enable"
            await cron_service.update(
                args.job_id,
                ctx.user_id,
                CronJobUpdate(enabled=enabled),
                workspace_id=ctx.workspace_id or None,
            )
            return ToolResult(
                title=f"Job {'enabled' if enabled else 'disabled'}",
                output=f"Cron job {args.job_id} is now {'enabled' if enabled else 'disabled'}.",
            )
        except ValueError as e:
            return ToolResult(title="Not found", output=str(e))

    return ToolResult(title="Unknown action", output=f"Unknown action: {args.action}")


def _parse_schedule(schedule_str: str, tz: str = "UTC"):
    """Parse a schedule string into a CronSchedule object."""
    from cron.types import CronScheduleCron, CronScheduleEvery
    import re

    # Try "every Nm" / "every Nh" / "every Ns" format
    match = re.match(r"every\s+(\d+)\s*(m|min|minutes?|h|hours?|s|seconds?)", schedule_str, re.IGNORECASE)
    if match:
        value = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("h"):
            ms = value * 3600_000
        elif unit.startswith("m"):
            ms = value * 60_000
        else:
            ms = value * 1000
        if ms < 60_000:
            return None  # Minimum 1 minute
        return CronScheduleEvery(every_ms=ms)

    # Try cron expression
    try:
        from croniter import croniter
        croniter(schedule_str)  # Validate
        return CronScheduleCron(expr=schedule_str, tz=tz)
    except (ValueError, KeyError):
        return None


cron_tool = define_tool(
    "cron",
    description="""\
Create, list, or manage scheduled tasks (cron jobs) for the current project.

Actions:
  - add: Create a new scheduled task (requires name, schedule, task)
  - list: Show all cron jobs for this project
  - remove: Delete a cron job (requires job_id)
  - enable/disable: Toggle a cron job (requires job_id)

Schedule formats:
  - Cron expression: "0 9 * * *" (daily at 9am), "*/30 * * * *" (every 30 min)
  - Interval: "every 30m", "every 1h", "every 6h"

Recurring jobs may not fire more often than every 5 minutes. The job executes
automatically at the scheduled time and its result appears in this conversation;
runs that produce nothing noteworthy stay silent.""",
    parameters=CronToolArgs,
    execute=execute,
    sandbox_required=False,
)
