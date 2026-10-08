"""Bind execution-page controls to the same durable Task commands as cards."""
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from assistant.commands import _authority, task_locked
from assistant.control import accept_control_command
from assistant.policy import AssistantError
from assistant.steering import ExpectedRun
from assistant.transactions import read_session
from db.models.agent_driver import AgentDriverState
from db.models.assistant import AssistantTask


class TaskStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=1, strict=True)
    expected_run: ExpectedRun | None
    idempotency_key: str = Field(min_length=1, max_length=64)


class StopBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_control: TaskStop | None = None


async def target_for_session(session, user_id):
    """A read-only, direct-task target; stopping a child cannot cancel its parent."""
    if session.kind == "assistant" or session.memory_policy != "assistant_isolated":
        return None
    async with read_session() as db:
        task = await db.scalar(select(AssistantTask).where(
            AssistantTask.execution_session_id == session.id))
        if task is None:
            return None
        await _authority(db, user_id=user_id, workspace_id=session.workspace_id,
                         main_id=task.assistant_session_id)
        task, execution = await task_locked(db, user_id=user_id, workspace_id=session.workspace_id,
            main_id=task.assistant_session_id, task_id=task.id)
        driver = await db.get(AgentDriverState, execution.id)
        run = ({"run_id": driver.run_id, "generation": driver.generation}
               if driver and driver.run_id and driver.phase != "idle" else None)
        return {"task_id": task.id, "expected_revision": task.control_revision,
                "expected_run": run, "desired_state": task.desired_state,
                "observed_state": task.observed_state}


async def stop_task(session, user_id, body: StopBody | None):
    """Return None only when the ordinary session stop remains applicable."""
    target = body.task_control if body else None
    isolated = session.kind != "assistant" and session.memory_policy == "assistant_isolated"
    if not isolated:
        if target is not None:
            raise AssistantError(409, "ASSISTANT_STOP_TARGET_CHANGED", "The task stop target is unavailable; reload this conversation")
        return None
    if target is None:
        from assistant.scheduling import require_runnable
        await require_runnable(session.id, user_id)
        raise AssistantError(409, "ASSISTANT_TASK_CONTROL_REQUIRED",
            "Reload this execution page or use its original task card to stop the task")
    async with read_session() as db:
        task = await db.scalar(select(AssistantTask).where(
            AssistantTask.execution_session_id == session.id,
            AssistantTask.id == target.task_id, AssistantTask.user_id == user_id,
            AssistantTask.workspace_id == session.workspace_id))
        if task is None:
            raise AssistantError(409, "ASSISTANT_STOP_TARGET_CHANGED", "The task stop target is unavailable; reload this conversation")
        main_id = task.assistant_session_id
    # This service revalidates authority, revision and exact run atomically,
    # and replays the original receipt before checking newer Task state.
    return await accept_control_command(user_id=user_id, workspace_id=session.workspace_id,
        main_id=main_id, task_id=target.task_id, action="cancel",
        idempotency_key=target.idempotency_key, expected_revision=target.expected_revision,
        expected_run=target.expected_run.model_dump() if target.expected_run else None)
