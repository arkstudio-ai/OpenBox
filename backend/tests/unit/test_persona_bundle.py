"""Persona bundle: several CANDIDATE memories, one card, one continuation."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api.stores import StoreCreate, create_store
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.session import Session as SessionRow
from db.repository.user_repo import PgUserRepo
from memory import service as memory_service
from question import continuation
from store import service as store_service
from tool.creator_context import CreatorContextArgs


async def _user() -> dict:
    suffix = uuid4().hex[:10]
    row = await PgUserRepo().create(id=f"bundle_{suffix}", username=f"bundle-{suffix}",
                                    email=f"bundle-{suffix}@example.test", password_hash="unused")
    return {"user_id": row["id"], "role": "user", "workspace_id": row["default_workspace_id"]}


async def _candidates(user: dict, n: int = 2) -> list[str]:
    ids = []
    for index, type_name in enumerate(("IDENTITY", "OFFERING", "VOICE")[:n]):
        row = await memory_service.write_memory(
            user_id=user["user_id"], workspace_id=user["workspace_id"], scope="LONG_TERM", type=type_name,
            value={"summary": f"事实 {index}"}, owner="SYSTEM_INFERRED", confidence=60,
            evidence={"source": "store-persona-init", "awaiting_confirm": True},
        )
        ids.append(row["id"])
    return ids


def test_parse_bundle_answer():
    assert memory_service.parse_bundle_answer("确认") == ("confirmed", {}, "")
    assert memory_service.parse_bundle_answer("稍后") == ("postponed", {}, "")
    assert memory_service.parse_bundle_answer("") == ("postponed", {}, "")
    decision, edits, note = memory_service.parse_bundle_answer('{"confirm": true, "items": {"m1": "改过的"}, "note": "ok"}')
    assert decision == "confirmed" and edits == {"m1": "改过的"} and note == "ok"
    assert memory_service.parse_bundle_answer('{"confirm": false, "items": {}}')[0] == "postponed"
    assert memory_service.parse_bundle_answer("{not json")[0] == "text"
    assert memory_service.parse_bundle_answer("其实我们主打烧烤")[0] == "text"


def test_propose_bundle_args_validation():
    ok = CreatorContextArgs(action="propose_bundle", items=[{"type": "IDENTITY", "summary": "a"}, {"type": "VOICE", "summary": "b"}])
    assert [item.type for item in ok.items] == ["IDENTITY", "VOICE"]
    with pytest.raises(ValueError):
        CreatorContextArgs(action="propose_bundle", items=[])
    with pytest.raises(ValueError):
        CreatorContextArgs(action="propose_bundle", items=[{"type": "IDENTITY", "summary": "a"}, {"type": "IDENTITY", "summary": "b"}])
    with pytest.raises(ValueError):
        CreatorContextArgs(action="propose_bundle", items=[{"type": "USER_NOTE", "summary": "a"}])
    with pytest.raises(ValueError):
        CreatorContextArgs(action="propose_bundle", items=[{"type": "IMPRESSION", "summary": "a"}])


async def test_apply_bundle_answer_confirm_edit_and_postpone():
    user = await _user()
    ids = await _candidates(user, 2)
    async with get_db_session() as db:
        result = await memory_service.apply_bundle_answer(
            db, user_id=user["user_id"], workspace_id=user["workspace_id"], memory_ids=ids, answer="稍后")
        assert result["decision"] == "postponed"
    async with get_db_session() as db:
        assert {(await db.get(UserMemory, i)).status for i in ids} == {"CANDIDATE"}

    async with get_db_session() as db:
        result = await memory_service.apply_bundle_answer(
            db, user_id=user["user_id"], workspace_id=user["workspace_id"], memory_ids=ids,
            answer='{"confirm": true, "items": {"%s": "改过的身份"}}' % ids[0])
        assert result["decision"] == "confirmed" and result["edited"] == [ids[0]]
    async with get_db_session() as db:
        first, second = await db.get(UserMemory, ids[0]), await db.get(UserMemory, ids[1])
        assert first.status == second.status == "ACTIVE"
        assert first.owner == "USER_CONFIRMED" and first.confidence == 90
        assert first.value["summary"] == "改过的身份" and second.value["summary"] == "事实 1"
    # Confirmed rows now reach the assembled context; before, CANDIDATE rows did too but
    # only ACTIVE ones carry USER_CONFIRMED ownership.
    active = await memory_service.list_active_memories(user_id=user["user_id"], workspace_id=user["workspace_id"])
    assert {row["id"] for row in active} >= set(ids)

    # Another user's ids are invisible: nothing to apply.
    stranger = await _user()
    async with get_db_session() as db:
        with pytest.raises(ValueError):
            await memory_service.apply_bundle_answer(
                db, user_id=stranger["user_id"], workspace_id=stranger["workspace_id"], memory_ids=ids, answer="确认")


async def test_continuation_bundle_confirms_and_activates_store_persona(monkeypatch):
    user = await _user()
    store = await create_store(StoreCreate(name="泽岚鲜果"), current_user=user)
    ids = await _candidates(user, 2)
    from session.session import create_session
    session = await create_session(user_id=user["user_id"], workspace_id=user["workspace_id"], title="你的店")
    emitted: list[dict] = []

    async def fake_emit(db, **kwargs):
        emitted.append(kwargs)

    import notifications.events as events
    monkeypatch.setattr(events, "emit", fake_emit)

    def _row(status: str, answers):
        return SimpleNamespace(
            id=f"q_{uuid4().hex[:6]}", session_id=session.id, user_id=user["user_id"], status=status,
            questions=[{"question": "确认人设?", "detail": {"kind": "store_persona_bundle"}}], answers=answers,
            continuation={"kind": "memory_proposal", "memory_ids": ids, "workspace_id": user["workspace_id"]},
        )

    async with get_db_session() as db:
        db_session = await db.get(SessionRow, session.id)
        result, events_out = await continuation._apply(db, db_session, _row("rejected", None))
        assert result["metadata"]["decision"] == "dismissed" and events_out == []

        result, _ = await continuation._apply(db, db_session, _row("answered", [["稍后"]]))
        assert result["metadata"]["decision"] == "postponed"

        result, _ = await continuation._apply(db, db_session, _row("answered", [["其实我们主打烧烤"]]))
        assert result["metadata"]["decision"] == "text" and "propose_bundle" in result["output"]

        result, _ = await continuation._apply(db, db_session, _row("answered", [["确认"]]))
        assert result["metadata"]["decision"] == "confirmed"
        assert "Saved as ACTIVE" in result["output"]

    updated = await store_service.get_store(user["workspace_id"])
    assert updated["id"] == store["id"] and updated["personaStatus"] == "active"
    assert emitted and emitted[0]["kind"] == "persona_ready" and emitted[0]["session_id"] == session.id
    async with get_db_session() as db:
        assert {(await db.get(UserMemory, i)).status for i in ids} == {"ACTIVE"}


async def test_single_memory_proposal_path_unchanged():
    """The legacy single-note continuation still resolves a PENDING_NOTE by `memory_id`."""
    user = await _user()
    proposal = await memory_service.propose_note(user_id=user["user_id"], workspace_id=user["workspace_id"], summary="待确认")
    from session.session import create_session
    session = await create_session(user_id=user["user_id"], workspace_id=user["workspace_id"])
    row = SimpleNamespace(
        id="q1", session_id=session.id, user_id=user["user_id"], status="answered",
        questions=[{"question": "要记住吗", "detail": {"kind": "memory_proposal", "summary": "待确认"}}], answers=[["记住"]],
        continuation={"kind": "memory_proposal", "memory_id": proposal["id"], "workspace_id": user["workspace_id"]},
    )
    async with get_db_session() as db:
        db_session = await db.get(SessionRow, session.id)
        result, _ = await continuation._apply(db, db_session, row)
        assert result["metadata"]["decision"] == "confirmed"
