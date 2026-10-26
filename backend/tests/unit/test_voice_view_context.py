from dataclasses import replace
from datetime import datetime, timezone

import pytest

from db.base import get_db_session
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from memory.packet import TurnContext
from session.session import create_session
from tests.unit.test_memory_write_protocol import setup
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from voice import view_context
from voice.tools import CallScope


@pytest.mark.parametrize("value", [None, {}, 123, "../admin", "x" * 65, "<script>"])
def test_navigation_payload_is_only_a_bounded_identity(value):
    assert view_context.session_hint(value) is None


async def test_visible_chat_is_owner_scoped_and_does_not_expose_history(monkeypatch):
    owner, other, workspace, main, _ = await setup(monkeypatch)
    task = await create_session(user_id=owner, workspace_id=workspace, title="早餐营业视频")
    stranger = await create_session(user_id=other, workspace_id=workspace, title="他人机密")
    turn = TurnContext(owner, workspace, "换成照片", session_id=main.id,
        include_all_projects=True, read_policy="allowed")
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(SessionExecution(session_id=task.id, user_id=owner, generation=2, updated_at=now))
        for generation in (1, 2):
            db.add(QuestionCheckpoint(id=f"view-card-{generation}", user_id=owner, session_id=task.id,
                generation=generation, status="pending", questions=[{"question": "private full script"}],
                draft=[], continuation={}, created_at=now, updated_at=now))
    async with get_db_session() as db:
        value = await view_context.read(db, turn, task.id)
        assert value["pending_request_ids"] == ["view-card-2"]
        assert value["title"] == task.title and "private full script" not in str(value)
        assert await view_context.read(db, turn, stranger.id) is None
        assert await view_context.read(db, turn, main.id) is None
        row = await db.get(Session, task.id)
        row.is_deleted = True
    async with get_db_session() as db:
        assert await view_context.read(db, turn, task.id) is None


@pytest.mark.parametrize("policy", ["current_only", "no_history", "documents_only", "uncertain"])
async def test_restriction_does_not_query_navigation_history(policy):
    turn = TurnContext("user", "space", "只谈今天", read_policy=policy)
    # No database object: restricted turns must perform no read at all.
    assert await view_context.read(None, turn, "visible-session") is None


async def test_snapshot_and_revalidation_do_not_retarget_mid_utterance(monkeypatch):
    from core.config import get_config
    from memory.orchestrator import render_memory_context
    owner, _, workspace, main, _ = await setup(monkeypatch)
    first = await create_session(user_id=owner, workspace_id=workspace, title="First")
    second = await create_session(user_id=owner, workspace_id=workspace, title="Second")
    scope = CallScope(owner, workspace, main.id, viewed_session_id=first.id)
    packet = {"_semantic_packet": True, "_turn": TurnContext(owner, workspace, "改一下", session_id=main.id,
        include_all_projects=True), "settings": {}, "route": {"read_policy": "allowed"},
        "items": [], "stable_background": {"items": []}, "known_gaps": [], "degraded_reasons": []}
    await view_context.attach(scope, packet)
    await view_context.attach(replace(scope, viewed_session_id=second.id), packet)
    assert packet["viewed_task"]["session_id"] == first.id
    assert packet["budget"]["tokens"] <= get_config().memory.recall_token_budget
    async with get_db_session() as db:
        row = await db.get(Session, first.id)
        row.memory_policy = "isolated"
    await view_context.attach(scope, packet)
    assert "viewed_task" not in packet
    packet["viewed_task"] = {"title": "must not leak"}
    packet["route"]["read_policy"] = "current_only"
    assert "must not leak" not in render_memory_context(packet)


async def test_visible_history_uses_actual_text_and_forget_filters(monkeypatch):
    from sqlalchemy import select
    from db.models.part import Part
    from db.models.memory_v2 import MemorySource, MemoryTombstone
    from session.session import create_user_message
    owner, other, workspace, main, _ = await setup(monkeypatch)
    chat = await create_session(user_id=owner, workspace_id=workspace, title="容易误导的标题")
    message = await create_user_message(chat.id, "下午在家画一朵水彩花", user_id=owner)
    turn = TurnContext(owner, workspace, "刚才这段聊了什么", session_id=main.id,
        include_all_projects=True, read_policy="allowed")
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        part = await db.scalar(select(Part).where(Part.message_id == message.id))
        part_id = part.id
        db.add(Part(id="hidden-reasoning", user_id=owner, session_id=chat.id, message_id=message.id,
            type="text", data={"text": "internal analysis must not enter voice", "channel": "analysis"}, created_at=now))
    async with get_db_session() as db:
        hint = await view_context.read(db, turn, chat.id)
        assert hint["history_status"] == "not_read" and "history" not in hint
        value = await view_context.read(db, turn, chat.id, include_history=True)
        assert value["kind"] == "conversation" and value["pending_request_ids"] == []
        assert value["history_status"] == "complete"
        assert [entry["text"] for entry in value["history"]["entries"]] == ["下午在家画一朵水彩花"]
        db.add(MemorySource(id="forgotten-view-source", user_id=owner, workspace_id=workspace,
            source_kind="user_statement", session_id=chat.id, message_id=message.id, part_id=part_id,
            body="下午在家画一朵水彩花", content_hash="a" * 64, created_at=now))
        db.add(MemoryTombstone(id="forgotten-view-tombstone", object_kind="source", object_id="forgotten-view-source",
            revision=1, user_id=owner, workspace_id=workspace, deleted_at=now))
    async with get_db_session() as db:
        fresh = await view_context.read(db, turn, chat.id, include_history=True)
        assert fresh["history"]["entries"] == []
        assert fresh["history"]["version"] != value["history"]["version"]
    # Session.visibility uses workspace/private, unlike the memory store's
    # PERSONAL enum. History discovery still enforces owner and project ACL.
    from core.config import get_config
    from memory import readers
    from memory.packet import ReadRequest
    from memory.policy import MemoryAccessDenied
    stranger = await create_session(user_id=other, workspace_id=workspace, title="other person's conversation")
    catalog = await readers.catalogs(turn, {"history_useful": 1}, get_config().memory)
    assert chat.id in {item["id"] for item in catalog}
    assert stranger.id not in {item["id"] for item in catalog}
    with pytest.raises(MemoryAccessDenied):
        await readers.read_request(ReadRequest(kind="history", refs=({"id": stranger.id, "revision": 1},)),
            turn, get_config().memory)


async def test_semantic_history_need_loads_bounded_view_and_missing_view_is_explicit(monkeypatch):
    from core.config import get_config
    from memory.orchestrator import render_memory_context
    from session.session import create_user_message
    owner, _, workspace, main, _ = await setup(monkeypatch)
    chat = await create_session(user_id=owner, workspace_id=workspace, title="门店说明")
    await create_user_message(chat.id, "总共二十秒，单镜头最多五秒。", user_id=owner)
    scope = CallScope(owner, workspace, main.id, viewed_session_id=chat.id)
    packet = {"_semantic_packet": True, "_turn": TurnContext(owner, workspace, "再说一下", session_id=main.id,
        include_all_projects=True), "settings": {}, "route": {"read_policy": "allowed", "needs": {"history_useful": .9}},
        "items": [], "stable_background": {"items": []}, "known_gaps": [], "degraded_reasons": []}
    await view_context.attach(scope, packet)
    assert "总共二十秒" in render_memory_context(packet)
    assert packet["budget"]["tokens"] <= get_config().memory.recall_token_budget
    packet.pop("_viewed_session_id")
    await view_context.attach(replace(scope, viewed_session_id=None), packet)
    rendered = render_memory_context(packet)
    assert '"status":"unavailable"' in rendered and "总共二十秒" not in rendered
    assert "viewed_task" not in packet


async def test_visible_manual_answers_are_fresh_bounded_and_do_not_revive_forgotten_cards(monkeypatch):
    from db.models.part import Part
    from db.models.memory_v2 import MemorySource, MemoryTombstone
    from session.session import create_user_message, create_assistant_message
    owner, _, workspace, main, _ = await setup(monkeypatch)
    chat = await create_session(user_id=owner, workspace_id=workspace, title="读书会")
    human = await create_user_message(chat.id, "给我两个风格选", user_id=owner)
    assistant = await create_assistant_message(chat.id, human.id, user_id=owner)
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(Part(id="selected-style-part", message_id=assistant.id, session_id=chat.id,
            user_id=owner, type="tool", data={}, created_at=now))
        db.add(QuestionCheckpoint(id="selected-style", user_id=owner, session_id=chat.id,
            message_id=assistant.id, part_id="selected-style-part", generation=1, status="answered",
            applied=True, questions=[{"question": "开场风格？"}], answers=[["轻松温暖"]],
            draft=[], continuation={}, created_at=now, updated_at=now))
    turn = TurnContext(owner, workspace, "我已经点好了，还要选吗", session_id=main.id,
        include_all_projects=True, read_policy="allowed")
    async with get_db_session() as db:
        assert "handled_decisions" not in await view_context.read(db, turn, chat.id)
        value = await view_context.read(db, turn, chat.id, include_history=True)
        [decision] = value["handled_decisions"]
        assert decision["answers"] == [["轻松温暖"]]
        assert decision["execution_confirmed"] is False
        (await db.get(QuestionCheckpoint, "selected-style")).answers = [["幽默"]]
    async with get_db_session() as db:
        fresh = await view_context.read(db, turn, chat.id, include_history=True)
        assert fresh["handled_decisions"][0]["answers"] == [["幽默"]]
        db.add(MemorySource(id="forgotten-card-source", user_id=owner, workspace_id=workspace,
            source_kind="user_statement", session_id=chat.id, message_id=assistant.id,
            part_id="selected-style-part", body="卡片", content_hash="a" * 64, created_at=now))
        db.add(MemoryTombstone(id="forgotten-card-marker", object_kind="source", object_id="forgotten-card-source",
            revision=1, user_id=owner, workspace_id=workspace, deleted_at=now))
    async with get_db_session() as db:
        assert not (await view_context.read(db, turn, chat.id, include_history=True))["handled_decisions"]
