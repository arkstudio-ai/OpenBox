"""Product contracts inherited from main, independent of kernel internals."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent import driver
from api import sessions as api
from models.message import TextPart
from session.fork import fork_session
from session.session import (
    create_assistant_message, create_user_message, get_messages, get_session,
    save_part, update_message_info,
)
from tests.unit.test_durable_questions import checkpoint, state  # noqa: F401
from tests.unit.test_run_fencing_api import loop_harness  # noqa: F401
from tool.tool import ToolContext, ToolResult, define_tool


async def _answer(prompt, text, finish=None):
    answer = await create_assistant_message("s1", prompt.id, user_id="u1")
    await save_part(TextPart(text=text, message_id=answer.id, session_id="s1"),
                    is_new=True, user_id="u1")
    if finish:
        answer.finish = finish
        await update_message_info(answer, user_id="u1")
    return answer


async def test_empty_session_can_be_forked(state):
    child = await fork_session("s1", user_id="u1")
    assert child.workspace_id == "w1" and child.project_id == "p1"
    assert await get_messages(child.id, user_id="u1") == []
    assert await driver.get_driver_state(child.id) is None


@pytest.mark.parametrize("cutoff", ["user", "tool_step", "unfinished", "all"])
async def test_fork_preserves_arbitrary_message_cutoff_and_open_history(state, cutoff):
    first = await create_user_message("s1", "First question", user_id="u1")
    middle = await _answer(first, "Working", "tool_calls")
    await _answer(first, "First answer", "stop")
    later = await create_user_message("s1", "Still working", user_id="u1")
    unfinished = await _answer(later, "Partial answer")
    choices = {"user": first.id, "tool_step": middle.id, "unfinished": unfinished.id, "all": None}
    source = await get_messages("s1", user_id="u1")
    expected_count = {"user": 1, "tool_step": 2, "unfinished": 5, "all": 5}[cutoff]

    child = await fork_session("s1", choices[cutoff], user_id="u1")
    copied = await get_messages(child.id, user_id="u1")

    assert len(copied) == expected_count
    assert [message.role for message in copied] == [message.role for message in source[:expected_count]]
    assert [message.finish for message in copied] == [message.finish for message in source[:expected_count]]
    assert all(message.id not in {original.id for original in source} for message in copied)
    if len(copied) > 1:
        assert copied[1].parent_id == copied[0].id
    assert len(await get_messages("s1", user_id="u1")) == len(source)
    assert await driver.get_driver_state(child.id) is None


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_default_send_preempts_current_turn_and_returns_complete_answer(
    state, loop_harness, monkeypatch, asynchronous,
):
    prompt = await create_user_message("s1", "Previous request", user_id="u1")
    old = await driver.reserve_run("s1", "u1", trigger_message_id=prompt.id)
    interrupted = []

    async def old_worker():
        await old.abort.wait()
        interrupted.append(old.run_id)
        await old.release(session_status="idle")

    async def provider(**kwargs):
        yield {"type": "text_delta", "text": "New answer"}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(loop_harness.processor, "stream_llm", provider)
    monkeypatch.setattr("agent.suggestions.generate_suggestions", AsyncMock(return_value=[]))
    monkeypatch.setattr(api, "check_concurrent_agents", AsyncMock())
    monkeypatch.setattr(api, "_remember_prompt_history", lambda *_args: None)
    retiring = asyncio.create_task(old_worker())
    try:
        endpoint = api.send_message_async if asynchronous else api.send_message
        response = await asyncio.wait_for(endpoint(
            "s1", api.PromptBody(text="New request", video_model="selected-video", video_resolution="720p"),
            current_user={"user_id": "u1", "workspace_id": "w1"},
        ), timeout=5)
        if asynchronous:
            assert response["ok"] is True
            await asyncio.wait_for(asyncio.gather(*tuple(api._background_tasks)), timeout=5)
        else:
            assert response["finish"] == "stop"
            assert any(part.get("text") == "New answer" for part in response["parts"])
        assert interrupted == [old.run_id]
        current = await driver.get_driver_state("s1")
        assert current.generation > old.generation and current.phase == "idle"
        session = await get_session("s1", user_id="u1")
        assert (session.video_model, session.video_resolution) == ("selected-video", "720p")
        messages = await get_messages("s1", user_id="u1")
        assert any((message.client_message_id or "").startswith("tabort:") for message in messages)
        assert messages[-1].finish == "stop"
    finally:
        retiring.cancel()
        await asyncio.gather(retiring, return_exceptions=True)
        await old.release()


async def test_stop_cancels_waiting_question_after_driver_has_released(state):
    from question import question

    await checkpoint()
    assert (await get_session("s1", user_id="u1")).status == "waiting_input"
    await api.abort_session("s1", current_user={"user_id": "u1", "workspace_id": "w1"})
    assert not await question.list_pending(user_id="u1")
    assert (await get_session("s1", user_id="u1")).status == "idle"


async def test_question_resume_failure_keeps_versioned_error_details(state):
    from sqlalchemy import delete
    from db.base import get_db_session
    from db.models.part import Part
    from question import question
    from question.continuation import QuestionContinuationWorker

    lease = await driver.reserve_run("s1", "u1")
    await lease.release(session_status="idle")
    request_id = await checkpoint(tool="plan_enter", continuation={"kind": "plan_enter"})
    await question.reply(request_id, [["Yes"]], "u1")
    async with get_db_session() as db:
        await db.execute(delete(Part).where(Part.id == "p1"))
    await QuestionContinuationWorker()._resume_candidate("s1", "u1", 0)

    errors = [payload for event, payload in state if event == "session.error"]
    assert len(errors) == 1
    assert errors[0]["generation"] == lease.generation
    assert errors[0]["error"]["code"] == "QUESTION_RESUME_FAILED"
    assert (await get_session("s1", user_id="u1")).status == "error"


async def test_delayed_question_failure_does_not_replace_new_run(state):
    from question import runtime

    lease = await driver.reserve_run("s1", "u1")
    try:
        await runtime.publish_status("s1", "u1", "error", error={"code": "OLD_FAILURE"})
        assert not any(event == "session.error" for event, _ in state)
        assert state[-1][1]["status"] == "busy"
        assert state[-1][1]["generation"] == lease.generation
    finally:
        await lease.release()


@pytest.mark.parametrize("path", [
    "/tmp/render.png", "/opt/openbox/skills/dev-browser/SKILL.md",
    "/data/skills/my-skill/script.py", "/workspace/uploads/input.pdf",
    "/workspace/other-project/out.txt", "/tmp/space at end ",
])
def test_existing_absolute_sandbox_paths_remain_available(path):
    ctx = ToolContext(workdir="/workspace/project", user_id="u1")
    assert ctx.resolve_file_path(path) == path
    assert ctx.resolve_file_path("../exports/output.txt") == "/workspace/exports/output.txt"


async def test_canonical_alias_still_obeys_explicit_permissions():
    from agent.hooks import ToolHooks
    from permission.permission import Rule
    from sandbox.client import ResolvedPath

    sandbox = SimpleNamespace(resolve_paths=AsyncMock(return_value=[ResolvedPath("/tmp/blocked.txt")]))
    hooks = ToolHooks(session_id="s1", user_id="u1", config_rules=[
        Rule(permission="*", pattern="*", action="allow"),
        Rule(permission="read", pattern="/tmp/blocked.txt", action="deny"),
    ])
    result = await hooks.authorize_tool("read", {"file_path": "/workspace/alias"},
                                       ctx=ToolContext(sandbox=sandbox))
    assert result is not None and result.metadata["blocked"]


async def test_batch_keeps_legacy_tools_and_runs_unknown_mutations_in_order(monkeypatch):
    from tool import registry
    from tool.batch import BatchArgs, Invocation, execute

    order = []

    async def legacy(_args, _ctx):
        order.append("start")
        await asyncio.sleep(0)
        order.append("finish")
        return ToolResult(title="saved", output="ok")

    tool = define_tool("legacy-write", description="legacy", parameters=BatchArgs,
                       execute=legacy, sandbox_required=False)
    monkeypatch.setitem(registry._tools, tool.id, tool)
    result = await execute(BatchArgs(invocations=[
        Invocation(tool=tool.id, parameters={"invocations": []})] * 2),
        ToolContext(available_tools=frozenset({"batch", tool.id}),
                    _authorize_tool=AsyncMock(return_value=None)))
    assert order == ["start", "finish", "start", "finish"]
    assert result.output.count("saved") == 2


async def test_cron_result_survives_model_context_fork_and_read_model_rebuild(state):
    from cron.injector import _commit_injection
    from session.agent_event_log import load_canonical_model_surface, rebuild_sql_read_model_from_events

    # Seed canonical history first, as every previously used conversation has.
    prompt = await create_user_message("s1", "Remember this", user_id="u1")
    await _answer(prompt, "Remembered", "stop")
    await load_canonical_model_surface("s1", user_id="u1")
    await _commit_injection("s1", "u1", "job-1", "Scheduled report", "Check progress", "Cron result", "en-US")
    public = await get_messages("s1", user_id="u1")
    canonical = await load_canonical_model_surface("s1", user_id="u1")
    assert [message.id for message in canonical.messages] == [message.id for message in public]
    assert public[-1].parts[0].text == "Cron result"
    child = await fork_session("s1", user_id="u1")
    assert (await get_messages(child.id, user_id="u1"))[-1].parts[0].text == "Cron result"
    await rebuild_sql_read_model_from_events("s1", user_id="u1")
    rebuilt = await get_messages("s1", user_id="u1")
    assert [message.model_dump() for message in rebuilt] == [message.model_dump() for message in public]


async def test_batch_uses_advertised_tool_and_cancels_peers_when_run_is_lost(monkeypatch):
    from tool import registry
    from tool.batch import BatchArgs, Invocation, execute

    started = asyncio.Event()
    canceled = []

    async def slow(_args, _ctx):
        started.set()
        try:
            await asyncio.Future()
        finally:
            canceled.append(True)

    async def expired(_args, _ctx):
        await started.wait()
        raise driver.LeaseLostError("replaced run")

    selected = {
        name: define_tool(name, description="test", parameters=BatchArgs,
                          execute=body, sandbox_required=False, parallel_safe=True)
        for name, body in (("slow", slow), ("expired", expired))
    }
    monkeypatch.setattr(registry, "get_tool", lambda *_args: pytest.fail("live registry bypassed frozen tools"))
    with pytest.raises(driver.LeaseLostError, match="replaced run"):
        await execute(BatchArgs(invocations=[
            Invocation(tool=name, parameters={"invocations": []}) for name in selected]),
            ToolContext(available_tools=frozenset({"batch", *selected}),
                        _tool_execution_lookup=selected,
                        _authorize_tool=AsyncMock(return_value=None)))
    assert canceled == [True]
