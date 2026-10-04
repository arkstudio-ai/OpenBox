"""Plan-mode preparation uses the real Driver, SQL ledger and HTTP hooks."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from agent import loop
from assistant.policy import AssistantError
from assistant.scheduling import TaskSchedulingHeld
from db.base import close_engine, get_engine, init_engine
from sandbox.runtime_operation import RuntimePreparationUncertain
from session.session import get_session
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import resource, close, drain  # noqa: F401
from tests.unit.test_assistant_resource_gateway import gateway  # noqa: F401
from tests.unit.test_assistant_runtime_resource import runtime, rows, set_task_intent  # noqa: F401
from tests.unit.test_assistant_resource_commands import remote, accept  # noqa: F401


async def remind(runtime, *, agent="plan", previous="build", step=0, run_fence=None):
    ctx = runtime[0]
    session = await get_session(ctx.session_id, user_id=ctx.user_id)
    return await loop._insert_reminders([{"role": "user", "content": "Prepare this plan"}],
        SimpleNamespace(name=agent), session=session, prev_agent=previous,
        sandbox=ctx.sandbox, user_id=ctx.user_id,
        run_fence=ctx.run_fence if run_fence is None else run_fence, preparation_step=step)


@pytest.fixture
def plan_transport(runtime):
    ctx, sent, transport, _ = runtime
    async def respond(request):
        await transport(request)  # Asserts persisted admission and resource headers.
        command = json.loads(request.content)["command"]
        return httpx.Response(200, json={"exit_code": 0,
            "stdout": "missing" if "test -f" in command else "", "stderr": ""})
    ctx.sandbox._transport = httpx.MockTransport(respond)
    return respond


async def test_plan_probe_and_directory_share_durable_origin_and_replay_after_reopen(runtime, plan_transport):
    ctx, sent, _, _ = runtime
    first = await remind(runtime)
    assert len(sent) == 2
    operation, = await rows(ctx)
    assert operation.operation == "plan_entry" and operation.state == "succeeded"
    assert operation.provider_receipt["result"] == {"exists": False}
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    assert await remind(runtime) == first
    assert len(sent) == 2 and len(await rows(ctx)) == 1


async def test_paused_plan_preparation_cannot_be_silently_treated_as_a_missing_file(runtime, plan_transport):
    ctx, sent, _, _ = runtime
    await set_task_intent(ctx, "paused")
    with pytest.raises(TaskSchedulingHeld):
        await remind(runtime)
    assert not sent and not await rows(ctx)


async def test_closed_resource_is_rejected_before_plan_probe(runtime, plan_transport, resource):
    await close(resource)
    with pytest.raises(AssistantError):
        await remind(runtime)
    assert not runtime[1]


async def test_completed_plan_receipt_still_obeys_a_later_task_hold(runtime, plan_transport):
    await remind(runtime)
    await set_task_intent(runtime[0], "paused")
    with pytest.raises(TaskSchedulingHeld):
        await remind(runtime)
    assert len(runtime[1]) == 2 and len(await rows(runtime[0])) == 1


async def test_different_step_rechecks_plan_and_build_transition_never_creates_a_directory(
        runtime, plan_transport):
    await remind(runtime)
    await remind(runtime, step=1)
    assert len(runtime[1]) == 4
    await remind(runtime, agent="build", previous="plan", step=2)
    # Managed plan approval supplies its own frozen input and skips this legacy
    # reminder. The ordinary transition still probes without creating a file.
    assert len(runtime[1]) == 4
    ctx = runtime[0]
    session = await get_session(ctx.session_id, user_id=ctx.user_id)
    session = session.model_copy(update={"memory_policy": "standard"})
    await loop._insert_reminders([{"role": "user", "content": "Build the plan"}],
        SimpleNamespace(name="build"), session=session, prev_agent="plan",
        sandbox=ctx.sandbox, user_id=ctx.user_id, run_fence=ctx.run_fence, preparation_step=2)
    assert len(runtime[1]) == 5
    assert [row.operation for row in await rows(runtime[0])] == [
        "plan_entry", "plan_entry", "plan_transition"]


async def test_stale_explicit_driver_fence_cannot_prepare_a_plan(runtime, plan_transport):
    session_id, run_id, generation = runtime[0].run_fence
    with pytest.raises(AssistantError):
        await remind(runtime, run_fence=(session_id, run_id, generation + 1))
    assert not runtime[1] and not await rows(runtime[0])


@pytest.mark.parametrize("interrupt", ["close", "pause", "timeout", "cancel"])
async def test_mid_preparation_interrupt_never_creates_directory_or_replays_unknown(
        runtime, plan_transport, resource, interrupt):
    ctx, sent, _, _ = runtime
    entered = asyncio.Event()
    async def respond(request):
        response = await plan_transport(request)
        entered.set()
        if interrupt == "close":
            await close(resource)
        elif interrupt == "pause":
            await set_task_intent(ctx, "paused")
        elif interrupt == "timeout":
            raise httpx.ReadTimeout("synthetic lost probe response", request=request)
        else:
            await asyncio.Event().wait()
        return response
    ctx.sandbox._transport = httpx.MockTransport(respond)
    pending = asyncio.create_task(remind(runtime))
    await asyncio.wait_for(entered.wait(), 5)
    if interrupt == "cancel":
        pending.cancel()
    with pytest.raises((AssistantError, RuntimePreparationUncertain, asyncio.CancelledError)):
        await pending
    assert len(sent) == 1
    operation, = await rows(ctx)
    assert operation.state == "outcome_unknown"
    assert (await drain(resource))["blocking_effect_ids"] == [operation.id]
    ctx.sandbox._transport = httpx.MockTransport(plan_transport)
    with pytest.raises((AssistantError, RuntimePreparationUncertain)):
        await remind(runtime)
    assert len(sent) == 1


async def test_nonphysical_plan_compatibility_quotes_paths_and_keeps_best_effort(monkeypatch):
    calls = []
    path = "/workspace/project with space/.openbox/plans/example.md"
    async def plan_path(_):
        return path
    async def execute(command, **_):
        calls.append(command)
        return SimpleNamespace(stdout="missing", exit_code=0)
    monkeypatch.setattr("session.session.plan_path_for", plan_path)
    session = SimpleNamespace(id="ordinary", memory_policy="standard")
    await loop._insert_reminders([{"role": "user", "content": "plan"}], SimpleNamespace(name="plan"),
        session=session, prev_agent="build", sandbox=SimpleNamespace(execute=execute))
    assert len(calls) == 2 and "'" + path + "'" in calls[0]
    assert "$(dirname" not in calls[1]


async def test_real_v2_server_prepares_only_the_fixed_plan_directory(runtime, remote, resource, tmp_path, monkeypatch):
    from assistant import resource_commands as commands
    accepted = await accept(remote, resource, "bind")
    assert await commands.dispatch(accepted["command_id"])
    path = tmp_path / "plan fixture" / "draft.md"
    async def plan_path(_):
        return str(path)
    monkeypatch.setattr("session.session.plan_path_for", plan_path)
    await remind(runtime)
    assert path.parent.is_dir() and not path.exists()
    operation, = await rows(runtime[0])
    assert operation.state == "succeeded"
    assert operation.safe_context["resource_journal_id"] == remote[1].status()["journal_id"]
    status = remote[1].status()
    # A successful opaque shell response cannot prove remote descendants have
    # stopped. Both requests retain their original effect in the remote journal.
    assert status["blocking_count"] == 2
    assert {row["effect_id"] for row in status["blocking_operations"]} == {operation.id}
    assert {row["state"] for row in status["blocking_operations"]} == {"unknown"}
    assert not status["tracked_operations_drained"] and not status["remote_exclusivity_verified"]
