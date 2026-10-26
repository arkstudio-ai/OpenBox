from types import SimpleNamespace

import pytest

from assistant.review_repair import repair_mode, permits_tool, OUT_OF_SCOPE, UNAVAILABLE, REPAIR_ONLY


def failed(code=OUT_OF_SCOPE):
    return SimpleNamespace(tool="question", status="error", metadata={"failure_code": code})


def test_correction_is_bounded_and_does_not_grant_unrelated_tools():
    assert repair_mode([]) is None
    mode = repair_mode([failed()])
    assert mode == "correct" and permits_tool(mode, "question")
    assert not permits_tool(mode, "bash") and not permits_tool(mode, "batch")
    assert repair_mode([failed(), failed()]) == "correct"
    assert repair_mode([failed(), failed(), failed()]) == "reply"
    assert not permits_tool("reply", "question")


def test_unavailable_or_out_of_scope_execution_stops_correction():
    assert repair_mode([failed(UNAVAILABLE)]) == "reply"
    assert repair_mode([failed(), {"tool":"bash", "metadata":{"failure_code":REPAIR_ONLY}}]) == "reply"
    assert repair_mode([{"tool":"bash", "metadata":{"failure_code":OUT_OF_SCOPE}}]) is None


def test_success_and_new_run_do_not_inherit_old_failure_state():
    assert repair_mode([failed(), {"tool":"question", "status":"completed"}]) is None
    assert repair_mode([{"tool":"question", "status":"error", "metadata":{"failure_code":"other"}}]) is None


@pytest.mark.parametrize("prior", [OUT_OF_SCOPE, UNAVAILABLE])
async def test_same_provider_batch_cannot_run_diagnostics_after_review_rejection(monkeypatch, prior):
    from agent import processor as p
    from agent.agent import AgentDef
    from tests.unit.test_processor_outcomes import Info, Ctx, NotAborted, fake_stream
    from tool.tool import ToolResult
    calls = []
    async def question(args, ctx):
        calls.append("question")
        return ToolResult(output="Review blocked", metadata={"error":prior, "failure_code":prior})
    async def bash(args, ctx):
        calls.append("bash")
        raise AssertionError("A content check must not launch platform diagnostics")
    async def noop(*a, **kw): pass
    class Hooks:
        async def wrap_execute(self, name, execute, args, ctx, **kwargs):
            return await execute(args, ctx)
    monkeypatch.setattr(p, "save_part", noop)
    monkeypatch.setattr(p, "update_message_info", noop)
    monkeypatch.setattr(p.bus, "publish", lambda *a, **kw: None)
    monkeypatch.setattr(p, "stream_llm", fake_stream(events=[
        {"type":"tool_call", "tool":"question", "args":{}, "call_id":"review", "invalid":False},
        {"type":"tool_call", "tool":"bash", "args":{"command":"ls /opt"}, "call_id":"diagnose", "invalid":False},
        {"type":"finish", "reason":"tool_calls", "usage":{}},
    ]))
    result = await p.process_step(session_id="s1", user_id="u1", session=None,
        agent_def=AgentDef(name="build", description="test"), system=[], llm_messages=[],
        tools={"question":SimpleNamespace(execute=question), "bash":SimpleNamespace(execute=bash)},
        model_id="test/model", ctx=Ctx(), hooks=Hooks(), assistant_info=Info(), sandbox=None,
        abort=NotAborted(), doom_loop_history=[])
    assert calls == ["question"]
    assert result.completed_tool_parts[-1].metadata["failure_code"] == REPAIR_ONLY
