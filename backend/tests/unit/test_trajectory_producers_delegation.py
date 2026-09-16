"""Subagent and cron recovery producers on the spool."""
from unittest.mock import AsyncMock

from db.models.cron import CronRun
from question import runtime
from tests.unit.test_durable_questions import read, state  # noqa: F401
from tests.unit.trajectory_producer_support import business_statements, recording_spool  # noqa: F401
from tool.tool import ToolContext
from trajectory import TraceContext


async def test_a_subagent_spawn_records_its_lifecycle_under_the_parent_trajectory(state, recording_spool,
                                                                                monkeypatch):
    from tool import task
    monkeypatch.setattr(task, "_run_child", AsyncMock())
    parent = TraceContext("u1", "s1", turn_id="turn", run_id="run", agent_id="agent-parent", call_id="call-task")
    ctx = ToolContext(session_id="s1", user_id="u1", workspace_id="w1", trace_context=parent)

    result = await task.execute(task.TaskArgs(description="Look around", prompt="List the files",
                                              subagent_type="explore"), ctx)

    child_id = result.metadata["child_session_id"]
    lifecycle = [item for item in recording_spool.events() if item["type"].startswith("agent.")]
    assert [item["type"] for item in lifecycle] == ["agent.spawned", "agent.message", "agent.finished"]
    assert {(item["session_id"], item["source_session_id"], item["parent_agent_id"], item["parent_call_id"])
            for item in lifecycle} == {("s1", child_id, "agent-parent", "call-task")}
    assert all("run_id" not in item and "call_id" not in item for item in lifecycle)
    [prompt] = [item for item in recording_spool.events("input.injected") if item["source_session_id"] == child_id]
    assert prompt["data"]["text"] == "List the files" and prompt["session_id"] == "s1"


async def test_cron_recovery_reports_interrupted_runs_and_reads_no_rows_while_recording_is_off(
        state, recording_spool, business_statements, monkeypatch):
    from cron.recovery import _mark_interrupted_runs
    from db.base import get_db_session

    async def running(run_id: str) -> None:
        async with get_db_session() as db:
            db.add(CronRun(id=run_id, job_id="job-1", user_id="u1", session_id="s1", status="running",
                           started_at=runtime.now(),
                           trace_context=TraceContext("u1", "s1", turn_id=run_id).to_dict()))

    await running("run-crashed")
    await _mark_interrupted_runs()
    events = recording_spool.events()
    assert [(item["type"], item["turn_id"]) for item in events] == [("job.finished", "run-crashed"),
                                                                     ("recording.gap", "run-crashed")]
    assert events[0]["event_id"] == "cron:run-crashed:interrupted"
    assert events[1]["data"]["reason"] == "process_restarted"
    assert (await read(CronRun, "run-crashed")).status == "error"

    await running("run-unrecorded")
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    business_statements.clear()
    await _mark_interrupted_runs()
    assert not [statement for statement in business_statements
                if statement.lstrip().upper().startswith("SELECT") and "cron_runs" in statement]
    assert (await read(CronRun, "run-unrecorded")).status == "error"
    assert len(recording_spool.events()) == 2
