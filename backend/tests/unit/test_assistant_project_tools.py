"""projects.create on real SQL (docs/ASSISTANT_VOICE_FIX_PLAN.md 2).

The assistant creates the project the user names, returns the user's existing
one instead of a duplicate (the default project included), records the user's
words as the command source and never starts a cloud desktop for it.
"""
import pytest
from sqlalchemy import func, select

from assistant.commands import ToolSource
from assistant.policy import AssistantError
from assistant.project_tools import create_project
from assistant.runtime import ASSISTANT_PROMPT
from db.base import get_db_session
from db.models.assistant import AssistantCommand
from db.models.project import Project
from models.message import ToolStatus
from project.workspace import DEFAULT_NAME
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import MAIN_MESSAGES, TOOLS, assistant, main_turn, tool_call


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-project-tools-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)

    async def no_desktop(**_kwargs):
        raise AssertionError("creating a project never acquires a cloud desktop")
    monkeypatch.setattr("sandbox.sandbox_manager.get_client_any", no_desktop)
    MAIN_MESSAGES.clear()


async def projects(owner, workspace):
    async with get_db_session() as db:
        return list((await db.scalars(select(Project).where(Project.user_id == owner,
            Project.workspace_id == workspace, Project.is_deleted.is_(False)).order_by(Project.created_at))).all())


async def test_create_records_the_users_words_and_a_repeat_returns_the_same_project():
    owner, _, workspace, main = await assistant()
    ctx, lease, human = await main_turn(owner, workspace, main, "新建一个项目叫手机视频宣传，然后做一个 iPhone 18 口播视频。")
    try:
        created, part = await tool_call(ctx, "projects.create", {"name": " 手机视频宣传 ", "description": "口播视频",
                                                                 "source_message_ids": [human]})
        assert part.status == ToolStatus.COMPLETED, created
        assert created["state"] == "created" and created["name"] == "手机视频宣传"
        assert created["link"] == f"/app?project={created['project_id']}"
        # The same persisted call again (a retried execution) returns its receipt.
        from dataclasses import replace
        replay = await TOOLS["projects.create"].execute({"name": " 手机视频宣传 ", "description": "口播视频",
            "source_message_ids": [human]}, replace(ctx, part_id=part.id))
        import json
        assert json.loads(replay.output) == created
        # A new call with the same name, in other spacing and case, uses the existing project.
        again, _ = await tool_call(ctx, "projects.create", {"name": "手机视频宣传", "source_message_ids": [human]})
        assert again["state"] == "existing" and again["project_id"] == created["project_id"]
        # And the created project is immediately usable for work in the same turn.
        submitted, _ = await tool_call(ctx, "tasks.submit", {"project_id": created["project_id"],
            "title": "iPhone 18 口播视频", "instructions": "做一个 iPhone 18 口播视频。", "source_message_ids": [human]})
        assert submitted["state"] == "accepted"
    finally:
        await lease.release(session_status="idle")
    rows = [row for row in await projects(owner, workspace) if row.name == "手机视频宣传"]
    assert len(rows) == 1 and rows[0].description == "口播视频" and rows[0].slug.startswith("project-")
    async with get_db_session() as db:
        commands = list((await db.scalars(select(AssistantCommand).where(AssistantCommand.actor_user_id == owner,
            AssistantCommand.action == "project_create").order_by(AssistantCommand.created_at))).all())
    assert [(row.target_type, row.target_id, row.state) for row in commands] == [
        ("project", created["project_id"], "applied")] * 2
    assert [row.receipt["state"] for row in commands] == ["created", "existing"]
    source = commands[0].source_ref
    assert source["part_id"] == part.id and [ref["message_id"] for ref in source["source_refs"]] == [human]
    assert all(ref["origin"] == "human" for ref in source["source_refs"])


async def test_the_default_project_and_case_variants_are_never_duplicated():
    owner, _, workspace, main = await assistant()
    before = await projects(owner, workspace)
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id)
    for name in (DEFAULT_NAME, "default", "Default"):
        found = await create_project(**scope, name=name)
        assert found["state"] == "existing" and found["project_id"] == main.project_id
    snake = await create_project(**scope, name="Snake Game")
    assert snake["state"] == "created"
    assert (await create_project(**scope, name="snake  game"))["project_id"] == snake["project_id"]
    after = await projects(owner, workspace)
    assert len(after) == len(before) + 1


async def test_a_name_whose_directory_is_taken_gets_its_own_directory():
    from project import workspace as projects_service
    owner, _, workspace, main = await assistant()
    renamed = await projects_service.create_project(owner, workspace, "Snake")
    await projects_service.rename_project(renamed.id, owner, workspace, "贪吃蛇")
    created = await create_project(user_id=owner, workspace_id=workspace, main_id=main.id, name="Snake")
    assert created["state"] == "created" and created["project_id"] != renamed.id
    reserved = await create_project(user_id=owner, workspace_id=workspace, main_id=main.id, name="sessions")
    assert reserved["state"] == "created"
    slugs = {row.id: row.slug for row in await projects(owner, workspace)}
    assert slugs[renamed.id] == "snake" and slugs[created["project_id"]].startswith("project-")
    assert slugs[reserved["project_id"]].startswith("project-")


async def test_create_refuses_unverified_sources_and_invalid_names_without_a_project():
    owner, other, workspace, main = await assistant()
    before = len(await projects(owner, workspace))
    ctx, lease, human = await main_turn(owner, workspace, main, "新建一个项目叫测试。")
    try:
        forged, part = await tool_call(ctx, "projects.create", {"name": "测试", "source_message_ids": [ctx.message_id]})
        assert part.status == ToolStatus.ERROR and forged["error"] == "ASSISTANT_SOURCE_UNVERIFIED"
        with pytest.raises(AssistantError) as missing:
            await create_project(user_id=owner, workspace_id=workspace, main_id=main.id, name="测试",
                                 source=ToolSource("missing-part", ctx.run_id, ctx.run_generation, (human,)))
        assert missing.value.code == "ASSISTANT_CALL_UNVERIFIED"
        blank, part = await tool_call(ctx, "projects.create", {"name": "   ", "source_message_ids": [human]})
        assert blank["error"] == "ASSISTANT_PROJECT_INVALID"
    finally:
        await lease.release(session_status="idle")
    with pytest.raises(AssistantError) as foreign:
        await create_project(user_id=other, workspace_id=workspace, main_id=main.id, name="别人的")
    assert foreign.value.code == "ASSISTANT_UNAVAILABLE"
    assert len(await projects(owner, workspace)) == before
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
            AssistantCommand.actor_user_id.in_((owner, other)), AssistantCommand.action == "project_create")) == 0


def test_prompt_creates_missing_projects_instead_of_sending_the_user_to_the_interface():
    prompt = " ".join(ASSISTANT_PROMPT.split())
    assert ("When the user names a project that does not exist, create it with projects.create and continue "
            "in the same turn; say so in one line. Never ask them to click 新建 in the interface.") in prompt


async def test_names_without_ascii_created_back_to_back_both_get_a_project():
    """project.workspace derives these directory names from the clock; a collision gets a random one."""
    owner, _, workspace, main = await assistant()
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id)
    first, second = [await create_project(**scope, name=name) for name in ("项目甲", "项目乙")]
    assert first["state"] == second["state"] == "created" and first["project_id"] != second["project_id"]
    assert {row.name for row in await projects(owner, workspace)} >= {"项目甲", "项目乙"}


async def test_a_name_the_user_never_said_is_refused():
    """Measured on a voice turn: the fast model named the project after an older topic before the right one."""
    owner, _, workspace, main = await assistant()
    before = await projects(owner, workspace)
    ctx, lease, human = await main_turn(owner, workspace, main, "帮我新建一个项目，名字叫语音演练七号。")
    try:
        refused, part = await tool_call(ctx, "projects.create", {"name": "语音播报", "source_message_ids": [human]})
        assert part.status == ToolStatus.ERROR and refused["error"] == "ASSISTANT_PROJECT_NAME_UNSAID"
        created, _ = await tool_call(ctx, "projects.create", {"name": "语音演练 七号", "source_message_ids": [human]})
        assert created["state"] == "created" and created["name"] == "语音演练 七号"
    finally:
        await lease.release(session_status="idle")
    known = {row.id for row in before}
    assert [row.name for row in await projects(owner, workspace)
            if row.id not in known and row.name != DEFAULT_NAME] == ["语音演练 七号"]
