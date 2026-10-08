"""Shared assistant test helpers (V2: no provenance re-validation on read).

Moved from removed V1 provenance tests. Test-module imports are lazy so this
module never depends on another test module that is being rewritten.
"""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from agent import inbox, loop, processor
from agent.driver import reserve_run
from agent.loop import _to_llm_messages
from assistant.commands import accept_task_command, command_digest
from assistant.projection import project_main_messages
from assistant.service import ensure_main_session
from db.base import get_db_session, get_engine
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantTask, TaskResult
from db.models.memory_v2 import MemorySource
from db.models.session import Session
from models.message import TextPart, ToolPartData, ToolStatus
from session.agent_event_log import checkpoint_model_request, load_canonical_model_surface
from session.session import create_assistant_message, save_part, update_message_info
from tool.tool import ToolContext


async def add_task(ctx, key="new-task"):
    return await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
        main_id=ctx.session_id, project_id=ctx.project_id, idempotency_key=key,
        prompt="A new pure text task", title="New task")


@pytest.fixture(autouse=True)
def no_task_dispatch(monkeypatch):
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)


def arguments(ctx, answer):
    return {"project_id": ctx.project_id, "title": "Derived task", "instructions": "Only produce a text response",
            "source_message_ids": [answer.parent_id]}


async def settle_execution(ctx, receipt):
    execution = await reserve_run(receipt["execution_session_id"], ctx.user_id)
    try:
        fence = (execution.session_id, execution.run_id, execution.generation)
        batch = await inbox.claim_inbox_boundary(execution, step=1, include_next_turn=True)
        message = await create_assistant_message(execution.session_id, batch.messages[0].id,
            model_id="test/model", agent="build", user_id=ctx.user_id, run_fence=fence)
        await save_part(TextPart(session_id=execution.session_id, message_id=message.id,
            text="Result derived from the accepted instructions"), user_id=ctx.user_id, is_new=True, run_fence=fence)
        message.finish = "stop"
        await update_message_info(message, user_id=ctx.user_id, run_fence=fence)
        await inbox.settle_claimed_inbox_items(execution, result_message_id=message.id, outcome="succeeded")
    finally:
        await execution.release(session_status="idle")
    async with get_db_session() as db:
        return await db.scalar(select(TaskResult).where(TaskResult.task_id == receipt["task_id"]))


async def finish(ctx, lease, message, text):
    part = TextPart(session_id=ctx.session_id, message_id=message.id, text=text)
    await save_part(part, is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
    message.finish = "stop"
    await update_message_info(message, user_id=ctx.user_id, run_fence=ctx.run_fence)
    await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
    await lease.release(session_status="idle")
    return part


async def next_turn(ctx, prompt="Restate the previous answer without reading another tool."):
    await inbox.accept_inbox_item(session_id=ctx.session_id, user_id=ctx.user_id, delivery="followup",
        prompt=prompt, origin="human", origin_ref={"actor_user_id": ctx.user_id})
    lease = await reserve_run(ctx.session_id, ctx.user_id)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    fence = (ctx.session_id, lease.run_id, lease.generation)
    message = await create_assistant_message(ctx.session_id, batch.messages[0].id, model_id="test/model",
        agent="assistant", user_id=ctx.user_id, run_fence=fence)
    return replace(ctx, message_id=message.id, part_id="", run_id=lease.run_id, run_generation=lease.generation), lease, message


async def start(monkeypatch, *, title="PRIVATE_KNOWLEDGE_TITLE", project=False):
    from tests.unit.test_assistant_knowledge import page_for, seed
    identity, _, projects, _ = await seed(monkeypatch)
    async with get_db_session() as db:
        main = await db.get(Session, identity["main_id"])
    ctx = ToolContext(user_id=main.user_id, workspace_id=main.workspace_id, session_id=main.id,
                      project_id=main.project_id, agent_id="assistant")
    ctx, lease, message = await next_turn(ctx, "Consult my knowledge titles for a text-only task. Do not publish.")
    page = (await page_for(identity, projects[0] if project else None, title))[0] if title else None
    return ctx, lease, message, identity, projects, page


async def revoke(page):
    async with get_db_session() as db:
        source = await db.get(MemorySource, page.source_manifest[0]["id"])
        source.status = "REVOKED"


async def events(ctx, kind):
    async with get_db_session() as db:
        return list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
            AgentEvent.kind == kind).order_by(AgentEvent.sequence))).all())


async def projected_request(ctx):
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    projected = await project_main_messages(list(surface.messages), ctx=ctx)
    messages = _to_llm_messages(projected, user_id=ctx.user_id, assistant_projection_verified=True)
    return surface, messages


async def checkpoint(ctx, surface):
    return await checkpoint_model_request(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence,
        request_id=f"knowledge:{ctx.message_id}:{surface.event_sequence}", model_id="test/model",
        provider_binding_digest="a" * 64, tool_schema_digest="b" * 64, prompt_shape_digest="c" * 64,
        expected_event_sequence=surface.event_sequence, expected_event_digest=surface.event_digest,
        message_id=ctx.message_id, assistant_context=ctx._assistant_context)


# --- Report and continuation turns (V2: a non-empty answer settles a report) ---

async def prepare_report():
    """A delivered result claimed by a report-only main turn with a running results.read call."""
    from assistant.results import deliver_task_result
    from tests.unit.test_assistant_results import result_ready
    owner, workspace, main, accepted, execution_lease, _ = await result_ready()
    await execution_lease.release(session_status="idle")
    async with get_db_session() as db:
        result_id = await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted["task_id"]))
    delivered = await deliver_task_result(result_id)
    lease = await reserve_run(main.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    fence = (main.id, lease.run_id, lease.generation)
    message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
        agent="assistant", user_id=owner, run_fence=fence)
    part = ToolPartData(tool="results.read", canonical_tool_id="results.read", call_id="report-read",
        wire_tool_name="results_read", provider_binding_digest="b" * 64, provider_dialect="openai",
        stream_seq=0, status=ToolStatus.RUNNING, input={"result_id": result_id},
        session_id=main.id, message_id=message.id)
    await save_part(part, is_new=True, user_id=owner, run_fence=fence)
    ctx = ToolContext(user_id=owner, workspace_id=workspace, session_id=main.id, project_id=main.project_id,
        agent_id="assistant", run_id=lease.run_id, run_generation=lease.generation, message_id=message.id,
        part_id=part.id)
    return ctx, lease, message, part, result_id, delivered


async def report_answer(ctx, lease, message, read_part, *, finish="stop"):
    fence = (ctx.session_id, lease.run_id, lease.generation)
    read_part.status = ToolStatus.COMPLETED
    read_part.output = "See the authorized read above."
    await save_part(read_part, user_id=ctx.user_id, run_fence=fence)
    await save_part(TextPart(session_id=ctx.session_id, message_id=message.id,
        text="The report was created. Browser verification remains untested."),
        is_new=True, user_id=ctx.user_id, run_fence=fence)
    message.finish = finish
    await update_message_info(message, user_id=ctx.user_id, run_fence=fence)


async def seen_read(ctx, part, **kwargs):
    """Deliver one results.read page to the model (no coverage receipt in V2)."""
    from assistant.reporting import read_report_sources
    from tests.unit.assistant_source_fixtures import consume_context
    page = await read_report_sources(ctx=ctx, **kwargs)
    part.status, part.output = ToolStatus.COMPLETED, json.dumps(page)
    await save_part(part, user_id=ctx.user_id, run_fence=ctx.run_fence)
    await consume_context(ctx, messages=[{"role": "tool", "tool_call_id": part.call_id, "content": part.output}])
    return page


CONTINUATION_QUOTE = "Keep working on this same text task until complete, with at most one followup."


async def ready(monkeypatch):
    """A processed report of a task created with a one-followup continuation grant."""
    from tests.unit.test_assistant_commands import setup_task
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lambda *args: None)
    values = await setup_task()
    kwargs = {**values[-1], "prompt": CONTINUATION_QUOTE,
              "continuation": {"authorization_quote": CONTINUATION_QUOTE, "max_followups": 1,
                  "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}}

    async def bounded_task():
        return (*values[:-1], kwargs)

    monkeypatch.setattr("tests.unit.test_assistant_results.setup_task", bounded_task)
    ctx, lease, message, part, result_id, _ = await prepare_report()
    try:
        await seen_read(ctx, part, result_id=result_id)
        await report_answer(ctx, lease, message, part)
    finally:
        await lease.release(session_status="idle")
    async with get_db_session() as db:
        task = await db.scalar(select(AssistantTask).where(AssistantTask.user_id == ctx.user_id))
    return values, task, result_id


async def call_part(ctx, operation, request):
    part = ToolPartData(tool=operation, canonical_tool_id=operation, call_id="call-" + command_digest(request)[:12],
        wire_tool_name=operation.replace(".", "_"), provider_binding_digest="b" * 64, provider_dialect="openai",
        stream_seq=0, status=ToolStatus.RUNNING, input=request, session_id=ctx.session_id, message_id=ctx.message_id)
    await save_part(part, is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
    ctx.part_id = part.id
    return part


async def coordinator(values, task, result_id, *, read=True):
    """Claim the continuation turn for ``task`` and optionally read its bound result."""
    from assistant.continuation import enqueue
    from assistant.reporting import read_result_sources
    from tests.unit.assistant_source_fixtures import consume_context
    owner, _, workspace, main, _ = values
    receipt = await enqueue(task.id)
    lease = await reserve_run(main.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    message = await create_assistant_message(main.id, batch.messages[0].id,
        model_id="test/model", agent="assistant", user_id=owner,
        run_fence=(main.id, lease.run_id, lease.generation))
    ctx = ToolContext(user_id=owner, workspace_id=workspace, session_id=main.id, project_id=main.project_id,
        agent_id="assistant", run_id=lease.run_id, run_generation=lease.generation, message_id=message.id)
    if read:
        args = {"result_id": result_id, "detail": "full", "offset": 0, "max_chars": 8000, "source_version": None}
        part = await call_part(ctx, "results.read", args)
        page = await read_result_sources(user_id=owner, workspace_id=workspace, main_id=main.id,
            result_id=result_id, ctx=ctx)
        part.status, part.output = ToolStatus.COMPLETED, json.dumps(page)
        await save_part(part, user_id=owner, run_fence=ctx.run_fence)
        await consume_context(ctx, messages=[{"role": "tool", "tool_call_id": part.call_id, "content": part.output}])
    else:
        await consume_context(ctx)
    return ctx, lease, receipt


# --- Real loop runtime ---

async def _runtime(monkeypatch):
    from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
    from tests.unit.test_assistant_foundation import accounts
    from tool.assistant_tools import assistant_tools
    config = _loop_config()
    config.compaction.auto = False
    config.permission = {"*": "allow"}
    real_history = loop._resolve_history_tool_names
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "_resolve_history_tool_names", real_history)

    async def tools(*_args, **_kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools},
                               catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    return SimpleNamespace(config=config, owner=owner, workspace=workspace, main=main)


async def _accept(state, text="Answer briefly using the verified history."):
    from assistant.inputs import accept_turn
    return await accept_turn(user_id=state.owner, workspace_id=state.workspace,
        main_id=state.main.id, client_id=f"single-{time.monotonic_ns()}", text=text)


async def _run(state):
    lease = await reserve_run(state.main.id, state.owner)
    try:
        return await asyncio.wait_for(loop.run_loop(state.main.id, state.owner, lease=lease), timeout=45)
    finally:
        await lease.release(session_status="idle")


async def _events(state, kind):
    async with get_db_session() as db:
        return list((await db.scalars(select(AgentEvent).where(
            AgentEvent.session_id == state.main.id, AgentEvent.kind == kind,
        ).order_by(AgentEvent.sequence))).all())


def stable(value):
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items()
                if k not in {"source_checked_at", "display_token", "event_cursor"}}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


@contextmanager
def sql_reads():
    statements = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        assert statement.lstrip().upper().startswith("SELECT")
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", count)


@pytest.fixture
async def repeated_result():
    """Four chained task results; each task is submitted from a later human turn."""
    from tests.unit.assistant_source_fixtures import consume_context
    from tests.unit.test_assistant_reads import call_tool, read_turn
    ctx, lease, answer, accepted, report = await read_turn()
    try:
        for index in range(4):
            output, _, _ = await call_tool(ctx, "history.read", {
                "session_id": accepted["execution_session_id"], "message_ids": [report.id]})
            assert not output.metadata.get("error"), output.output
            await consume_context(ctx)
            output, _, _ = await call_tool(ctx, "tasks.submit", arguments(ctx, answer))
            assert not output.metadata.get("error"), output.output
            accepted = json.loads(output.output)
            await finish(ctx, lease, answer, f"Accepted derived text task {index}.")
            result = await settle_execution(ctx, accepted)
            report = SimpleNamespace(id=result.result_message_id)
            if index < 3:
                ctx, lease, answer = await next_turn(ctx, "Read that result and create another text-only task.")
        return dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id), result.id
    finally:
        await lease.release(session_status="idle")
