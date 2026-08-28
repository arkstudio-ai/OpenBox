"""Generic skill_job tool: admission-only start, wait budget, resume, cancel."""
import uuid

from skill_runtime import registry, repository as repo
from skill_runtime.types import JobStatus
from skill_runtime.worker import SkillJobWorker
from tool.skill_job import WAIT_BUDGET_PER_TURN, skill_job_tool
from tool.tool import ToolContext


async def _make_session(user_id: str) -> str:
    """A job may only be filed under a session and project its caller owns, so
    the tool tests need real rows rather than made-up ids."""
    from datetime import datetime, timezone

    from db.base import get_db_session
    from db.models.project import Project
    from db.models.session import Session as SessionORM

    session_id = "session_" + uuid.uuid4().hex[:10]
    project_id = "project_" + uuid.uuid4().hex[:10]
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(Project(
            id=project_id, user_id=user_id, name="tool test",
            created_at=now, updated_at=now,
        ))
        db.add(SessionORM(
            id=session_id, user_id=user_id, title="tool test",
            project_id=project_id, created_at=now, updated_at=now,
        ))
    return session_id


async def _owned_ctx(**overrides):
    user_id = overrides.pop("user_id", "u_" + uuid.uuid4().hex[:8])
    session_id = overrides.pop("session_id", None) or await _make_session(user_id)
    return _ctx(user_id=user_id, session_id=session_id, **overrides)


def _ctx(**overrides):
    fields = dict(
        session_id="sess_" + uuid.uuid4().hex[:8],
        user_id="u_" + uuid.uuid4().hex[:8],
        message_id="msg_" + uuid.uuid4().hex[:8],
        part_id="part_" + uuid.uuid4().hex[:8],
        # A skill may only be started once this agent turn actually loaded it
        # (§8.1); the loop fills this from the skills it activated.
        active_skills=frozenset({"builtin:demo-echo"}),
    )
    fields.update(overrides)
    return ToolContext(**fields)


async def _run(args, ctx):
    return await skill_job_tool.execute(args, ctx)


async def _drive_until(job_id, user_id, statuses, ticks=40):
    # concurrency=1: the in-memory test DB rides one shared StaticPool
    # connection, so concurrent invocations would interleave inside a single
    # transaction. Real isolation (PostgreSQL / file SQLite) is exercised by
    # the dedicated concurrency suite.
    registry.load_builtin_handlers()
    worker = SkillJobWorker(queues=("default",), concurrency=1, per_user_limit=0)
    job = await repo.get_job(job_id, user_id)
    for _ in range(ticks):
        if job.status in statuses:
            return job
        await worker.run_once()
        await worker.drain()
        job = await repo.get_job(job_id, user_id)
    return job


async def test_start_admits_and_replay_reuses():
    ctx = await _owned_ctx()
    first = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "echo",
         "input": {"text": "hi"}},
        ctx,
    )
    assert first.metadata["created"] is True
    assert "background=true" in first.output
    job_id = first.metadata["job_id"]

    # Same tool call replayed (same part_id) must not create a second job.
    again = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "echo",
         "input": {"text": "hi"}},
        ctx,
    )
    assert again.metadata["created"] is False
    assert again.metadata["job_id"] == job_id


async def test_start_unknown_skill_is_friendly():
    result = await _run(
        {"action": "start", "skill": "builtin:nope", "operation": "x", "input": {}},
        await _owned_ctx(active_skills=frozenset({"builtin:nope"})),
    )
    assert result.title == "Unknown skill"


async def test_start_requires_the_skill_to_be_activated_this_turn():
    """A model may not start a skill this turn never loaded, even a real one."""
    result = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "echo",
         "input": {"text": "hi"}},
        await _owned_ctx(active_skills=frozenset()),
    )
    assert result.title == "Skill not activated"


async def test_wait_budget_exhausts_per_turn():
    ctx = await _owned_ctx()
    started = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "ask_then_echo",
         "input": {}},
        ctx,
    )
    job_id = started.metadata["job_id"]

    for _ in range(WAIT_BUDGET_PER_TURN):
        result = await _run({"action": "wait", "job_id": job_id, "wait_seconds": 1}, ctx)
        assert "wait_budget_exhausted" not in result.output

    third = await _run({"action": "wait", "job_id": job_id, "wait_seconds": 1}, ctx)
    assert third.metadata.get("wait_budget_exhausted") is True

    # A new turn (new message) gets a fresh budget.
    fresh = await _run(
        {"action": "wait", "job_id": job_id, "wait_seconds": 1},
        _ctx(user_id=ctx.user_id, session_id=ctx.session_id),
    )
    assert "wait_budget_exhausted" not in fresh.output


async def test_resume_flow_through_tool():
    ctx = await _owned_ctx()
    started = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "ask_then_echo",
         "input": {}},
        ctx,
    )
    job_id = started.metadata["job_id"]
    job = await _drive_until(job_id, ctx.user_id, {JobStatus.WAITING_USER.value})
    assert job.status == JobStatus.WAITING_USER.value

    got = await _run({"action": "get", "job_id": job_id}, ctx)
    assert "waiting_user_prompt=What should I echo?" in got.output

    resumed = await _run(
        {"action": "resume", "job_id": job_id, "input": {"text": "answered"}}, ctx
    )
    assert resumed.metadata["created"] is True

    job = await _drive_until(job_id, ctx.user_id, {JobStatus.SUCCEEDED.value})
    result = await _run({"action": "result", "job_id": job_id}, ctx)
    assert '"echo": "answered"' in result.output or '"echo":"answered"' in result.output


async def test_cancel_through_tool():
    ctx = await _owned_ctx()
    started = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "slow_echo",
         "input": {"text": "x", "delay_seconds": 300}},
        ctx,
    )
    job_id = started.metadata["job_id"]
    cancelled = await _run({"action": "cancel", "job_id": job_id}, ctx)
    assert cancelled.metadata["status"] == JobStatus.CANCELLED.value


async def test_other_users_job_invisible():
    ctx = await _owned_ctx()
    started = await _run(
        {"action": "start", "skill": "builtin:demo-echo", "operation": "echo",
         "input": {"text": "hi"}},
        ctx,
    )
    stranger = await _owned_ctx()
    result = await _run({"action": "get", "job_id": started.metadata["job_id"]}, stranger)
    assert result.title == "Job not found"


async def test_validation_errors_are_tool_results():
    result = await _run({"action": "start"}, await _owned_ctx())
    assert "validation error" in result.output.lower() or "Invalid input" in result.title
