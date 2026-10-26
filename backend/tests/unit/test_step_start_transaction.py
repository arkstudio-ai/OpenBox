"""Step start cannot outlive or be published before its message transaction."""
import pytest

from db.base import get_db_session
from db.models.message import Message
from db.models.part import Part
from models.message import StepStartPart
from session.session import create_assistant_message, create_user_message
from session.agent_event_log import load_canonical_model_surface
from tests.unit.test_durable_questions import state  # noqa: F401


async def test_message_and_step_commit_together_and_publish_in_order(state):
    user = await create_user_message("s1", "hello", user_id="u1")
    start = StepStartPart(session_id="s1", message_id="", step=1)
    answer = await create_assistant_message("s1", user.id, user_id="u1", step_start=start)
    assert start.message_id == answer.id
    async with get_db_session() as db:
        row = await db.get(Part, start.id)
        assert row.message_id == answer.id and row.user_id == "u1"
    events = [(e, p) for e, p in state if p.get("messageId") == answer.id or p.get("message", {}).get("id") == answer.id]
    assert [event for event, _ in events] == ["message.created", "part.created"]
    assert "session_id" not in events[-1][1]["part"]
    surface = await load_canonical_model_surface("s1", user_id="u1", repair_tail=False)
    assert surface.messages[-1].parts[0].id == start.id


async def test_failure_to_append_step_rolls_back_message_and_publishes_nothing(state, monkeypatch):
    user = await create_user_message("s1", "hello", user_id="u1")
    count = len(state)
    async def fail(*args, **kwargs):
        raise RuntimeError("injected write failure")
    monkeypatch.setattr("session.agent_event_log.append_part_event_locked", fail)
    start = StepStartPart(session_id="s1", message_id="", step=1)
    with pytest.raises(RuntimeError, match="injected"):
        await create_assistant_message("s1", user.id, user_id="u1", step_start=start)
    assert len(state) == count
    async with get_db_session() as db:
        assert await db.get(Message, start.message_id) is None
        assert await db.get(Part, start.id) is None


async def test_initial_step_cannot_be_attached_across_sessions(state):
    with pytest.raises(ValueError, match="Initial step"):
        await create_assistant_message("s1", "parent", user_id="u1",
            step_start=StepStartPart(session_id="s2", step=1))
