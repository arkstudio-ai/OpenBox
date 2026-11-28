"""The kernel's extension registry: empty by default, built and sealed at startup."""
import copy
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from agent import extensions as ext
from tool.tool import ToolContext, ToolResult, define_tool
from tests.unit.test_run_fencing_api import loop_harness, state, statements  # noqa: F401
from tests.unit.trajectory_producer_support import recording_spool  # noqa: F401


def test_an_empty_registry_has_inert_ports():
    with ext.use(ext.KernelExtensions()) as registry:
        assert registry.recorder is ext.NULL_RECORDER
        assert registry.call_wrapper is ext.NULL_CALL_WRAPPER
        assert registry.timing is None
        assert registry.recorder.enabled("u") is False
        assert registry.recorder.current() is None


def test_plugins_register_once_and_the_installed_registry_is_sealed():
    class Plugin:
        name = "probe"

        def register(self, registry):
            registry.add_prompt_section(object())

    registry = ext.KernelExtensions().add(Plugin())
    with pytest.raises(ValueError):
        registry.add(Plugin())
    with ext.use(registry):
        assert ext.extensions() is registry
        with pytest.raises(RuntimeError):
            registry.add_tool_pack(object())
    assert ext.extensions() is not registry


async def test_recovery_hooks_run_in_order_and_one_failure_does_not_stop_the_next():
    calls = []

    class Hook:
        def __init__(self, name, result):
            self.name, self.result = name, result

        async def recover(self):
            calls.append(self.name)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    registry = ext.KernelExtensions()
    for hook in (Hook("a", 2), Hook("b", RuntimeError("down")), Hook("c", 1)):
        registry.add_recovery_hook(hook)
    with ext.use(registry):
        assert await ext.run_recovery_hooks() == 3
    assert calls == ["a", "b", "c"]


def test_plugin_state_writes_never_reach_the_context_a_copy_came_from():
    parent = ToolContext(session_id="s")
    ext.set_plugin_state(parent, "probe.value", 1)
    child = copy.copy(parent)
    ext.set_plugin_state(child, "probe.value", 2)
    assert ext.plugin_state(parent, "probe.value") == 1
    assert ext.plugin_state(child, "probe.value") == 2
    assert ext.plugin_state(ToolContext(), "probe.value", "absent") == "absent"


async def test_a_kernel_without_plugins_runs_a_turn_records_nothing_and_adds_no_sql(
        state, recording_spool, loop_harness, statements, monkeypatch):
    import litellm
    from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices
    from agent import llm
    from session.session import create_user_message, update_session

    class EchoArgs(BaseModel):
        text: str = ""

    async def echo(args, ctx):
        await ctx.update_output("partial")
        return ToolResult(title="echo", output=args.text)

    loop_harness.tools["echo"] = define_tool("echo", description="Echo text.", parameters=EchoArgs,
                                             execute=echo, sandbox_required=False)
    calls = {"n": 0}

    async def completion(**kwargs):
        calls["n"] += 1
        tool_step = calls["n"] % 2 == 1
        delta = (Delta(tool_calls=[{"index": 0, "id": f"call_{calls['n']}", "type": "function",
                                    "function": {"name": "echo", "arguments": '{"text":"hi"}'}}])
                 if tool_step else Delta(content="done"))

        async def chunks():
            yield ModelResponseStream(choices=[StreamingChoices(index=0, finish_reason=None, delta=delta)])
            yield ModelResponseStream(choices=[StreamingChoices(
                index=0, finish_reason="tool_calls" if tool_step else "stop", delta=Delta())])
        return chunks()

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _model: {})
    monkeypatch.setattr("agent.suggestions.generate_suggestions", AsyncMock(return_value=[]))
    await update_session("s1", user_id="u1", title="bench", model="openai/gpt-4o")

    async def turn(number):
        await create_user_message("s1", f"turn {number}", model="openai/gpt-4o", user_id="u1")
        before = len(statements)
        assert await loop_harness.loop.run_loop("s1", user_id="u1") is not None
        return len(statements) - before

    await turn(1)
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    with_plugin_recording_off = await turn(2)
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    recorded = len(recording_spool.lines())
    with ext.use(ext.KernelExtensions()):
        without_plugins = await turn(3)
    assert recording_spool.lines()[recorded:] == []
    assert without_plugins <= with_plugin_recording_off
