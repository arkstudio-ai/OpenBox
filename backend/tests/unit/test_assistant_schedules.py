"""Real SQL coverage for owner-only cron reads in the private main.

V2 (PERSONAL_ASSISTANT_DESIGN_V2.md 4.2, D1): each schedules.list read checks
current scope; earlier observations and saved answers are not re-validated.
"""
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import func, select

from assistant.history import read_history
from assistant.policy import AssistantError
from assistant.schedules import list_schedules
from cron.service import CronService
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.cron import CronJob, CronRun
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from db.models.workspace import WorkspaceMember
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_api import client_for
from tests.unit.test_assistant_commands import setup_task
from tests.unit.assistant_helpers import finish
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import call_tool, read_turn
from tests.unit.assistant_helpers import prepare_report
from tool.assistant_tools import assistant_tools


async def job_for(user, workspace, project, **changes):
    now = datetime.now(timezone.utc)
    row = CronJob(id="cron_" + uuid4().hex, user_id=user, workspace_id=workspace, project_id=project,
        name="PRIVATE_SCHEDULE_MARKER", enabled=False, schedule={"kind": "every", "every_ms": 600000},
        task_prompt="PRIVATE_PROMPT_NEVER_PROJECT", summary_cache="PRIVATE_HISTORY_NEVER_PROJECT",
        last_error="PRIVATE_ERROR_NEVER_PROJECT", delivery={"webhook_token": "PRIVATE_TOKEN_NEVER_PROJECT"},
        created_at=now, updated_at=now)
    for key, value in changes.items():
        setattr(row, key, value)
    async with get_db_session() as db:
        db.add(row)
    return row


async def test_inventory_paginates_filters_and_never_creates_or_wakes_work(monkeypatch):
    owner, other, workspace, main, _ = await setup_task()
    first = await job_for(owner, workspace, main.project_id, name="literal%_schedule", session_id=main.id)
    second = await job_for(owner, workspace, main.project_id, name="Another job", enabled=True)
    await job_for(other, workspace, main.project_id)
    await job_for(owner, workspace, main.project_id, is_deleted=True)
    await job_for(owner, workspace, main.project_id, session_id="missing")
    await job_for(owner, workspace, "missing")
    async with get_db_session() as db:
        foreign_workspace = (await db.get(User, other)).default_workspace_id
    await job_for(owner, foreign_workspace, main.project_id)
    def forbidden(*args, **kwargs):
        raise AssertionError("An inventory read must not trigger work")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", forbidden)
    monkeypatch.setattr("cron.service.arm_timer", forbidden)
    monkeypatch.setattr("session.session._new_session_record", forbidden)
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id)
    a = await list_schedules(**scope, limit=1)
    b = await list_schedules(**scope, limit=1, cursor=a["next_cursor"])
    assert {item["id"] for item in a["items"] + b["items"]} == {first.id, second.id}
    assert b["next_cursor"] is None and a["untrusted_data"] is True
    assert [x["id"] for x in (await list_schedules(**scope, query="%_", project_id=main.project_id))["items"]] == [first.id]
    assert [x["id"] for x in (await list_schedules(**scope, enabled=True))["items"]] == [second.id]
    text = json.dumps(a, default=str)
    assert all(marker not in text for marker in ("PRIVATE_PROMPT", "PRIVATE_HISTORY", "PRIVATE_ERROR", "PRIVATE_TOKEN"))
    async with get_db_session() as db:
        for model in (AssistantCommand, AssistantTask, AgentInboxItem, CronRun):
            field = model.actor_user_id if model is AssistantCommand else model.user_id
            assert await db.scalar(select(func.count()).select_from(model).where(field == owner)) == 0
        assert await db.scalar(select(func.count()).select_from(Session).where(Session.user_id == owner)) == 1


async def test_cron_rest_list_counts_details_and_runs_share_owner_and_workspace_scope(monkeypatch):
    from api import cron as api
    owner, other, workspace, main, _ = await setup_task()
    now = datetime.now(timezone.utc)
    private = await job_for(owner, workspace, main.project_id, session_id=main.id,
                          enabled=True, running_at=now, next_run_at=now + timedelta(hours=1))
    peer = await job_for(other, workspace, main.project_id, name="Peer job")
    await job_for(owner, workspace, main.project_id, is_deleted=True, running_at=now)
    async with get_db_session() as db:
        other_workspace = (await db.get(User, other)).default_workspace_id
        db.add(CronRun(id="cron_run_" + uuid4().hex, job_id=private.id, user_id=owner,
            status="ok", started_at=now, summary_text="PRIVATE_RUN_BODY"))
    await job_for(owner, other_workspace, main.project_id, enabled=True, running_at=now)
    app = FastAPI()
    app.include_router(api.router)
    actor = {"user_id": other, "workspace_id": workspace}
    app.dependency_overrides[api.get_current_user] = lambda: actor
    app.dependency_overrides[api.get_workspace] = lambda: {"id": workspace}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cron.test") as client:
        assert [x["id"] for x in (await client.get("/api/cron/jobs")).json()] == [peer.id]
        status = (await client.get("/api/cron/status")).json()
        assert (status["total_jobs"], status["enabled_jobs"], status["running_jobs"], status["next_run_at"]) == (1, 0, 0, None)
        assert (await client.get(f"/api/cron/jobs/{private.id}")).status_code == 404
        assert (await client.get(f"/api/cron/jobs/{private.id}/runs")).json() == []
        actor["user_id"] = owner
        status = (await client.get("/api/cron/status")).json()
        assert (status["total_jobs"], status["enabled_jobs"], status["running_jobs"]) == (1, 1, 1)
        assert status["next_run_at"]
        assert len((await client.get(f"/api/cron/jobs/{private.id}/runs")).json()) == 1
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
    service = CronService()
    assert await service.list_jobs(owner, workspace_id=workspace) == []
    assert await service.get_job(private.id, owner, workspace_id=workspace) is None
    assert await service.list_runs(private.id, owner, workspace_id=workspace) == []
    assert (await service.status(owner, workspace))["total_jobs"] == 0


async def test_legacy_cron_tool_does_not_project_workspace_peers_prompts():
    from tool.cron_tool import CronToolArgs, execute
    from tool.tool import ToolContext
    owner, other, workspace, main, _ = await setup_task()
    await job_for(owner, workspace, main.project_id, name="Own job", task_prompt="Own prompt")
    await job_for(other, workspace, main.project_id)
    result = await execute(CronToolArgs(action="list"), ToolContext(user_id=owner,
        workspace_id=workspace, project_id=main.project_id, session_id=main.id))
    assert "Own job" in result.output and "PRIVATE_SCHEDULE_MARKER" not in result.output
    assert "PRIVATE_PROMPT" not in result.output


@pytest.mark.parametrize("change", ["job_deleted", "owner", "workspace", "project_deleted", "project_owner", "notify_deleted", "notify_policy", "membership"])
async def test_saved_schedule_answers_stay_and_the_next_read_checks_current_scope(change):
    from tests.unit.assistant_helpers import next_turn, projected_request
    ctx, lease, answer, original, _ = await read_turn()
    job = await job_for(ctx.user_id, ctx.workspace_id, ctx.project_id, session_id=original["execution_session_id"])
    try:
        result, _, part = await call_tool(ctx, "schedules.list", {})
        assert not result.metadata.get("error"), result.output
        assert "PRIVATE_SCHEDULE_MARKER" in result.output and "PRIVATE_PROMPT" not in result.output
        await consume_context(ctx)
        await finish(ctx, lease, answer, "PRIVATE_SCHEDULE_DERIVATION")
        async with get_db_session() as db:
            row = await db.get(CronJob, job.id)
            other = await db.scalar(select(WorkspaceMember.user_id).where(
                WorkspaceMember.workspace_id == ctx.workspace_id, WorkspaceMember.user_id != ctx.user_id))
            if change == "job_deleted": row.is_deleted = True
            if change == "owner": row.user_id = other
            if change == "workspace": row.workspace_id = (await db.get(User, other)).default_workspace_id
            if change == "project_deleted": (await db.get(Project, ctx.project_id)).is_deleted = True
            if change == "project_owner": (await db.get(Project, ctx.project_id)).user_id = other
            if change == "notify_deleted": (await db.get(Session, row.session_id)).is_deleted = True
            if change == "notify_policy": (await db.get(Session, row.session_id)).visibility = "workspace"
            if change == "membership": (await db.get(WorkspaceMember, (ctx.workspace_id, ctx.user_id))).status = "removed"
        identity = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        if change == "membership":
            for read in (read_history(**identity, session_id=ctx.session_id, message_ids=[answer.id]),
                         list_schedules(**identity)):
                with pytest.raises(AssistantError) as denied:
                    await read
                assert denied.value.code == "ASSISTANT_WORKSPACE_FORBIDDEN"
            return
        # D1: the saved answer is history; only a new read reflects the change.
        page = await read_history(**identity, session_id=ctx.session_id, message_ids=[answer.id])
        assert "PRIVATE_SCHEDULE_DERIVATION" in json.dumps(page)
        listed = [item["id"] for item in (await list_schedules(**identity))["items"]]
        assert listed == ([job.id] if change == "notify_policy" else [])
        # The earlier observation is not replayed into a later provider request.
        ctx, lease, _ = await next_turn(ctx, "Which schedules exist now?")
        _, messages = await projected_request(ctx)
        assert "PRIVATE_SCHEDULE_MARKER" not in json.dumps(messages)
        assert "PRIVATE_SCHEDULE_DERIVATION" in json.dumps(messages)
    finally:
        await lease.release(session_status="idle")


async def test_progress_shows_in_the_next_read_without_rewriting_the_observation():
    ctx, lease, _, _, _ = await read_turn()
    job = await job_for(ctx.user_id, ctx.workspace_id, ctx.project_id)
    try:
        observed, ctx, _ = await call_tool(ctx, "schedules.list", {})
        assert json.loads(observed.output)["items"][0]["total_runs"] == 0
        async with get_db_session() as db:
            row = await db.get(CronJob, job.id)
            row.name, row.enabled, row.last_status, row.total_runs = "Renamed job", True, "ok", 1
            row.schedule = {"kind": "cron", "expr": "0 8 * * *", "tz": "Asia/Shanghai"}
        [current] = (await list_schedules(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                                          main_id=ctx.session_id))["items"]
        assert (current["name"], current["total_runs"], current["enabled"]) == ("Renamed job", 1, True)
        assert current["schedule"] == {"kind": "cron", "expr": "0 8 * * *", "tz": "Asia/Shanghai"}
        # This run's observation is used as read; it is neither refreshed nor rejected.
        wire = json.dumps(await consume_context(ctx))
        assert "PRIVATE_SCHEDULE_MARKER" in wire and "Renamed job" not in wire
    finally:
        await lease.release(session_status="idle")


async def test_report_only_cannot_read_inventory():
    ctx, lease, *_ = await prepare_report()
    try:
        tool = next(item for item in assistant_tools if item.id == "schedules.list")
        result = await tool.execute({}, ctx)
        assert result.metadata["failure_code"] == "ASSISTANT_TOOL_FORBIDDEN"
    finally:
        await lease.release(session_status="idle")


async def test_http_inventory_is_read_only_and_rejects_invalid_filters(monkeypatch):
    owner, _, workspace = await accounts()
    async with client_for(owner, workspace, monkeypatch) as client:
        assert (await client.get("/api/assistant/schedules")).status_code == 404
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(Session).where(Session.user_id == owner)) == 0
    owner, _, workspace, main, _ = await setup_task()
    job = await job_for(owner, workspace, main.project_id)
    async with client_for(owner, workspace, monkeypatch) as client:
        response = await client.get("/api/assistant/schedules", params={"query": "PRIVATE_SCHEDULE", "enabled": "false"})
        assert response.status_code == 200 and response.json()["items"][0]["id"] == job.id
        for params in ({"limit": 0}, {"limit": 51}, {"query": "x" * 201}, {"cursor": "x" * 65}, {"enabled": "maybe"}):
            assert (await client.get("/api/assistant/schedules", params=params)).status_code == 422
    tool = next(item for item in assistant_tools if item.id == "schedules.list")
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        tool.parameters.model_validate({"actor_user_id": owner})


@pytest.mark.parametrize("schedule,expected", [
    ({"kind": "every", "every_ms": 600000, "secret": "DO_NOT_PROJECT"}, {"kind": "every", "every_ms": 600000, "anchor_ms": None}),
    ({"kind": "at", "at": "2030-01-01T00:00:00Z", "token": "DO_NOT_PROJECT"}, {"kind": "at", "at": "2030-01-01T00:00:00Z"}),
    ({"kind": "cron", "expr": "0 8 * * *", "tz": "UTC", "payload": "DO_NOT_PROJECT"}, {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"}),
    ({"kind": "cron", "expr": "x" * 50000}, None),
    ({"kind": "every", "every_ms": True}, None),
])
async def test_clock_projection_is_bounded_and_allowlisted(schedule, expected):
    owner, _, workspace, main, _ = await setup_task()
    await job_for(owner, workspace, main.project_id, schedule=schedule)
    page = await list_schedules(user_id=owner, workspace_id=workspace, main_id=main.id)
    assert page["items"][0]["schedule"] == expected
    assert "DO_NOT_PROJECT" not in json.dumps(page, default=str)
    assert page["items"][0]["created_at"].endswith("+00:00")
