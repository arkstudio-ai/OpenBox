"""Real SQL context: semantic clues beyond titles, scoped, bounded and supplied before decisions."""
import json

import pytest
from assistant.project_tools import create_project
from assistant.reads import list_projects, list_sessions
from assistant.work_context import directory
from core.identifier import generate_id
from db.base import get_db_session
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from question.runtime import now
from session.session import create_session
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import assistant, main_turn
from tests.unit.test_assistant_projection_voice import projected


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)


async def text(session, owner, body, *, role="user", **data):
    message_id = generate_id()
    async with get_db_session() as db:
        db.add(Message(id=message_id, session_id=session.id, user_id=owner, role=role, created_at=now()))
        await db.flush()
        db.add(Part(id=generate_id(), message_id=message_id, session_id=session.id, user_id=owner,
                    type="text", data={"text": body, **data}, created_at=now()))
    return message_id


async def test_directory_exposes_meaning_beyond_titles_and_omits_other_owners_and_hidden_text():
    owner, other, workspace, main = await assistant()
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id)
    project = await create_project(**scope, name="North Star", description="咖啡品牌门店运营与新品上市")
    from project.brief import update_brief
    await update_brief(user_id=owner, workspace_id=workspace, project_id=project["project_id"],
        content="正在准备杭州门店开业，预算尚未决定。", expected_revision=0, updated_by="assistant")
    session = await create_session(user_id=owner, workspace_id=workspace, project_id=project["project_id"], title="聊一聊")
    original = await text(session, owner, "继续完善杭州那家店的开业活动，先做预算。")
    await text(session, owner, "已整理活动结构，等待预算。", role="assistant")
    await text(session, owner, "那就再补一版吧。")
    await text(session, owner, "HIDDEN_CANARY", synthetic=True)
    await text(session, owner, "IGNORED_CANARY", ignored=True)
    theirs = await create_session(user_id=other, workspace_id=workspace, title="OTHER_OWNER_CANARY")
    await text(theirs, other, "PRIVATE_CANARY")
    async with get_db_session() as db:
        view = await directory(db, main)
    serialized = json.dumps(view, ensure_ascii=False, default=str)
    assert all(word not in serialized for word in ("HIDDEN_CANARY", "IGNORED_CANARY", "OTHER_OWNER_CANARY", "PRIVATE_CANARY"))
    chosen = next(item for item in view["projects"]["items"] if item["id"] == project["project_id"])
    assert "咖啡" in chosen["description"]["text"] and chosen["brief_available"] is True
    assert "brief" not in chosen
    details = await list_projects(**scope, include_context=True)
    assert "杭州" in next(item for item in details["items"] if item["id"] == project["project_id"])["brief"]["text"]
    conversation = next(item for item in view["conversations"]["items"] if item["id"] == session.id)
    assert conversation["title"] == "聊一聊"
    assert conversation["recent_messages"][0]["message_id"] == original
    assert "预算" in conversation["recent_messages"][0]["text"]
    assert conversation["recent_messages"][-1]["text"] == "那就再补一版吧。"
    assert view["untrusted_data"] is True


async def test_pagination_and_truncation_are_visible_and_deleted_projects_are_not_candidates():
    owner, _, workspace, main = await assistant()
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id)
    for index in range(9):
        await create_project(**scope, name=f"Project {index}")
    for index in range(7):
        session = await create_session(user_id=owner, workspace_id=workspace, title=f"Conversation {index}")
        await text(session, owner, "x" * 1000)
    async with get_db_session() as db:
        view = await directory(db, main)
    assert len(view["projects"]["items"]) == 8 and view["projects"]["next_cursor"]
    assert len(view["conversations"]["items"]) == 6 and view["conversations"]["next_cursor"]
    assert all(item["recent_messages"][0]["truncated"] for item in view["conversations"]["items"])
    more = await list_sessions(**scope, cursor=view["conversations"]["next_cursor"], include_context=True)
    assert len(more["items"]) == 1 and more["next_cursor"] is None
    removed = view["projects"]["items"][1]["id"]
    async with get_db_session() as db:
        (await db.get(Project, removed)).is_deleted = True
    assert removed not in {item["id"] for item in (await list_projects(**scope, include_context=True))["items"]}


async def test_ordinary_model_projection_already_has_directory_without_a_lookup_call():
    owner, _, workspace, main = await assistant()
    session = await create_session(user_id=owner, workspace_id=workspace, title="早先的安排")
    await text(session, owner, "这家咖啡店的开业预算")
    ctx, lease, _ = await main_turn(owner, workspace, main, "把预算补上")
    try:
        block = next(message for message in await projected(ctx) if message.id == "assistant:work-directory")
        assert "这家咖啡店的开业预算" in block.parts[0]["text"]
        assert block.parts[0]["origin"] == "system_recovery" and block.parts[0]["synthetic"]
        assert all(message.id != "assistant:work-directory" for message in await projected(ctx, for_compaction=True))
    finally:
        await lease.release(session_status="idle")


async def test_current_directory_survives_old_chat_but_never_displaces_current_input(monkeypatch):
    from types import SimpleNamespace
    from assistant.projection import project_main_messages
    from session.agent_event_log import load_canonical_model_surface

    owner, _, workspace, main = await assistant()
    ctx, lease, human = await main_turn(owner, workspace, main, "把刚才的方案补上预算")
    try:
        surface = await load_canonical_model_surface(main.id, user_id=owner, run_fence=ctx.run_fence)
        baseline = await projected(ctx)
        costs = {m.id: sum(len(json.dumps(p, ensure_ascii=False, default=str)) for p in m.parts)
                 for m in baseline}
        stale = SimpleNamespace(id="old-chat", role="assistant", summary=False, finish="stop",
                                error=None, parts=[{"type": "text", "text": "old discussion " * 100}])
        messages = [stale, *surface.messages]
        monkeypatch.setattr("assistant.projection.MAX_CONTEXT_CHARS", sum(costs.values()) + 50)
        ids = {m.id for m in await project_main_messages(messages, ctx=ctx)}
        assert human in ids and "assistant:work-directory" in ids and stale.id not in ids

        # If even navigation cannot fit, the original request still survives.
        monkeypatch.setattr("assistant.projection.MAX_CONTEXT_CHARS",
                            sum(costs.values()) - costs["assistant:work-directory"] + 50)
        ids = {m.id for m in await project_main_messages(messages, ctx=ctx)}
        assert human in ids and "assistant:work-directory" not in ids
    finally:
        await lease.release(session_status="idle")
