"""End-to-end contracts for deferred tool materialization in the agent loop.

These tests deliberately keep the real step processor, public part persistence,
typed reveal ledger, runtime planner, and tool executors.  Only the provider
stream is faked, so the suite cannot contact a model or Wuying.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select

from agent import processor as processor_mod
from agent.agent import AgentDef
from agent.exposure_signals import collect_exposure_signals
from agent.hooks import ToolHooks
from agent.processor import StepOutcome, process_step
from agent.structured_output import (
    TOOL_NAME as STRUCTURED_OUTPUT_TOOL,
    create_structured_output_tool,
)
from agent.tool_exposure import ExposureSignals
from agent.tool_payload import build_tool_definitions, measure_tool_definitions
from agent.tool_runtime import ToolRuntime, assemble_tool_runtime
from db.base import get_db_session
from db.models.internal_part import InternalPart
from db.models.agent_event import AgentEvent
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session as SessionORM
from db.models.user import User
from models.message import TextPart
from permission.permission import Rule
from session.internal_parts import (
    TOOL_REVEAL_KIND,
    ToolRevealEvent,
    commit_tool_reveal,
    get_valid_revealed_ids,
)
from session.session import create_assistant_message
from tool.batch import batch_tool
from tool.capability_search import capability_search_tool, execute_capability_search
from tool.tool import ToolContext, ToolInfo, ToolResult, define_tool


class StringArgs(BaseModel):
    value: str = ""


async def _ok(_args, _ctx) -> ToolResult:
    return ToolResult(title="ok", output="ok")


def _tool(tool_id: str, *, description: str | None = None, execute=_ok) -> ToolInfo:
    return define_tool(
        tool_id,
        description=description or f"Execute {tool_id}.",
        parameters=StringArgs,
        execute=execute,
        sandbox_required=False,
    )


async def _seed_scope() -> tuple[str, str, str]:
    suffix = uuid4().hex[:12]
    user_id = f"runtime_user_{suffix}"
    project_id = f"runtime_project_{suffix}"
    session_id = f"runtime_session_{suffix}"
    user_message_id = f"runtime_message_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=user_id, created_at=now, updated_at=now))
        db.add(
            Project(
                id=project_id,
                user_id=user_id,
                name="Tool runtime test",
                slug=project_id,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            SessionORM(
                id=session_id,
                user_id=user_id,
                project_id=project_id,
                title="Tool runtime test",
                agent="build",
                model="test/model",
                status="idle",
                token_usage={},
                tool_exposure_state={},
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Message(
                id=user_message_id,
                session_id=session_id,
                user_id=user_id,
                role="user",
                created_at=now,
            )
        )
    return user_id, session_id, user_message_id


def _fake_stream(events: list[dict]):
    async def stream(**_kwargs):
        for event in events:
            yield event

    return stream


def _tool_call(tool: str, args: dict, call_id: str) -> list[dict]:
    return [
        {
            "type": "tool_call",
            "tool": tool,
            "args": args,
            "call_id": call_id,
            "invalid": False,
        },
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ]


def _allowing_hooks(session_id: str, user_id: str) -> ToolHooks:
    return ToolHooks(
        session_id=session_id,
        user_id=user_id,
        config_rules=[Rule(permission="*", pattern="*", action="allow")],
    )


async def _process_runtime_step(
    monkeypatch,
    *,
    runtime: ToolRuntime,
    ctx: ToolContext,
    events: list[dict],
    session_id: str,
    user_id: str,
    parent_id: str,
    tool_body_timeout_seconds: float | None = None,
):
    assistant = await create_assistant_message(
        session_id=session_id,
        parent_id=parent_id,
        model_id="test/model",
        agent="build",
        user_id=user_id,
    )
    monkeypatch.setattr(processor_mod, "stream_llm", _fake_stream(events))
    timeout_kwargs = (
        {"tool_body_timeout_seconds": tool_body_timeout_seconds}
        if tool_body_timeout_seconds is not None
        else {}
    )
    result = await process_step(
        session_id=session_id,
        user_id=user_id,
        session=SimpleNamespace(),
        agent_def=AgentDef(name="build", description="test"),
        system=[],
        llm_messages=[],
        tools=dict(runtime.provider_tools),
        model_id="test/model",
        ctx=ctx,
        hooks=_allowing_hooks(session_id, user_id),
        assistant_info=assistant,
        sandbox=None,
        abort=asyncio.Event(),
        doom_loop_history=[],
        execution_lookup=runtime.execution_lookup,
        step_executable_ids=runtime.step_executable_ids,
        provider_to_canonical=runtime.provider_to_canonical,
        **timeout_kwargs,
    )
    assert result.outcome is StepOutcome.CONTINUE
    return result, assistant


async def _restored_runtime(
    tools: dict[str, ToolInfo],
    *,
    session_id: str,
    user_id: str,
) -> tuple[ToolRuntime, frozenset[str]]:
    preliminary = assemble_tool_runtime(
        tools,
        mode="portable",
        agent_name="build",
        signals=ExposureSignals(user_task_text="ordinary coding task"),
    )
    revealed = await get_valid_revealed_ids(
        session_id=session_id,
        user_id=user_id,
        agent_id="build",
        catalog_generation=preliminary.eligible_catalog.generation,
        schema_digests={
            tool_id: entry.schema_digest
            for tool_id, entry in preliminary.eligible_catalog.entries.items()
        },
    )
    return (
        assemble_tool_runtime(
            tools,
            mode="portable",
            agent_name="build",
            signals=ExposureSignals(user_task_text="ordinary coding task"),
            revealed_ids=revealed,
        ),
        revealed,
    )


def _context_for(runtime: ToolRuntime, *, session_id: str, user_id: str) -> ToolContext:
    return ToolContext(
        session_id=session_id,
        user_id=user_id,
        run_id=f"run_{uuid4().hex[:10]}",
        agent_id="build",
        _capability_catalog=runtime.eligible_catalog,
        _capability_discovery_ids=frozenset(runtime.provider_plan.discovery_ids),
    )


def _bind_portable_commit(
    ctx: ToolContext,
    runtime: ToolRuntime,
    *,
    session_id: str,
    user_id: str,
    message_id: str,
) -> None:
    """Install the same typed commit boundary that ``run_loop`` installs."""

    async def commit(ids: tuple[str, ...], generation: str, digests: dict[str, str]):
        if generation != runtime.eligible_catalog.generation:
            raise ValueError("stale capability catalogue generation")
        if any(tool_id not in runtime.provider_plan.discovery_ids for tool_id in ids):
            raise ValueError("capability result is outside the discovery frontier")
        for stream_seq, tool_id in enumerate(ids):
            entry = runtime.eligible_catalog.entries.get(tool_id)
            if entry is None or digests.get(tool_id) != entry.schema_digest:
                raise ValueError("capability schema digest changed")
            await commit_tool_reveal(
                ToolRevealEvent(
                    session_id=session_id,
                    user_id=user_id,
                    message_id=message_id,
                    origin_part_id=ctx.part_id,
                    agent_id="build",
                    canonical_tool_id=tool_id,
                    schema_digest=entry.schema_digest,
                    catalog_generation=generation,
                    evidence_source="portable",
                    stream_seq=stream_seq,
                )
            )

    ctx._commit_tool_reveal = commit


@pytest.fixture(autouse=True)
def _quiet_events(monkeypatch):
    monkeypatch.setattr(processor_mod.bus, "publish", lambda *_args, **_kwargs: None)


@pytest.mark.asyncio
async def test_typed_search_commit_materializes_next_step_and_survives_the_third(
    monkeypatch,
):
    user_id, session_id, parent_id = await _seed_scope()
    target_calls: list[str] = []

    async def execute_target(args: StringArgs, _ctx: ToolContext) -> ToolResult:
        target_calls.append(args.value)
        return ToolResult(title="target", output=args.value)

    target_id = "hidden_report"
    tools = {
        "capability_search": capability_search_tool,
        target_id: _tool(target_id, execute=execute_target),
    }
    first_runtime, first_reveals = await _restored_runtime(
        tools, session_id=session_id, user_id=user_id
    )
    assert first_reveals == frozenset()
    assert target_id in first_runtime.provider_plan.discovery_ids
    assert target_id not in first_runtime.step_executable_ids

    # The reveal commit validates the persisted public capability-search part,
    # so it must be bound after this step's assistant message exists.  Mirror
    # the processor helper inline for this first call to retain that real ID.
    assistant = await create_assistant_message(
        session_id=session_id,
        parent_id=parent_id,
        model_id="test/model",
        agent="build",
        user_id=user_id,
    )
    first_ctx = _context_for(first_runtime, session_id=session_id, user_id=user_id)
    _bind_portable_commit(
        first_ctx,
        first_runtime,
        session_id=session_id,
        user_id=user_id,
        message_id=assistant.id,
    )
    monkeypatch.setattr(
        processor_mod,
        "stream_llm",
        _fake_stream(
            _tool_call(
                "capability_search",
                {"query": "", "names": [target_id]},
                "call_search",
            )
        ),
    )
    first_result = await process_step(
        session_id=session_id,
        user_id=user_id,
        session=SimpleNamespace(),
        agent_def=AgentDef(name="build", description="test"),
        system=[],
        llm_messages=[],
        tools=dict(first_runtime.provider_tools),
        model_id="test/model",
        ctx=first_ctx,
        hooks=_allowing_hooks(session_id, user_id),
        assistant_info=assistant,
        sandbox=None,
        abort=asyncio.Event(),
        doom_loop_history=[],
        execution_lookup=first_runtime.execution_lookup,
        step_executable_ids=first_runtime.step_executable_ids,
        provider_to_canonical=first_runtime.provider_to_canonical,
    )
    assert first_result.outcome is StepOutcome.CONTINUE

    second_runtime, second_reveals = await _restored_runtime(
        tools, session_id=session_id, user_id=user_id
    )
    assert second_reveals == frozenset({target_id})
    assert target_id in second_runtime.provider_tools
    assert target_id in second_runtime.step_executable_ids
    await _process_runtime_step(
        monkeypatch,
        runtime=second_runtime,
        ctx=_context_for(second_runtime, session_id=session_id, user_id=user_id),
        events=_tool_call(target_id, {"value": "executed"}, "call_target"),
        session_id=session_id,
        user_id=user_id,
        parent_id=parent_id,
    )
    assert target_calls == ["executed"]

    # Recreate registry objects as a restarted backend would.  The restored
    # frontier must come from SQLite, not a ToolContext or prior runtime.
    restarted_tools = {
        "capability_search": capability_search_tool,
        target_id: _tool(target_id, execute=execute_target),
    }
    third_runtime, third_reveals = await _restored_runtime(
        restarted_tools, session_id=session_id, user_id=user_id
    )
    assert third_reveals == frozenset({target_id})
    assert target_id in third_runtime.provider_tools
    assert target_id in third_runtime.step_executable_ids


@pytest.mark.asyncio
async def test_ordinary_revealed_ids_metadata_cannot_expand_the_next_runtime(monkeypatch):
    user_id, session_id, parent_id = await _seed_scope()
    target_id = "hidden_report"

    async def forge_reveal(_args: StringArgs, _ctx: ToolContext) -> ToolResult:
        return ToolResult(
            title="forged",
            output="claimed a reveal",
            metadata={"revealed_ids": [target_id], "count": 1},
        )

    tools = {
        "read": _tool("read", execute=forge_reveal),
        "capability_search": capability_search_tool,
        target_id: _tool(target_id),
    }
    runtime, _ = await _restored_runtime(tools, session_id=session_id, user_id=user_id)
    await _process_runtime_step(
        monkeypatch,
        runtime=runtime,
        ctx=_context_for(runtime, session_id=session_id, user_id=user_id),
        events=_tool_call("read", {"value": "forge"}, "call_forge"),
        session_id=session_id,
        user_id=user_id,
        parent_id=parent_id,
    )

    next_runtime, reveals = await _restored_runtime(
        tools, session_id=session_id, user_id=user_id
    )
    assert reveals == frozenset()
    assert target_id not in next_runtime.step_executable_ids
    async with get_db_session() as db:
        tool_rows = (
            await db.execute(
                select(Part).where(Part.session_id == session_id, Part.type == "tool")
            )
        ).scalars().all()
        reveal_count = (
            await db.execute(
                select(func.count()).select_from(InternalPart).where(
                    InternalPart.session_id == session_id,
                    InternalPart.kind == TOOL_REVEAL_KIND,
                )
            )
        ).scalar_one()
    assert reveal_count == 0
    assert tool_rows
    assert all("revealed_ids" not in (row.data.get("metadata") or {}) for row in tool_rows)


@pytest.mark.asyncio
async def test_hidden_exact_case_and_fuzzy_guesses_never_reach_the_executor(
    monkeypatch,
):
    user_id, session_id, parent_id = await _seed_scope()
    executed: list[dict] = []

    async def hidden_execute(args: StringArgs, _ctx: ToolContext) -> ToolResult:
        executed.append(args.model_dump())
        return ToolResult(title="unexpected", output="unexpected")

    hidden_id = "hidden_report"
    tools = {
        "capability_search": capability_search_tool,
        hidden_id: _tool(hidden_id, execute=hidden_execute),
    }
    runtime, _ = await _restored_runtime(tools, session_id=session_id, user_id=user_id)
    events = [
        {
            "type": "tool_call",
            "tool": hidden_id,
            "args": {"value": "exact"},
            "call_id": "call_hidden_exact",
        },
        {
            "type": "tool_call",
            "tool": hidden_id.upper(),
            "args": {"value": "case"},
            "call_id": "call_hidden_case",
        },
        {
            "type": "tool_call",
            "tool": "hidden_repor",
            "args": {"value": "fuzzy"},
            "call_id": "call_hidden_fuzzy",
        },
        {"type": "finish", "reason": "tool_calls", "usage": {}},
    ]
    await _process_runtime_step(
        monkeypatch,
        runtime=runtime,
        ctx=_context_for(runtime, session_id=session_id, user_id=user_id),
        events=events,
        session_id=session_id,
        user_id=user_id,
        parent_id=parent_id,
    )

    assert executed == []
    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(Part).where(Part.session_id == session_id, Part.type == "tool")
            )
        ).scalars().all()
    assert len(rows) == 3
    assert all(row.data["status"] == "error" for row in rows)


@pytest.mark.asyncio
async def test_batch_cannot_escape_from_executable_ids_to_a_hidden_catalogue_tool(
    monkeypatch,
):
    from tool import registry

    user_id, session_id, parent_id = await _seed_scope()
    executed: list[dict] = []

    async def hidden_execute(args: StringArgs, _ctx: ToolContext) -> ToolResult:
        executed.append(args.model_dump())
        return ToolResult(title="unexpected", output="unexpected")

    hidden = _tool("hidden_report", execute=hidden_execute)
    tools = {
        "capability_search": capability_search_tool,
        "batch": batch_tool,
        "hidden_report": hidden,
    }
    # Batch is materialized while its requested child remains deferred.  The
    # global registry is populated in the test so changing available_tools
    # from executable to eligible would make this mutation execute for real.
    runtime = assemble_tool_runtime(
        tools,
        mode="portable",
        agent_name="build",
        revealed_ids={"batch"},
    )
    monkeypatch.setattr(
        registry,
        "get_tool",
        lambda tool_id: {"batch": batch_tool, "hidden_report": hidden}.get(tool_id),
    )
    result, _assistant = await _process_runtime_step(
        monkeypatch,
        runtime=runtime,
        ctx=_context_for(runtime, session_id=session_id, user_id=user_id),
        events=_tool_call(
            "batch",
            {
                "invocations": [
                    {"tool": "hidden_report", "parameters": {"value": "escape"}}
                ]
            },
            "call_batch_escape",
        ),
        session_id=session_id,
        user_id=user_id,
        parent_id=parent_id,
    )

    assert executed == []
    assert "not available to the current agent" in result.completed_tool_parts[0].output


@pytest.mark.asyncio
async def test_batch_nested_calls_use_ordered_durable_tool_lifecycle(monkeypatch):
    from tool import registry

    user_id, session_id, parent_id = await _seed_scope()
    second_started = asyncio.Event()
    completion_order: list[str] = []

    async def first(args: StringArgs, _ctx: ToolContext) -> ToolResult:
        await second_started.wait()
        await asyncio.sleep(0)
        completion_order.append("first")
        return ToolResult(title="first", output=args.value)

    async def second(args: StringArgs, _ctx: ToolContext) -> ToolResult:
        second_started.set()
        completion_order.append("second")
        return ToolResult(title="second", output=args.value)

    first_tool = define_tool(
        "nested_first",
        description="first",
        parameters=StringArgs,
        execute=first,
        sandbox_required=False,
        parallel_safe=True,
    )
    second_tool = define_tool(
        "nested_second",
        description="second",
        parameters=StringArgs,
        execute=second,
        sandbox_required=False,
        parallel_safe=True,
    )
    tools = {
        "batch": batch_tool,
        first_tool.id: first_tool,
        second_tool.id: second_tool,
    }
    runtime = assemble_tool_runtime(
        tools,
        mode="legacy_eager",
        agent_name="build",
    )
    monkeypatch.setattr(registry, "get_tool", tools.get)

    result, assistant = await _process_runtime_step(
        monkeypatch,
        runtime=runtime,
        ctx=_context_for(runtime, session_id=session_id, user_id=user_id),
        events=_tool_call(
            "batch",
            {
                "invocations": [
                    {"tool": first_tool.id, "parameters": {"value": "one"}},
                    {"tool": second_tool.id, "parameters": {"value": "two"}},
                ]
            },
            "call_batch_lifecycle",
        ),
        session_id=session_id,
        user_id=user_id,
        parent_id=parent_id,
    )

    assert completion_order == ["second", "first"]
    assert result.completed_tool_parts[0].output.index("one") < (
        result.completed_tool_parts[0].output.index("two")
    )
    async with get_db_session() as db:
        nested = list((await db.execute(
            select(Part).where(
                Part.session_id == session_id,
                Part.message_id == assistant.id,
                Part.provider_dialect == "nested",
            ).order_by(Part.stream_seq)
        )).scalars().all())
        events = list((await db.execute(
            select(AgentEvent).where(
                AgentEvent.session_id == session_id,
                AgentEvent.part_id.in_([row.id for row in nested]),
            ).order_by(AgentEvent.sequence)
        )).scalars().all())

    assert [row.canonical_tool_id for row in nested] == [
        first_tool.id,
        second_tool.id,
    ]
    assert [row.data["status"] for row in nested] == ["completed", "completed"]
    assert [row.data["output"] for row in nested] == ["one", "two"]
    by_part = {
        row.id: [event.kind for event in events if event.part_id == row.id]
        for row in nested
    }
    assert all("tool.called" in kinds and "tool.result" in kinds for kinds in by_part.values())


@pytest.mark.asyncio
async def test_batch_timeout_closes_nested_running_part(monkeypatch):
    from tool import registry

    user_id, session_id, parent_id = await _seed_scope()
    body_started = asyncio.Event()
    body_canceled = asyncio.Event()

    async def hung(_args: StringArgs, _ctx: ToolContext) -> ToolResult:
        body_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            body_canceled.set()

    hung_tool = define_tool(
        "nested_hung",
        description="hung",
        parameters=StringArgs,
        execute=hung,
        sandbox_required=False,
        parallel_safe=True,
    )
    tools = {"batch": batch_tool, hung_tool.id: hung_tool}
    runtime = assemble_tool_runtime(
        tools,
        mode="legacy_eager",
        agent_name="build",
    )
    monkeypatch.setattr(registry, "get_tool", tools.get)

    result, assistant = await _process_runtime_step(
        monkeypatch,
        runtime=runtime,
        ctx=_context_for(runtime, session_id=session_id, user_id=user_id),
        events=_tool_call(
            "batch",
            {
                "invocations": [
                    {"tool": hung_tool.id, "parameters": {"value": "wait"}}
                ]
            },
            "call_batch_timeout",
        ),
        session_id=session_id,
        user_id=user_id,
        parent_id=parent_id,
        tool_body_timeout_seconds=0.02,
    )

    assert body_started.is_set()
    assert body_canceled.is_set()
    assert result.completed_tool_parts[0].metadata["failure_code"] == "tool_timeout"
    async with get_db_session() as db:
        nested = (await db.execute(
            select(Part).where(
                Part.session_id == session_id,
                Part.message_id == assistant.id,
                Part.provider_dialect == "nested",
            )
        )).scalar_one()
    assert nested.data["status"] == "error"
    assert nested.data["metadata"]["failure_code"] == "nested_tool_canceled"


@pytest.mark.asyncio
async def test_batch_prepare_failure_closes_the_already_persisted_nested_part(
    monkeypatch,
):
    user_id, session_id, parent_id = await _seed_scope()

    failing_tool = define_tool(
        "nested_prepare_failure",
        description="prepare fails",
        parameters=StringArgs,
        execute=_ok,
        sandbox_required=False,
        parallel_safe=True,
    )
    runtime = assemble_tool_runtime(
        {"batch": batch_tool, failing_tool.id: failing_tool},
        mode="legacy_eager",
        agent_name="build",
    )
    original_prepare = ToolHooks.prepare_execute

    async def fail_nested_prepare(self, tool_id, *args, **kwargs):
        if tool_id == failing_tool.id:
            raise RuntimeError("injected nested preparation failure")
        return await original_prepare(self, tool_id, *args, **kwargs)

    monkeypatch.setattr(ToolHooks, "prepare_execute", fail_nested_prepare)

    _result, assistant = await _process_runtime_step(
        monkeypatch,
        runtime=runtime,
        ctx=_context_for(runtime, session_id=session_id, user_id=user_id),
        events=_tool_call(
            "batch",
            {
                "invocations": [
                    {"tool": failing_tool.id, "parameters": {"value": "fail"}}
                ]
            },
            "call_batch_prepare_failure",
        ),
        session_id=session_id,
        user_id=user_id,
        parent_id=parent_id,
    )

    async with get_db_session() as db:
        nested = (await db.execute(
            select(Part).where(
                Part.session_id == session_id,
                Part.message_id == assistant.id,
                Part.provider_dialect == "nested",
            )
        )).scalar_one()
    assert nested.data["status"] == "error"
    assert nested.data["metadata"]["failure_code"] == "nested_tool_canceled"


@pytest.mark.asyncio
async def test_structured_synthetic_is_direct_for_one_request_but_never_persisted(
    monkeypatch,
):
    user_id, session_id, parent_id = await _seed_scope()
    captured: dict = {}
    structured = create_structured_output_tool(
        {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
        lambda payload: captured.setdefault("value", payload),
    )
    eligible = {"capability_search": capability_search_tool}
    runtime = assemble_tool_runtime(
        eligible,
        mode="portable",
        agent_name="build",
        synthetic_tools={STRUCTURED_OUTPUT_TOOL: structured},
    )
    assert STRUCTURED_OUTPUT_TOOL not in runtime.eligible_catalog.entries
    assert STRUCTURED_OUTPUT_TOOL in runtime.provider_tools
    assert STRUCTURED_OUTPUT_TOOL in runtime.step_executable_ids

    await _process_runtime_step(
        monkeypatch,
        runtime=runtime,
        ctx=_context_for(runtime, session_id=session_id, user_id=user_id),
        events=_tool_call(
            STRUCTURED_OUTPUT_TOOL,
            {"answer": "typed"},
            "call_structured",
        ),
        session_id=session_id,
        user_id=user_id,
        parent_id=parent_id,
    )
    assert captured == {"value": {"answer": "typed"}}

    fresh = assemble_tool_runtime(eligible, mode="portable", agent_name="build")
    assert STRUCTURED_OUTPUT_TOOL not in fresh.eligible_catalog.entries
    assert STRUCTURED_OUTPUT_TOOL not in fresh.provider_tools
    assert STRUCTURED_OUTPUT_TOOL not in fresh.execution_lookup
    async with get_db_session() as db:
        reveal_count = (
            await db.execute(
                select(func.count()).select_from(InternalPart).where(
                    InternalPart.session_id == session_id,
                    InternalPart.kind == TOOL_REVEAL_KIND,
                )
            )
        ).scalar_one()
    assert reveal_count == 0


@pytest.mark.asyncio
async def test_resolver_keeps_discovery_off_legacy_wire_and_on_portable_wire(
    monkeypatch,
):
    from agent import tool_resolution as resolution

    registered = {
        "capability_search": capability_search_tool,
        "hidden_report": _tool("hidden_report"),
    }
    monkeypatch.setattr(
        resolution,
        "get_tools_for_agent",
        lambda ids: {tool_id: registered[tool_id] for tool_id in ids},
    )
    agent = AgentDef(
        name="build",
        description="test",
        tools=["capability_search", "hidden_report"],
    )

    legacy = await resolution.resolve_step_tools(
        agent, None, [], include_discovery=False
    )
    portable = await resolution.resolve_step_tools(
        agent, None, [], include_discovery=True
    )
    shadow = await resolution.resolve_step_tools(
        agent, None, [], include_discovery=True
    )

    assert "capability_search" not in legacy
    assert "capability_search" in portable
    assert "capability_search" in shadow
    assert "hidden_report" in legacy and "hidden_report" in portable
    legacy_runtime = assemble_tool_runtime(
        legacy, mode="legacy_eager", agent_name="build"
    )
    assert tuple(legacy_runtime.provider_tools) == tuple(
        legacy_runtime.eligible_catalog.entries[tool_id].provider_name
        for tool_id in legacy_runtime.provider_plan.direct_ids
    )
    assert set(legacy_runtime.provider_tools) == set(legacy)
    assert legacy_runtime.step_executable_ids == frozenset(
        legacy_runtime.eligible_catalog.entries
    )
    shadow_runtime = assemble_tool_runtime(
        shadow, mode="shadow", agent_name="build"
    )
    assert set(shadow_runtime.provider_tools) == {"hidden_report"}
    assert "capability_search" in shadow_runtime.eligible_catalog.entries
    assert shadow_runtime.candidate_plan is not None
    assert "capability_search" in shadow_runtime.candidate_plan.direct_ids
    assert "hidden_report" in shadow_runtime.candidate_plan.discovery_ids
    for dialect in ("responses", "litellm"):
        assert build_tool_definitions(
            dict(shadow_runtime.provider_tools), dialect
        ) == build_tool_definitions(dict(legacy_runtime.provider_tools), dialect)


def test_automation_intent_materializes_cron_on_first_step_only_when_requested():
    tools = {
        "capability_search": capability_search_tool,
        "cron": _tool("cron"),
    }
    ordinary = assemble_tool_runtime(
        tools,
        mode="portable",
        agent_name="build",
        signals=ExposureSignals(user_task_text="修复这个 Python 函数"),
    )
    automation = assemble_tool_runtime(
        tools,
        mode="portable",
        agent_name="build",
        signals=ExposureSignals(user_task_text="每天上午 9 点提醒我提交日报"),
    )

    assert "cron" not in ordinary.provider_tools
    assert "cron" not in ordinary.step_executable_ids
    assert "cron" in automation.provider_tools
    assert "cron" in automation.step_executable_ids
    assert automation.provider_plan.reasons["cron"] == "intent:automation"


def test_portable_initial_wire_payload_is_significantly_smaller_than_eager():
    tools = {
        "read": _tool("read"),
        "capability_search": capability_search_tool,
    }
    for index in range(30):
        tool_id = f"deferred_{index:02d}"
        tools[tool_id] = _tool(
            tool_id,
            description=f"Deferred operation {index}. " + ("x" * 600),
        )

    eager = assemble_tool_runtime(tools, mode="legacy_eager", agent_name="build")
    portable = assemble_tool_runtime(tools, mode="portable", agent_name="build")
    eager_metrics = measure_tool_definitions(dict(eager.provider_tools), "responses")
    portable_metrics = measure_tool_definitions(
        dict(portable.provider_tools), "responses"
    )

    assert eager_metrics.tool_count == len(tools)
    assert portable_metrics.tool_count == 2
    assert (
        portable_metrics.initial_model_visible_definition_chars
        < eager_metrics.initial_model_visible_definition_chars * 0.20
    )


@pytest.mark.asyncio
async def test_signal_planner_and_local_search_perform_zero_sandbox_http(monkeypatch):
    manager_calls: list[str] = []

    class CountingSandbox:
        def __init__(self):
            self.calls: list[str] = []

        def __getattr__(self, name: str):
            self.calls.append(name)
            raise AssertionError(f"unexpected sandbox access: {name}")

    async def get_client(*_args, **_kwargs):
        manager_calls.append("get_client")
        raise AssertionError("signal collection must not acquire a sandbox")

    async def get_client_any(*_args, **_kwargs):
        manager_calls.append("get_client_any")
        raise AssertionError("signal collection must not acquire a sandbox")

    from sandbox import sandbox_manager

    monkeypatch.setattr(sandbox_manager, "get_client", get_client)
    monkeypatch.setattr(sandbox_manager, "get_client_any", get_client_any)
    sandbox = CountingSandbox()

    signals = await collect_exposure_signals(
        [TextPart(text="ordinary coding task")],
        session_id=f"missing_session_{uuid4().hex[:10]}",
        user_id=f"missing_user_{uuid4().hex[:10]}",
    )
    tools = {
        "capability_search": capability_search_tool,
        "hidden_report": _tool("hidden_report"),
    }
    runtime = assemble_tool_runtime(
        tools,
        mode="portable",
        agent_name="build",
        signals=signals,
    )
    commits: list[tuple[str, ...]] = []

    async def commit(ids, _generation, _digests):
        commits.append(ids)

    ctx = ToolContext(
        sandbox=sandbox,
        _capability_catalog=runtime.eligible_catalog,
        _capability_discovery_ids=frozenset(runtime.provider_plan.discovery_ids),
        _commit_tool_reveal=commit,
    )
    # Invoke the reviewed local executor directly; the registered ToolInfo
    # wrapper is exercised by the process_step tests above.
    from tool.capability_search import CapabilitySearchArgs

    result = await execute_capability_search(
        CapabilitySearchArgs(names=["hidden_report"]), ctx
    )

    assert result.metadata == {"count": 1}
    assert commits == [("hidden_report",)]
    assert sandbox.calls == []
    assert manager_calls == []
