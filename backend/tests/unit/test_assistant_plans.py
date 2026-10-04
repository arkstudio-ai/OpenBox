"""Exact plan approval through real Question, Task and provider continuations."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from agent import inbox, loop, processor
from assistant.policy import AssistantError
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from db.models.external_effect import ExternalEffect
from db.models.message import Message
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from models.message import ToolPartData
from question import question as q, runtime
from question.continuation import apply_answers
from session.agent_event_log import verify_agent_event_parity
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_steering import running
from tool.plan import PlanExitArgs, execute_exit, plan_enter_tool, plan_exit_tool
from tool.tool import ToolContext

PLAN = "# Exact plan\n\n1. Save a blue report.\n2. Keep the original source.\n"


class PlanSandbox:
    def __init__(self, content=PLAN):
        self.content = content
        self.reads = []
        self.commands = []

    async def read_file_raw(self, path):
        self.reads.append(path)
        return self.content

    async def execute(self, command, **kwargs):
        self.commands.append(command)
        return SimpleNamespace(stdout="exists", exit_code=0)


async def pending(*, content=PLAN):
    args, created, lease, batch = await running()
    sandbox = PlanSandbox(content)
    ticket = await runtime.start_run(lease.session_id, lease.user_id, driver_lease=lease)
    token = runtime.current_run.set(ticket)
    fence = (lease.session_id, lease.run_id, lease.generation)
    try:
        message = await create_assistant_message(lease.session_id, batch.messages[0].id, agent="plan",
            model_id="test/model", user_id=lease.user_id, run_fence=fence)
        part = ToolPartData(session_id=lease.session_id, message_id=message.id, tool="plan_exit", status="running", input={})
        await save_part(part, is_new=True, user_id=lease.user_id, run_fence=fence)
        ctx = ToolContext(session_id=lease.session_id, user_id=lease.user_id, workspace_id=args["workspace_id"],
            message_id=message.id, part_id=part.id, sandbox=sandbox, run_id=lease.run_id, run_generation=lease.generation)
        with pytest.raises(q.QuestionSuspended) as suspended:
            await execute_exit(PlanExitArgs(), ctx)
        # A retried call must reuse the first review, not read a replacement file.
        sandbox.content = "A later, unapproved plan"
        with pytest.raises(q.QuestionSuspended) as replay:
            await execute_exit(PlanExitArgs(), ctx)
        assert replay.value.request_id == suspended.value.request_id
        assert len(sandbox.reads) == 1
        message.finish = "waiting_input"
        await update_message_info(message, user_id=lease.user_id, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
        await runtime.finish_run(ticket)
    finally:
        runtime.current_run.reset(token)
        await lease.release(session_status="waiting_input")
    request = await q.get_request(suspended.value.request_id, lease.user_id)
    binding = {"reply_id": "plan-reply", "expected_request_revision": request.assistant["request_revision"],
        "options_hash": request.assistant["options_hash"], "source_ref": {"kind": "card"}}
    return args, created, request, binding, batch.messages[0].id


async def proposal_for(request):
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, request.id)
        return await db.get(Part, row.continuation["plan_part_id"])


@pytest.mark.parametrize("choice", ["Approve", "Revise", None])
async def test_review_restores_exact_file_and_applies_once_in_the_original_turn(choice):
    args, created, request, binding, original = await pending()
    assert request.questions[0].question.endswith(PLAN)
    assert not request.questions[0].custom
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    answer = [[choice]] if choice else None
    async def reply():
        return await (q.reply(request.id, answer, args["user_id"], **binding) if choice
                      else q.reject(request.id, args["user_id"], **binding))
    left, right = await asyncio.gather(reply(), reply())
    assert left == right
    assert await apply_answers(request.session_id, args["user_id"]) == request.generation
    assert await apply_answers(request.session_id, args["user_id"]) == request.generation
    assert (await reply())["state"] == "applied"
    async with get_db_session() as db:
        session = await db.get(Session, request.session_id)
        assert session.agent == ("build" if choice == "Approve" else "plan")
        assert await db.scalar(select(func.count()).select_from(Message).where(
            Message.session_id == session.id, Message.role == "user")) == 1
        assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(
            TaskSubmission.task_id == created["task_id"])) == 1
        assert not await db.scalar(select(TaskResult.id).where(TaskResult.task_id == created["task_id"]))
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
            AssistantCommand.target_type == "question", AssistantCommand.target_id == request.id)) == 1
        call = await db.get(Part, request.tool["callID"])
        assert call.data["metadata"]["approved"] is (choice == "Approve")
        assert call.data["metadata"]["reply_ref"]["command_id"] == left["command_id"]
        assert "later, unapproved" not in call.data["output"]
    from assistant.plans import agent_for_turn
    assert await agent_for_turn(session, original) == session.agent
    assert (await proposal_for(request)).data["status"] == ("accepted" if choice == "Approve" else "rejected")


@pytest.mark.parametrize("when", ["before_reply", "before_apply"])
@pytest.mark.parametrize("change", ["content", "path", "reference", "status"])
async def test_replaced_plan_never_applies_approval(when, change):
    args, _, request, binding, _ = await pending()
    if when == "before_apply":
        receipt = await q.reply(request.id, [["Approve"]], args["user_id"], **binding)
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, request.id)
        part = await db.get(Part, row.continuation["plan_part_id"])
        if change == "reference":
            row.continuation = {**row.continuation, "plan_part_id": "missing-plan"}
        else:
            part.data = {**part.data, change: "replacement"}
    if when == "before_reply":
        with pytest.raises(q.QuestionGone):
            await q.reply(request.id, [["Approve"]], args["user_id"], **binding)
    else:
        assert await apply_answers(request.session_id, args["user_id"]) is None
        async with get_db_session() as db:
            assert (await db.get(AssistantCommand, receipt["command_id"])).state == "failed"
            assert (await db.get(Part, request.tool["callID"])).data["status"] == "error"


async def test_two_devices_cannot_approve_and_reject_one_plan():
    args, _, request, binding, _ = await pending()
    outcomes = await asyncio.gather(q.reply(request.id, [["Approve"]], args["user_id"], **binding),
        q.reply(request.id, [["Revise"]], args["user_id"], **{**binding, "reply_id": "other-device"}),
        return_exceptions=True)
    assert sum(isinstance(value, dict) for value in outcomes) == 1
    assert sum(isinstance(value, q.QuestionConflict) for value in outcomes) == 1


@pytest.mark.parametrize("change", ["paused", "canceled", "membership"])
async def test_accepted_review_respects_task_and_current_authority(change):
    args, created, request, binding, _ = await pending()
    receipt = await q.reply(request.id, [["Approve"]], args["user_id"], **binding)
    async with get_db_session() as db:
        if change == "membership":
            (await db.get(WorkspaceMember, (args["workspace_id"], args["user_id"]))).status = "removed"
        else:
            (await db.get(AssistantTask, created["task_id"])).desired_state = change
    assert await apply_answers(request.session_id, args["user_id"]) is None
    assert (await proposal_for(request)).data["status"] == "ready"
    async with get_db_session() as db:
        assert (await db.get(AssistantCommand, receipt["command_id"])).state == ("accepted" if change == "paused" else "failed")


@pytest.mark.parametrize("content", ["", " \n", "中" * 21846], ids=["empty", "whitespace", "oversized-utf8"])
async def test_empty_or_oversized_plan_cannot_create_review(content):
    async with get_db_session() as db:
        questions = await db.scalar(select(func.count()).select_from(QuestionCheckpoint))
        plans = await db.scalar(select(func.count()).select_from(Part).where(Part.type == "plan"))
    with pytest.raises(AssistantError) as error:
        await pending(content=content)
    assert error.value.code == "ASSISTANT_PLAN_UNAVAILABLE"
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(QuestionCheckpoint)) == questions
        assert await db.scalar(select(func.count()).select_from(Part).where(Part.type == "plan")) == plans


async def test_old_plan_routes_refuse_managed_tasks_before_sandbox_or_raw_run(monkeypatch):
    from api import sessions as routes
    args, created, request, _, _ = await pending()
    async def forbidden(*args, **kwargs):
        pytest.fail("Legacy plan actions must not obtain a sandbox or reserve a raw run")
    monkeypatch.setattr(routes, "_reserve_prompt_run", forbidden)
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", forbidden)
    actor = {"user_id": args["user_id"], "workspace_id": args["workspace_id"]}
    for action in (routes.accept_plan, routes.reject_plan):
        with pytest.raises(HTTPException) as error:
            await action(created["execution_session_id"], current_user=actor)
        assert error.value.detail["code"] == "ASSISTANT_PLAN_REVIEW_REQUIRED"
    with pytest.raises(HTTPException):
        await routes.update_plan(request.session_id, routes.PlanUpdateBody(content="replacement"), current_user=actor)


@pytest.mark.parametrize("change", ["plan", "decision", "tool_output"])
async def test_applied_plan_is_rechecked_for_provider_context(change):
    from assistant.plans import validate_context
    from session.session import get_messages
    args, _, request, binding, _ = await pending()
    receipt = await q.reply(request.id, [["Approve"]], args["user_id"], **binding)
    await apply_answers(request.session_id, args["user_id"])
    messages = await get_messages(request.session_id, user_id=args["user_id"])
    ctx = ToolContext(session_id=request.session_id, user_id=args["user_id"])
    assert await validate_context(messages, ctx)
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, request.id)
        if change == "plan":
            proposal = await db.get(Part, row.continuation["plan_part_id"])
            proposal.data = {**proposal.data, "content": "Changed after approval"}
        elif change == "decision":
            (await db.get(AssistantCommand, receipt["command_id"])).state = "failed"
        else:
            call = next(p for msg in messages for p in msg.parts if p.id == request.tool["callID"])
            call.output = "Implement an unapproved replacement"
    with pytest.raises((AssistantError, q.QuestionGone)):
        await validate_context(messages, ctx)


async def test_isolated_child_returns_plan_to_parent_without_legacy_card():
    from session.session import create_session
    args, created, _, _, _ = await pending()
    child = await create_session(user_id=args["user_id"], workspace_id=args["workspace_id"],
        parent_id=created["execution_session_id"], visibility="private")
    with pytest.raises(AssistantError) as error:
        await execute_exit(PlanExitArgs(), ToolContext(session_id=child.id, user_id=args["user_id"]))
    assert error.value.code == "ASSISTANT_PLAN_PARENT_REQUIRED"


async def test_ordinary_plan_keeps_legacy_card_flow():
    from api.sessions import _require_legacy_plan
    from models.message import PlanPart
    from session.session import create_session, create_user_message
    from tests.unit.test_assistant_foundation import accounts
    owner, _, workspace = await accounts()
    session = await create_session(user_id=owner, workspace_id=workspace)
    original = await create_user_message(session.id, "Write a plan", user_id=owner)
    message = await create_assistant_message(session.id, original.id, agent="plan", model_id="test/model", user_id=owner)
    plan = PlanPart(session_id=session.id, message_id=message.id, path="/workspace/ordinary.md", content=PLAN)
    await save_part(plan, is_new=True, user_id=owner)
    await _require_legacy_plan(session, owner)
    result = await execute_exit(PlanExitArgs(), ToolContext(session_id=session.id, user_id=owner, message_id=message.id))
    assert result.metadata == {"plan_ready": True}
    async with get_db_session() as db:
        saved = await db.get(Part, plan.id)
        assert saved.data["status"] == "ready" and saved.data["content"] == PLAN
        assert saved.data["review_via_question"] is False
        assert not await db.scalar(select(QuestionCheckpoint.id).where(QuestionCheckpoint.session_id == session.id))


async def test_revision_draft_cannot_overwrite_the_previous_review_snapshot():
    from agent.driver import reserve_run
    from session.session import get_session
    args, _, request, binding, original = await pending()
    await q.reply(request.id, [["Revise"]], args["user_id"], **binding)
    await apply_answers(request.session_id, args["user_id"])
    lease = await reserve_run(request.session_id, args["user_id"], trigger_message_id=original)
    await lease.set_phase("running")
    fence = (lease.session_id, lease.run_id, lease.generation)
    try:
        message = await create_assistant_message(lease.session_id, original, agent="plan", model_id="test/model",
            user_id=lease.user_id, run_fence=fence)
        await save_part(ToolPartData(session_id=lease.session_id, message_id=message.id, tool="write", status="completed",
            input={"file_path": "/workspace/.openbox/plans/new.md", "content": "A revised draft"}),
            is_new=True, user_id=lease.user_id, run_fence=fence)
        session = await get_session(lease.session_id, user_id=lease.user_id)
        await loop._upsert_plan_part(lease.session_id, message.id, None, session, user_id=lease.user_id, run_fence=fence)
        assert (await proposal_for(request)).data["content"] == PLAN
        assert (await proposal_for(request)).data["status"] == "rejected"
        async with get_db_session() as db:
            draft = await db.scalar(select(Part).where(Part.message_id == message.id, Part.type == "plan"))
            assert draft.data["content"] == "A revised draft" and draft.data["review_via_question"]
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("changed_after_step", [False, True])
async def test_plan_entry_review_and_execution_complete_one_task_after_restarts(monkeypatch, changed_after_step):
    from tool.tool import ToolResult, define_tool
    config = _loop_config()
    config.permission = {"*": "allow"}
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    sandbox = PlanSandbox()
    async def no_title(*args, **kwargs): return None
    monkeypatch.setattr(loop, "_ensure_title", no_title)
    async def check(*args, **kwargs): return ToolResult(output="Checked the approved version")
    check_tool = define_tool("fixture_check", description="Check the fixture", parameters=PlanExitArgs,
        execute=check, sandbox_required=False)
    async def get_sandbox(*args, **kwargs): return sandbox
    async def tools(*args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in (plan_enter_tool, plan_exit_tool, check_tool)},
            catalogue_availability="available")
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", get_sandbox)
    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    args, created, lease, batch = await running()
    calls = []
    async def stream(**kwargs):
        calls.append(kwargs["ctx"].agent_id)
        phase = len(calls)
        if phase <= 2:
            name = "plan_enter" if phase == 1 else "plan_exit"
            assert kwargs["ctx"].agent_id == ("build" if phase == 1 else "plan")
            yield {"type": "tool_call", "tool": name, "args": {}, "call_id": name, "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            assert kwargs["ctx"].agent_id == "build"
            evidence = json.dumps(kwargs["messages"], ensure_ascii=False)
            assert "Save a blue report" in evidence and "recorded plan review" in evidence
            assert "later, unapproved" not in evidence
            if phase == 3:
                if changed_after_step:
                    async with get_db_session() as db:
                        row = await db.scalar(select(QuestionCheckpoint).where(
                            QuestionCheckpoint.session_id == lease.session_id).order_by(QuestionCheckpoint.created_at.desc()))
                        part = await db.get(Part, row.continuation["plan_part_id"])
                        part.data = {**part.data, "content": "Changed after the first build step"}
                yield {"type": "tool_call", "tool": "fixture_check", "args": {}, "call_id": "check", "invalid": False}
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            yield {"type": "text_delta", "text": "Implemented the exact approved blue report."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", stream)
    await loop.run_loop(lease.session_id, user_id=lease.user_id, lease=lease)
    for choice in ("Yes", "Approve"):
        requests = await q.list_pending(args["user_id"])
        assert len(requests) == 1
        request = requests[0]
        binding = {"reply_id": "reply-" + choice,
            "expected_request_revision": request.assistant["request_revision"],
            "options_hash": request.assistant["options_hash"], "source_ref": {"kind": "card"}}
        await q.reply(request.id, [[choice]], args["user_id"], **binding)
        generation = await apply_answers(request.session_id, args["user_id"])
        sandbox.content = "A later, unapproved plan" if choice == "Approve" else PLAN
        url = get_engine().url.render_as_string(hide_password=False)
        await close_engine()
        init_engine(url)
        await loop.run_loop(request.session_id, user_id=args["user_id"], expected_generation=generation)
    assert calls == (["build", "plan", "build"] if changed_after_step else ["build", "plan", "build", "build"])
    async with get_db_session() as db:
        results = (await db.scalars(select(TaskResult).where(TaskResult.task_id == created["task_id"]))).all()
        if changed_after_step:
            result, = results
            assert result.outcome == "error" and result.observed_intent_revision == 1
            assert result.consumed_inbox_ids == [created["inbox_id"]]
            assert (await db.get(AssistantTask, created["task_id"])).observed_state == "error"
            return
        result, = results
        assert result.outcome == "succeeded" and result.observed_intent_revision == 1
        assert result.consumed_inbox_ids == [created["inbox_id"]]
        assert await db.scalar(select(func.count()).select_from(Message).where(
            Message.session_id == lease.session_id, Message.role == "user")) == 1
        assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(
            TaskSubmission.task_id == created["task_id"])) == 1
    assert (await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)).ok


# Real SandboxClient request hooks and effect ledger, synthetic remote response.
from tests.unit.test_assistant_resource_control import resource  # noqa: E402,F401
from tests.unit.test_assistant_resource_gateway import gateway, new_tool_call  # noqa: E402,F401


async def test_physical_snapshot_settles_before_question_suspends(gateway, resource, monkeypatch):
    from agent.hooks import ToolHooks
    ctx, sent, transport = gateway
    _, _, lease, _ = resource
    async def respond(request):
        await transport(request)
        return httpx.Response(200, json={"exit_code": 0, "stdout": PLAN, "stderr": ""})
    ctx.sandbox._transport = httpx.MockTransport(respond)
    await new_tool_call(ctx, "plan_exit", {})
    ticket = await runtime.start_run(ctx.session_id, ctx.user_id, driver_lease=lease)
    token = runtime.current_run.set(ticket)
    hooks = ToolHooks(ctx.session_id, ctx.user_id)
    async def allow(*args, **kwargs): return None
    monkeypatch.setattr(hooks, "authorize_tool", allow)
    try:
        prepared = await hooks.prepare_execute("plan_exit", plan_exit_tool.execute, {}, ctx,
            part_id=ctx.part_id, isolate_context=True)
        with pytest.raises(q.QuestionSuspended):
            await hooks.dispatch_execute(prepared)
        async with get_db_session() as db:
            effects = (await db.scalars(select(ExternalEffect).where(ExternalEffect.session_id == ctx.session_id))).all()
            assert [(row.adapter, row.operation, row.state) for row in effects] == [("sandbox_preparation", "plan_review", "succeeded")]
            assert await db.scalar(select(QuestionCheckpoint.id).where(QuestionCheckpoint.part_id == ctx.part_id))
        assert len(sent) == 1 and sent[0].url.path == "/execute"
    finally:
        await runtime.finish_run(ticket)
        runtime.current_run.reset(token)
