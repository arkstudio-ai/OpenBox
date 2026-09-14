"""With recording on, business flows send no statement that touches a trajectory table (SPEC §0.1, §14.3).

The flows run against the migrated business schema (SQLite, or PostgreSQL with
OBX_QUESTION_TEST_DATABASE_URL) through the spool sink: chat with a tool call
through the real loop, adapter and request capture, a durable question and its
resumed run, a cron run with its injection, fork, revert, asset upload and
delete, and session deletion. Their facts reach the spool instead.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel
from sqlalchemy import event

import db.base as database
from db.models.cron import CronJob, CronRun
from db.models.file_asset import FileAsset
from db.models.part import Part
from models.message import TodoItem, TodoList
from question import question as q
from question import runtime
from session.session import create_user_message, delete_session
from tests.unit.test_durable_questions import checkpoint, read, state  # noqa: F401
from tests.unit.test_run_fencing_api import loop_harness  # noqa: F401
from tests.unit.test_run_fencing_execution import resumed_runs, sweep
from tests.unit.trajectory_producer_support import recording_spool  # noqa: F401
from tool.tool import ToolResult, define_tool


class _Label(BaseModel):
    label: str


@pytest.fixture
def trajectory_sql(state):
    """Business statements naming any trajectory table, legacy names included."""
    touched = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if "trajector" in statement.lower():
            touched.append(statement)
    engine = database._engine.sync_engine
    event.listen(engine, "before_cursor_execute", listener)
    yield touched
    event.remove(engine, "before_cursor_execute", listener)


def _chunk(*, content=None, name=None, arguments=None):
    calls = [] if arguments is None else [SimpleNamespace(
        index=0, id="provider-call", function=SimpleNamespace(name=name, arguments=arguments))]
    return SimpleNamespace(choices=[SimpleNamespace(index=0, finish_reason=None, delta=SimpleNamespace(
        content=content, reasoning_content=None, tool_calls=calls))], usage=None)


@pytest.fixture
def scripted_provider(loop_harness, monkeypatch):  # noqa: F811
    """The real stream_llm, adapter and RequestCapture over a scripted LiteLLM completion."""
    import litellm
    from agent import llm
    from billing.service import UsageMeter
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        number = len(calls)

        async def stream():
            if number == 1:
                yield _chunk(name=kwargs["tools"][0]["function"]["name"], arguments='{"label": "work"}')
            else:
                yield _chunk(content="All done.")
        return stream()
    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(llm, "_needs_responses_api", lambda _model: False)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _model: {})
    monkeypatch.setattr(llm, "_get_variant_kwargs", lambda *_args: {})
    monkeypatch.setattr(llm, "_get_max_output_tokens", lambda _model: 100)
    monkeypatch.setattr(UsageMeter, "start", AsyncMock(return_value=None))
    return calls


async def test_business_flows_with_recording_on_never_touch_trajectory_tables(
        state, recording_spool, loop_harness, scripted_provider, trajectory_sql, monkeypatch):  # noqa: F811
    from api import assets
    from cron import executor
    from session import revert
    from session.fork import fork_session
    from session.todo import save_todo
    monkeypatch.setattr("agent.suggestions.generate_suggestions", AsyncMock(return_value=None))
    monkeypatch.setattr("sandbox.sandbox_manager.release", AsyncMock())

    async def effect(arguments, ctx):
        await save_todo(ctx.session_id, TodoList(items=[TodoItem(id="t1", subject=arguments.label,
                                                                 status="completed")]), user_id=ctx.user_id)
        return ToolResult(output=f"did {arguments.label}")
    loop_harness.tools["effect"] = define_tool("effect", description="test only", parameters=_Label,
                                               execute=effect, sandbox_required=False)

    # Chat with a tool call.
    prompt = await create_user_message("s1", "Do the work", user_id="u1")
    assert await asyncio.wait_for(loop_harness.loop.run_loop("s1", user_id="u1"), timeout=10) is not None
    assert len(scripted_provider) >= 2

    # A durable question and its resumed run.
    request_id = await checkpoint(part_id="p-question")
    await q.reply(request_id, [["Yes"]], "u1")
    started = resumed_runs(monkeypatch)
    await sweep()
    assert started == ["s1"]

    # A cron run whose result is injected into the conversation.
    async with database.get_db_session() as db:
        db.add(CronJob(id="job-isolation", user_id="u1", workspace_id="w1", project_id="p1", session_id="s1",
                       name="Daily", schedule={"kind": "every", "every_seconds": 3600}, task_prompt="Report",
                       created_at=runtime.now(), updated_at=runtime.now()))
    monkeypatch.setattr(executor, "_get_session_summary", AsyncMock(return_value=""))
    monkeypatch.setattr(executor, "_run_agent_loop", AsyncMock(return_value="Nightly report"))
    monkeypatch.setattr("cron.runlog.append_run_log", AsyncMock())
    outcome = await executor.execute_cron_job({"id": "job-isolation", "user_id": "u1", "session_id": "s1",
                                               "workspace_id": "w1", "project_id": "p1", "name": "Daily",
                                               "task_prompt": "Report", "schedule": {"kind": "every"}})
    assert outcome["status"] == "ok"
    assert (await read(CronRun, outcome["run_id"])).injected

    # Fork and revert.
    await fork_session("s1", user_id="u1")
    async with database.get_db_session() as db:
        db.add(Part(id="p-step", session_id="s1", message_id=prompt.id, user_id="u1", type="step-start",
                    data={"id": "p-step", "type": "step-start", "snapshot": "snap-before",
                          "session_id": "s1", "message_id": prompt.id}, created_at=runtime.now()))
    monkeypatch.setattr(revert.snapshot, "track", AsyncMock(return_value="snap-now"))
    monkeypatch.setattr(revert.snapshot, "restore", AsyncMock(return_value=True))
    assert await revert.revert_to_message("s1", prompt.id, user_id="u1")

    # Asset upload and delete.
    oss = SimpleNamespace(head=AsyncMock(return_value={"size": 5}), delete=AsyncMock(),
                          presign_get=lambda key, **_kwargs: f"https://oss.example/{key}")
    monkeypatch.setattr(assets, "_oss_or_503", lambda: oss)
    async with database.get_db_session() as db:
        db.add(FileAsset(id="asset-isolation", user_id="u1", workspace_id="w1", session_id="s1", name="notes.txt",
                         oss_key="assets/u1/asset-isolation/notes.txt", mime="text/plain", size=0,
                         status="pending", created_at=runtime.now()))
    user = {"user_id": "u1", "workspace_id": "w1"}
    await assets.complete_asset("asset-isolation", current_user=user, _workspace={})
    await assets.delete_asset("asset-isolation", current_user=user, _workspace={})

    # Session deletion.
    assert await delete_session("s1", user_id="u1")

    assert trajectory_sql == []
    events = {item["type"] for item in recording_spool.events()}
    assert {"baseline.captured", "turn.started", "input.accepted", "run.started", "step.started",
            "request.prepared", "request.started", "request.delta", "request.finished", "tool.requested",
            "tool.started", "tool.output", "tool.finished", "todo.changed", "question.asked", "question.resolved",
            "input.injected", "job.submitted", "job.finished", "history.forked", "history.reverted",
            "artifact.recorded", "run.finished"} <= events
    assert {"asset.deleted", "session.deleted"} <= {item["type"] for item in recording_spool.controls()}
