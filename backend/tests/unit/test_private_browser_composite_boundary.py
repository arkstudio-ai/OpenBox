"""Registered finite-browser tools cannot enter either batch dispatcher.

The durable case retains real Inbox, Driver, ToolPart and canonical events.
The browser fixture has a live in-process finite HTTP supervisor, but neither
dispatcher may contact it or require a generic sandbox to reject the call.
"""
import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from agent.hooks import ToolHooks
from agent.nested_tool_runtime import NestedToolRuntime
from assistant.commands import accept_task_command
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.external_effect import ExternalEffect
from db.models.part import Part
from models.message import ToolPartData, ToolStatus
from session.session import create_assistant_message, save_part
from tests.offline_wuying import install_wuying_offline_guard
from tests.unit.test_assistant_browser_resources import browser_world  # noqa: F401
from tests.unit.test_private_runtime import assistant_database, private_world  # noqa: F401
from tool import registry
from tool.private_browser import private_browser_tool
from tool.tool import ToolContext


@pytest.fixture
async def registered(browser_world, monkeypatch):
    install_wuying_offline_guard(monkeypatch)
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    # Exercise the production registry loader; restore its prior mapping on
    # teardown rather than replacing the browser tool with a stand-in.
    monkeypatch.setattr(registry, "_tools", dict(registry._tools))
    registry.register_builtin_tools(load_custom=False)
    assert registry.get_tool("private_browser") is private_browser_tool
    assert private_browser_tool.parallel_safe is False
    assert private_browser_tool.sandbox_required is False
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lambda *_args: None)
    w = browser_world
    w.sandbox_acquires = []

    async def unexpected(*args, **kwargs):
        w.sandbox_acquires.append((args, kwargs))
        pytest.fail("A rejected browser composite attempted sandbox preparation")

    monkeypatch.setattr("sandbox.sandbox_manager.acquire", unexpected)
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", unexpected)
    return w


async def test_registered_private_browser_is_rejected_by_legacy_batch_without_sandbox(registered):
    w = registered
    assert w.requests == w.pipe.calls == []
    ctx = ToolContext(available_tools=frozenset({"batch", "private_browser"}), sandbox=None)
    result = await registry.get_tool("batch").execute({"invocations": [
        {"tool": "private_browser", "parameters": {"action": "capture"}},
    ]}, ctx)
    assert "[private_browser] Error: Tool is not safe for parallel execution." in result.output
    assert w.requests == w.pipe.calls == w.sandbox_acquires == []
    async with get_db_session() as db:
        assert await db.scalar(select(ExternalEffect.id).where(ExternalEffect.tenant_id == w.w.owner)) is None


async def test_registered_private_browser_is_rejected_by_durable_nested_batch_without_sandbox(registered):
    w = registered
    task = await accept_task_command(**w.scope, project_id=w.main.project_id,
        idempotency_key="composite-browser", prompt="Inspect this browser through the offered tools.")
    lease = await reserve_run(task["execution_session_id"], w.w.owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        await lease.set_phase("running")
        fence = (lease.session_id, lease.run_id, lease.generation)
        message = await create_assistant_message(lease.session_id, batch.messages[0].id, agent="build",
            model_id="test/model", user_id=lease.user_id, run_fence=fence)
        args = {"invocations": [{"tool": "private_browser", "parameters": {"action": "capture"}}]}
        parent = ToolPartData(session_id=lease.session_id, message_id=message.id, tool="batch",
            canonical_tool_id="batch", wire_tool_name="batch", call_id="browser-composite",
            status=ToolStatus.RUNNING, input=args, provider_binding_digest="a" * 64,
            provider_dialect="test", stream_seq=0)
        await save_part(parent, is_new=True, user_id=lease.user_id, run_fence=fence)
        lookup = {name: registry.get_tool(name) for name in ("batch", "private_browser")}
        ctx = ToolContext(session_id=lease.session_id, user_id=lease.user_id, workspace_id=w.w.workspace,
            project_id=w.main.project_id, message_id=message.id, part_id=parent.id, sandbox=None,
            run_id=lease.run_id, run_generation=lease.generation, _assert_current=lease.assert_current,
            available_tools=frozenset(lookup), _tool_execution_lookup=lookup)
        ctx._nested_tool_runtime = NestedToolRuntime(ToolHooks(lease.session_id, lease.user_id), ctx)
        assert w.requests == w.pipe.calls == []
        result = await registry.get_tool("batch").execute(args, ctx)
        assert result.metadata["error"] is True
        assert "private_browser' is not safe for parallel execution" in result.output
        async with get_db_session() as db:
            child, = list((await db.scalars(select(Part).where(Part.session_id == lease.session_id,
                Part.canonical_tool_id == "private_browser"))).all())
            events = list((await db.scalars(select(AgentEvent).where(AgentEvent.part_id == child.id)
                .order_by(AgentEvent.sequence))).all())
            assert await db.scalar(select(ExternalEffect.id).where(ExternalEffect.session_id == lease.session_id)) is None
        assert child.data["status"] == "error"
        assert child.data["metadata"]["failure_code"] == "nested_tool_not_parallel_safe"
        assert any(event.kind == "tool.called" for event in events)
        terminal, = [event for event in events if event.kind == "tool.result"]
        assert terminal.payload["part"]["data"]["status"] == "error"
        assert w.requests == w.pipe.calls == w.sandbox_acquires == []
    finally:
        await lease.release(session_status="idle")
