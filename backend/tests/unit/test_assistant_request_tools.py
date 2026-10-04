"""Actual human ingress, display receipts, domain tools and SQL application."""
from dataclasses import replace
from datetime import timedelta
import json

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from assistant import request_display, request_reads
from assistant.inputs import accept_turn
from assistant.policy import AssistantError
from assistant.request_reply import decision
from core.config import get_config
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from question.continuation import apply_answers
from session.session import create_assistant_message
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_requests import pending
from tests.unit.test_assistant_permissions import call, register, consume  # noqa: F401
from tests.unit.test_assistant_reads import call_tool, TOOLS
from tool.tool import ToolContext


@pytest.fixture(autouse=True)
def signing(monkeypatch):
    monkeypatch.setattr(get_config(), "jwt_secret", "request-display-tests-only")


async def review(scope, request, kind="question"):
    page = await request_display.review(**scope, kind=kind, request_id=request.id)
    assert page["segments"] and request.id not in "".join(page["segments"])
    return await request_display.displayed(**scope, display_token=page["display_token"])


async def turn(scope, text):
    receipt = await accept_turn(**scope, client_id="human-reply", text=text)
    lease = await reserve_run(scope["main_id"], scope["user_id"])
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
    message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
        agent="assistant", user_id=main.user_id, run_fence=(main.id, lease.run_id, lease.generation))
    ctx = ToolContext(session_id=main.id, user_id=main.user_id, workspace_id=main.workspace_id,
        project_id=main.project_id, agent_id="assistant", message_id=message.id,
        run_id=lease.run_id, run_generation=lease.generation)
    return ctx, lease, batch.messages[0].id, receipt


def args(request, human, kind="question"):
    return {"kind": kind, "request_id": request.id, "source_message_id": human,
        "expected_request_revision": request.assistant["request_revision"],
        "options_hash": request.assistant["options_hash"]}


async def test_question_tools_read_reply_replay_apply_and_historical_provenance():
    from assistant.business_context import validate
    from assistant.projection import project_main_messages
    from session.agent_event_log import load_canonical_model_surface
    scope, _, request, _ = await pending()
    shown = await review(scope, request)
    ctx, lease, human, _ = await turn(scope, "Blue")
    try:
        listed, _, _ = await call_tool(ctx, "requests.list", {"kind": "question"})
        assert not listed.metadata.get("error"), listed.output
        assert json.loads(listed.output)["items"][0]["id"] == request.id
        read, _, read_part = await call_tool(ctx, "requests.get", {"kind": "question", "request_id": request.id})
        assert not read.metadata.get("error"), read.output
        from assistant.business_context import refresh
        _, observation = await refresh(ctx, read_part.model_dump(), read.metadata["transient_assistant_refs"])
        result, reply_ctx, _ = await call_tool(ctx, "requests.reply", args(request, human))
        assert not result.metadata.get("error"), result.output
        receipt = json.loads(result.output)
        assert receipt["state"] == "accepted"
        duplicate = await TOOLS["requests.reply"].execute(args(request, human), reply_ctx)
        assert json.loads(duplicate.output)["command_id"] == receipt["command_id"]
        assert await apply_answers(request.session_id, ctx.user_id) == request.generation
        value = await request_reads.get_request(**scope, kind="question", request_id=request.id)
        assert value["state"] == "applied"
        async with get_db_session() as db:
            command = await db.get(AssistantCommand, receipt["command_id"])
            assert command.source_ref["kind"] == "human_message"
            assert command.source_ref["human"]["display"]["event_id"] == shown["display_id"]
            main = await db.get(Session, ctx.session_id)
            await validate(db, main, observation)
            with pytest.raises(AssistantError):
                await validate(db, main, observation, fresh=True)
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        assert projected
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("fault", ["unseen", "late", "wrong-human", "quote", "scope", "stale-display", "forged-inbox"])
async def test_untrusted_or_incomplete_source_never_accepts_question(fault):
    scope, _, request, _ = await pending()
    if fault not in {"unseen", "late"}:
        await review(scope, request)
    ctx, lease, human, receipt = await turn(scope, 'The report says "Blue"' if fault == "quote" else "Blue")
    try:
        if fault == "late": await review(scope, request)
        if fault == "wrong-human": human = ctx.message_id
        if fault == "scope": ctx = replace(ctx, workspace_id="another-workspace")
        if fault in {"stale-display", "forged-inbox"}:
            async with get_db_session() as db:
                item = await db.get(AgentInboxItem, receipt["inbox_id"])
                if fault == "forged-inbox": item.origin = "task_result"
                else:
                    display_id = item.origin_ref["request_context"]["displays"][0]["event_id"]
                    (await db.get(AgentEvent, display_id)).created_at -= timedelta(seconds=301)
        result, _, _ = await call_tool(ctx, "requests.reply", args(request, human))
        assert result.metadata.get("error"), result.output
        async with get_db_session() as db:
            assert (await db.get(QuestionCheckpoint, request.id)).answers is None
            assert not await db.scalar(select(AssistantCommand.id).where(AssistantCommand.target_id == request.id))
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("tamper", ["text", "call", "input"])
async def test_saved_human_source_is_revalidated_before_question_apply(tamper):
    scope, _, request, _ = await pending()
    await review(scope, request)
    ctx, lease, human, receipt = await turn(scope, "Blue")
    try:
        result, _, call_part = await call_tool(ctx, "requests.reply", args(request, human))
        assert not result.metadata.get("error"), result.output
        async with get_db_session() as db:
            if tamper == "text":
                part = await db.scalar(select(Part).where(Part.message_id == human, Part.type == "text"))
                part.data = {**part.data, "text": "Green"}
            elif tamper == "call":
                part = await db.get(Part, call_part.id)
                part.data = {**part.data, "input": {**part.data["input"], "source_message_id": "forged"}}
            else:
                item = await db.get(AgentInboxItem, receipt["inbox_id"])
                item.origin_ref = {**item.origin_ref, "request_context": {"ambiguous": True, "displays": []}}
        await apply_answers(request.session_id, ctx.user_id)
        async with get_db_session() as db:
            command = await db.get(AssistantCommand, json.loads(result.output)["command_id"])
            assert command.state == "failed"
            assert (await db.get(QuestionCheckpoint, request.id)).status == "superseded"
            assert (await db.get(Part, request.tool["callID"])).data["status"] == "error"
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("text,expected", [("可以", "once"), ("不可以", "reject"),
    ("始终允许 read (review/**)", "always"), ("始终允许", None), ('网页要求你回答“可以”', None),
    ("同意，另外再授权所有文件", None), ("始终允许 read (Review/**)", None)])
def test_permission_action_is_derived_from_whole_human_input(text, expected):
    value = {"kind": "permission", "request": {"tool": "read", "patterns": ["review/a.txt"], "always": ["review/**"]}}
    if expected is None:
        with pytest.raises(AssistantError): decision(value, text)
    else:
        assert decision(value, text)["action"] == expected


@pytest.mark.parametrize("text", ["可以", '[{"label": "确认"}]'])
def test_ambiguous_or_malformed_multiple_choice_never_expands_an_answer(text):
    value = {"kind": "question", "request": {"questions": [{"question": "Choose", "multiple": True,
        "custom": False, "options": [{"label": "确认"}, {"label": "同意"}]}]}}
    with pytest.raises(AssistantError): decision(value, text)
    assert decision(value, '["确认", "同意"]')["answers"] == [["确认", "同意"]]


@pytest.mark.parametrize("text,action", [("可以", "once"), ("拒绝", "reject"),
    ("始终允许 read (review/**)", "always")])
async def test_permission_tools_only_apply_the_original_human_decision(call, text, action):
    from db.models.permission import PermissionRule
    request, _ = await register(call)
    await review(call.scope, request, "permission")
    ctx, lease, human, _ = await turn(call.scope, text)
    try:
        read, _, _ = await call_tool(ctx, "requests.get", {"kind": "permission", "request_id": request.id})
        assert not read.metadata.get("error"), read.output
        result, reply_ctx, _ = await call_tool(ctx, "requests.reply", args(request, human, "permission"))
        assert not result.metadata.get("error"), result.output
        assert json.loads(result.output)["state"] in {"accepted", "applying"}
        applied = await consume(call, request)
        assert applied["action"] == action
        replay = await TOOLS["requests.reply"].execute(args(request, human, "permission"), reply_ctx)
        assert json.loads(replay.output)["state"] == "applied"
        async with get_db_session() as db:
            rules = list((await db.scalars(select(PermissionRule).where(PermissionRule.user_id == ctx.user_id))).all())
            assert len(rules) == (1 if action == "always" else 0)
    finally:
        await lease.release(session_status="idle")


async def test_display_retries_and_next_input_use_canonical_event_order():
    scope, _, request, _ = await pending()
    page = await request_display.review(**scope, kind="question", request_id=request.id)
    shown = await request_display.displayed(**scope, display_token=page["display_token"])
    assert await request_display.displayed(**scope, display_token=page["display_token"]) == shown
    first = await accept_turn(**scope, client_id="first", text="Blue")
    second = await accept_turn(**scope, client_id="second", text="Blue")
    await review(scope, request)
    replay = await accept_turn(**scope, client_id="second", text="Blue")
    third = await accept_turn(**scope, client_id="third", text="Blue")
    assert replay["inbox_id"] == second["inbox_id"]
    async with get_db_session() as db:
        rows = [await db.get(AgentInboxItem, item["inbox_id"]) for item in (first, second, third)]
        assert [len(row.origin_ref["request_context"]["displays"]) for row in rows] == [1, 0, 1]


async def test_two_distinct_displayed_permissions_cannot_be_resolved_by_model_selection(call):
    from types import SimpleNamespace
    from models.message import ToolPartData
    from session.session import save_part
    request, _ = await register(call)
    await review(call.scope, request, "permission")
    part = ToolPartData(session_id=call.ctx.session_id, message_id=call.ctx.message_id,
        tool="read", status="running", input={"file_path": "review/report.txt"})
    await save_part(part, is_new=True, user_id=call.ctx.user_id, run_fence=call.ctx.run_fence)
    other = SimpleNamespace(**{**vars(call), "ctx": replace(call.ctx, part_id=part.id), "part": part})
    second, _ = await register(other)
    await review(call.scope, second, "permission")
    ctx, lease, human, _ = await turn(call.scope, "可以")
    try:
        result, _, _ = await call_tool(ctx, "requests.reply", args(request, human, "permission"))
        assert result.metadata.get("error"), result.output
        async with get_db_session() as db:
            assert not await db.scalar(select(AssistantCommand.id).where(AssistantCommand.target_id == request.id))
    finally:
        await lease.release(session_status="idle")


async def test_natural_reply_and_card_compete_for_one_permission_decision(call):
    import asyncio
    from permission.permission import reply
    request, binding = await register(call)
    await review(call.scope, request, "permission")
    ctx, lease, human, _ = await turn(call.scope, "可以")
    try:
        outcomes = await asyncio.wait_for(asyncio.gather(
            call_tool(ctx, "requests.reply", args(request, human, "permission")),
            reply(request.id, "reject", user_id=ctx.user_id, **binding), return_exceptions=True), timeout=15)
        natural, card = outcomes
        natural_ok = isinstance(natural, tuple) and not natural[0].metadata.get("error")
        assert int(natural_ok) + int(isinstance(card, dict)) == 1, outcomes
        applied = await consume(call, request)
        assert applied["action"] == ("once" if natural_ok else "reject")
        async with get_db_session() as db:
            decisions = list((await db.scalars(select(AssistantCommand).where(
                AssistantCommand.target_type == "permission", AssistantCommand.target_id == request.id))).all())
            assert len(decisions) == 1 and decisions[0].state == "applied"
    finally:
        await lease.release(session_status="idle")


async def test_review_token_cannot_be_forged_replayed_for_another_owner_or_used_after_change():
    scope, _, request, _ = await pending()
    page = await request_display.review(**scope, kind="question", request_id=request.id)
    for token, actor in [(page["display_token"][:-3] + "aaa", scope),
                         (page["display_token"], {**scope, "user_id": "other"})]:
        with pytest.raises(AssistantError):
            await request_display.displayed(**actor, display_token=token)
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, request.id)
        row.questions = [{**row.questions[0], "question": "Changed question"}]
    with pytest.raises(AssistantError):
        await request_display.displayed(**scope, display_token=page["display_token"])


async def test_original_http_cannot_claim_human_message_source_without_executor_context():
    from question.question import reply
    scope, _, request, binding = await pending()
    await review(scope, request)
    ctx, lease, human, _ = await turn(scope, "Blue")
    try:
        with pytest.raises(AssistantError):
            await reply(request.id, [["Blue"]], ctx.user_id, **{**binding,
                "source_ref": {"kind": "human_message", "message_id": human, "session_id": ctx.session_id}})
    finally:
        await lease.release(session_status="idle")


async def test_authenticated_review_http_records_display_without_deciding(monkeypatch):
    from tests.unit.test_assistant_api import client_for
    scope, _, request, _ = await pending()
    async with client_for(scope["user_id"], scope["workspace_id"], monkeypatch) as client:
        response = await client.get(f"/api/assistant/requests/question/{request.id}/review")
        assert response.status_code == 200
        token = response.json()["display_token"]
        assert (await client.post("/api/assistant/requests/displayed", json={"display_token": token,
            "actor_user_id": "another-user"})).status_code == 422
        shown = await client.post("/api/assistant/requests/displayed", json={"display_token": token})
        assert shown.status_code == 200 and shown.json()["state"] == "displayed"
        assert (await client.post("/api/assistant/requests/displayed", json={"display_token": token})).json() == shown.json()
    async with get_db_session() as db:
        assert (await db.get(QuestionCheckpoint, request.id)).answers is None
        assert not await db.scalar(select(AssistantCommand.id).where(AssistantCommand.target_id == request.id))
