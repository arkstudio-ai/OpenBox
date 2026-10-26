"""A suspended turn re-reads authorized memory; a saved tool receipt is not a packet."""
from types import SimpleNamespace

import pytest

from agent import recall_ahead
from models.message import MessageWithParts, TextPart, ToolPartData
from tests.unit.test_memory_lazy_read import preparation_args
from tests.unit.test_memory_read_protocol import context
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401


def surface(session_id="s", turn_id="u", query="把刚才的开场白改成两句。"):
    human = MessageWithParts(id=turn_id, session_id=session_id, role="user",
        parts=[TextPart(text=query)])
    tool = ToolPartData(tool="provider_alias", canonical_tool_id="memory_prepare", status="completed",
        session_id=session_id, message_id="a", input=preparation_args().model_dump(),
        output='{"status":"complete","items":["THIS IS NOT AUTHORITY"]}')
    tool.input["plan"]["recent_context_useful"] = 1.0
    assistant = MessageWithParts(id="a", session_id=session_id, parent_id=turn_id,
        role="assistant", parts=[tool])
    return human, assistant


def inputs(history, human, *, user_id="owner", workspace_id="w", **changes):
    session = SimpleNamespace(id=human.session_id, workspace_id=workspace_id, project_id=None)
    return recall_ahead.recall_inputs(session, human, history, user_id=user_id,
        assistant_scope=True, run_id="new-run", model_id="test/model", **changes)


async def test_resumed_turn_rebuilds_context_without_another_model_and_reads_current_settings(monkeypatch):
    from assistant.profile import save
    config, original = await context(monkeypatch)
    config.lazy_read_planning = True
    human, assistant = surface(original.session_id, "same-human", original.query)
    request = inputs([human, assistant], human, user_id=original.user_id, workspace_id=original.workspace_id)

    async def forbidden(*args, **kwargs):
        pytest.fail("The existing same-turn semantic proposal needs no extra model request")
    monkeypatch.setattr("memory.providers.configured_read.evaluate", forbidden)
    monkeypatch.setattr("memory.read_routing.evaluate_read_needs", forbidden)
    first = await recall_ahead.recall(request, session_id=original.session_id, turn_id=human.id, config=config)
    assert first["route"]["read_policy"] == "allowed"
    assert not first["_turn"].deferred_read
    assert first["route"]["recent_context_useful"] == 1
    await save(original.user_id, {"address": "新称呼"})
    resumed = await recall_ahead.recall(request, session_id=original.session_id, turn_id=human.id, config=config)
    assert resumed is not first
    assert resumed["settings"]["address"] == "新称呼"
    assert resumed["context_version"] != first["context_version"]
    assert "THIS IS NOT AUTHORITY" not in str(resumed)


@pytest.mark.parametrize("change", ["parent", "session", "role", "pending", "canonical", "invalid", "new_turn"])
async def test_plan_cannot_be_borrowed_from_unrelated_or_invalid_tool_evidence(monkeypatch, change):
    config, original = await context(monkeypatch)
    config.lazy_read_planning = True
    human, assistant = surface(original.session_id, "same-human", original.query)
    part = assistant.parts[0]
    if change == "parent": assistant.parent_id = "another-human"
    if change == "session": assistant.session_id = "another-session"
    if change == "role": assistant.role = "user"
    if change == "pending": part.status = "pending"
    if change == "canonical": part.canonical_tool_id = "untrusted_tool"; part.tool = "memory_prepare"
    if change == "invalid": part.input["plan"]["invented_authority"] = True
    history = [human, assistant]
    if change == "new_turn":
        human = human.model_copy(update={"id": "new-human"}); history.append(human)
    request = inputs(history, human, user_id=original.user_id, workspace_id=original.workspace_id)
    result = await recall_ahead.recall(request, session_id=original.session_id, turn_id=human.id, config=config)
    assert result["status"] == "deferred"
    assert result["route"]["memory_forbidden"]


async def test_resume_preserves_read_restriction_and_first_effective_plan(monkeypatch):
    config, original = await context(monkeypatch)
    config.lazy_read_planning = True
    human, assistant = surface(original.session_id, "same-human", "只看这句话，不要查历史。")
    restriction = {"role": "access", "policy": "current_only", "quote": "不要查历史", "confidence": 1.0}
    plan = assistant.parts[0].input["plan"]
    plan.update(restriction=restriction, memory="skip", task="skip", recent_context_useful=0.0)
    _, later = surface(original.session_id, "same-human", human.parts[0].text)
    request = inputs([human, assistant, later], human, user_id=original.user_id, workspace_id=original.workspace_id)
    result = await recall_ahead.recall(request, session_id=original.session_id, turn_id=human.id, config=config)
    assert result["route"]["read_policy"] == "current_only"
    assert not result["items"] and not result["stable_background"]["items"]


async def test_canonical_storage_roundtrip_restores_plan_but_revoked_session_still_denies(monkeypatch):
    from core.identifier import ascending
    from db.base import get_db_session
    from db.models.session import Session
    from memory.policy import MemoryAccessDenied
    from session.session import create_user_message, create_assistant_message, save_part
    from session.agent_event_log import load_canonical_model_surface
    config, original = await context(monkeypatch)
    config.lazy_read_planning = True
    human = await create_user_message(original.session_id, original.query, user_id=original.user_id,
        agent="assistant", origin="human", origin_ref={"actor_user_id": original.user_id})
    assistant = await create_assistant_message(original.session_id, human.id,
        user_id=original.user_id, agent="assistant")
    _, fixture = surface(original.session_id, human.id, original.query)
    tool = fixture.parts[0].model_copy(update={"id": ascending("part"), "message_id": assistant.id,
        "wire_tool_name": "provider_alias", "provider_binding_digest": "a" * 64,
        "provider_dialect": "openai", "stream_seq": 0})
    await save_part(tool, is_new=True, user_id=original.user_id)
    stored = await load_canonical_model_surface(original.session_id, user_id=original.user_id)
    anchor = next(m for m in stored.messages if m.id == human.id)
    request = inputs(stored.messages, anchor, user_id=original.user_id, workspace_id=original.workspace_id)
    result = await recall_ahead.recall(request, session_id=original.session_id, turn_id=human.id, config=config)
    assert result["route"]["read_policy"] == "allowed"
    async with get_db_session() as db:
        (await db.get(Session, original.session_id)).is_deleted = True
    with pytest.raises(MemoryAccessDenied):
        await recall_ahead.recall(request, session_id=original.session_id, turn_id=human.id, config=config)
