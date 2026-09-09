"""Entering plan mode has to ask the person who is actually there.

Questions are filed and delivered by user id. plan_enter asked without one,
so the dialog was addressed to the literal user "default": it never appeared
in anyone's pending list, no WS event reached the browser, and the tool
blocked until the run was killed. Build mode could propose plan mode and
never enter it.
"""
import asyncio

import pytest

import question.question as q_mod
from tool.plan import execute_enter, PlanEnterArgs
from tool.tool import ToolContext

USER = "01MREALUSER"


@pytest.fixture
def captured(monkeypatch):
    """Answer any question immediately, recording who it was addressed to."""
    seen: dict = {}

    async def fake_ask(session_id, questions, tool=None, user_id="default", *, continuation=None):
        seen["user_id"] = user_id
        seen["header"] = questions[0].header
        assert continuation["kind"] == "plan_enter"
        return [["Yes"]]

    monkeypatch.setattr("question.question.ask", fake_ask)

    async def fake_get_session(session_id, user_id="default"):
        return None

    async def fake_create_user_message(**kwargs):
        seen["switch_agent"] = kwargs.get("agent")
        seen["message_user_id"] = kwargs.get("user_id")
        return None

    monkeypatch.setattr("session.session.get_session", fake_get_session)
    monkeypatch.setattr("session.session.create_user_message", fake_create_user_message)
    return seen


async def test_the_question_goes_to_the_real_user(captured):
    ctx = ToolContext(session_id="s1", user_id=USER, message_id="m1", part_id="p1")
    await execute_enter(PlanEnterArgs(), ctx)
    assert captured["user_id"] == USER


async def test_it_is_the_plan_mode_question(captured):
    ctx = ToolContext(session_id="s1", user_id=USER)
    await execute_enter(PlanEnterArgs(), ctx)
    assert captured["header"] == "Plan Mode"


async def test_saying_yes_hands_the_session_to_the_plan_agent(captured):
    ctx = ToolContext(session_id="s1", user_id=USER)
    await execute_enter(PlanEnterArgs(), ctx)
    assert captured["switch_agent"] == "plan"


async def test_the_switch_message_belongs_to_the_real_user_too(captured):
    ctx = ToolContext(session_id="s1", user_id=USER)
    await execute_enter(PlanEnterArgs(), ctx)
    assert captured["message_user_id"] == USER


async def test_saying_no_keeps_build_mode(monkeypatch):
    from tool.plan import PlanRejectedError

    async def say_no(session_id, questions, tool=None, user_id="default", *, continuation=None):
        return [["No"]]

    monkeypatch.setattr("question.question.ask", say_no)

    async def fake_get_session(session_id, user_id="default"):
        return None

    monkeypatch.setattr("session.session.get_session", fake_get_session)
    with pytest.raises(PlanRejectedError):
        await execute_enter(PlanEnterArgs(), ToolContext(session_id="s1", user_id=USER))


async def test_a_missing_users_durable_pending_list_is_empty():
    # Cross-user isolation is covered with real SQL checkpoints in
    # test_durable_questions; there is no process-local pending dictionary.
    assert await q_mod.list_pending(user_id=USER) == []
