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
    from core.config import get_config, ProviderConfig
    config = get_config().model_copy(deep=True)
    config.provider["openai"] = ProviderConfig(api_key="test-key", base_url="https://provider.invalid/v1")
    monkeypatch.setattr("core.config.get_config", lambda: config)
    from agent import driver
    from agent.subagent_authority import compose_subagent_authority
    from permission.permission import Rule
    from models.message import ToolPartData, TextPart
    from session.session import (create_user_message, create_assistant_message,
                                 save_part, update_message_info)
    from tool.collaboration import task
    from trajectory import bind

    prompt = await create_user_message("s1", "Delegate", user_id="u1")
    lease = await driver.reserve_run("s1", "u1", trigger_message_id=prompt.id)
    assistant = await create_assistant_message("s1", prompt.id, user_id="u1")
    card = ToolPartData(id="call-task", tool="task", status="running", input={},
                        session_id="s1", message_id=assistant.id, call_id="delegate")
    await save_part(card, is_new=True, user_id="u1")
    parent = TraceContext("u1", "s1", turn_id="turn", run_id=lease.run_id,
                          agent_id="agent-parent", call_id=card.id)
    ctx = ToolContext(session_id="s1", user_id="u1", workspace_id="w1", trace_context=parent,
                      message_id=assistant.id, part_id=card.id, run_id=lease.run_id,
                      run_generation=lease.generation)
    ctx._subagent_authority_snapshot = compose_subagent_authority(
        tool_ids=("task",), permission_rules=[Rule(permission="*", pattern="*", action="allow")],
        guard_rules=(),
    ).to_json()

    async def complete_child(ctx, child_id, child_lease):
        from session.session import get_messages
        user_message = (await get_messages(child_id, user_id="u1"))[-1]
        fence = (child_id, child_lease.run_id, child_lease.generation)
        answer = await create_assistant_message(child_id, user_message.id, user_id="u1", run_fence=fence)
        await save_part(TextPart(text="Files found", session_id=child_id, message_id=answer.id),
                        is_new=True, user_id="u1", run_fence=fence)
        answer.finish = "stop"
        await update_message_info(answer, user_id="u1", run_fence=fence)
        await child_lease.release()

    monkeypatch.setattr(task, "_run_child", complete_child)
    token = driver.bind_current_lease(lease)
    try:
        with bind(parent):
            result = await task.execute(task.TaskArgs(description="Look around", prompt="List the files",
                                                      subagent_type="explore"), ctx)
    finally:
        driver.reset_current_lease(token)
        await lease.release()

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
