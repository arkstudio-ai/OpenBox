"""requests.reply from a call (docs/ASSISTANT_VOICE_FIX_PLAN.md 3.2).

A spoken answer is a human turn of the main session (entrypoint
``assistant_voice``), and a call that read the whole request aloud records the
same display evidence as the card UI, with ``channel: "voice"``. Everything
else is unchanged: one fresh complete display, then the user's own answer.
"""
import json

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from assistant import request_display, request_reads
from assistant.inputs import accept_turn
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantCommand
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from question.continuation import apply_answers
from session.session import create_assistant_message
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_permissions import call, consume, register  # noqa: F401
from tests.unit.test_assistant_reads import call_tool
from tests.unit.test_assistant_requests import pending
from tool.tool import ToolContext


@pytest.fixture(autouse=True)
def signing(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "request-reply-voice-tests-only")


async def show(scope, request, kind="question", *, voice=True):
    page = await request_display.review(**scope, kind=kind, request_id=request.id)
    extra = {"channel": "voice", "call_id": "call-1"} if voice else {}
    return await request_display.displayed(**scope, display_token=page["display_token"], **extra)


async def turn(scope, text, *, voice=True):
    extra = ({"entrypoint": "assistant_voice", "extra_ref": {"voice_call_id": "call-1"}} if voice else {})
    receipt = await accept_turn(**scope, client_id="voice:call-1:1" if voice else "typed-1", text=text, **extra)
    lease = await reserve_run(scope["main_id"], scope["user_id"])
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
    message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
        agent="assistant", user_id=main.user_id, run_fence=(main.id, lease.run_id, lease.generation))
    ctx = ToolContext(session_id=main.id, user_id=main.user_id, workspace_id=main.workspace_id,
        project_id=main.project_id, agent_id="assistant", message_id=message.id,
        run_id=lease.run_id, run_generation=lease.generation)
    return ctx, lease, batch.messages[0].id


def reply_args(request, human, kind="question"):
    return {"kind": kind, "request_id": request.id, "source_message_id": human,
            "expected_request_revision": request.assistant["request_revision"],
            "options_hash": request.assistant["options_hash"]}


@pytest.mark.parametrize("display_voice,turn_voice", [(True, True), (True, False), (False, True)],
                         ids=["read-aloud-and-spoken", "read-aloud-then-typed", "card-then-spoken"])
async def test_a_spoken_answer_or_a_read_aloud_display_replies_and_applies(display_voice, turn_voice):
    scope, _, request, _ = await pending()
    shown = await show(scope, request, voice=display_voice)
    ctx, lease, human = await turn(scope, "Blue", voice=turn_voice)
    try:
        result, _, _ = await call_tool(ctx, "requests.reply", reply_args(request, human))
        assert not result.metadata.get("error"), result.output
        receipt = json.loads(result.output)
        assert receipt["state"] == "accepted"
        # Applying rechecks the saved human and display evidence.
        assert await apply_answers(request.session_id, ctx.user_id) == request.generation
        value = await request_reads.get_request(**scope, kind="question", request_id=request.id)
        assert value["state"] == "applied"
        async with get_db_session() as db:
            command = await db.get(AssistantCommand, receipt["command_id"])
            event = await db.get(AgentEvent, shown["display_id"])
        assert command.source_ref["kind"] == "human_message"
        assert command.source_ref["human"]["display"]["event_id"] == shown["display_id"]
        assert (event.payload.get("channel"), event.payload.get("call_id")) == (
            ("voice", "call-1") if display_voice else (None, None))
    finally:
        await lease.release(session_status="idle")


async def test_a_spoken_answer_without_a_complete_display_is_refused():
    scope, _, request, _ = await pending()
    ctx, lease, human = await turn(scope, "Blue")
    try:
        result, _, _ = await call_tool(ctx, "requests.reply", reply_args(request, human))
        assert json.loads(result.output)["error"] == "ASSISTANT_REPLY_SOURCE_REQUIRED"
        async with get_db_session() as db:
            assert (await db.get(QuestionCheckpoint, request.id)).answers is None
            assert not await db.scalar(select(AssistantCommand.id).where(AssistantCommand.target_id == request.id))
    finally:
        await lease.release(session_status="idle")


async def test_display_channels_are_closed(monkeypatch):
    scope, _, request, _ = await pending()
    page = await request_display.review(**scope, kind="question", request_id=request.id)
    for extra in ({"channel": "sms"}, {"call_id": "call-1"}, {"channel": "voice", "call_id": "x" * 65}):
        with pytest.raises(ValueError):
            await request_display.displayed(**scope, display_token=page["display_token"], **extra)
    # A display event from any other channel is no evidence for a reply.
    monkeypatch.setattr(request_display, "CHANNELS", frozenset({"voice", "sms"}))
    await request_display.displayed(**scope, display_token=page["display_token"], channel="sms")
    ctx, lease, human = await turn(scope, "Blue")
    try:
        result, _, _ = await call_tool(ctx, "requests.reply", reply_args(request, human))
        assert json.loads(result.output)["error"] == "ASSISTANT_REPLY_SOURCE_REQUIRED"
    finally:
        await lease.release(session_status="idle")


async def test_a_permission_read_aloud_is_granted_once_by_a_spoken_yes(call):
    request, _ = await register(call)
    await show(call.scope, request, "permission")
    ctx, lease, human = await turn(call.scope, "可以")
    try:
        result, _, _ = await call_tool(ctx, "requests.reply", reply_args(request, human, "permission"))
        assert not result.metadata.get("error"), result.output
        assert (await consume(call, request))["action"] == "once"
    finally:
        await lease.release(session_status="idle")
