"""Provider classification, bounded retries and durable team settlement together.

Only provider events and transcript persistence are substituted; Driver,
Inbox, team journal and scheduler use the isolated test database.
"""
import pytest

from agent import driver, inbox
from agent.loop import _run_provider_attempts
from agent.processor import StepOutcome
from agent.retry import RetryableError
from team import commands, lifecycle, runtime_binding, scheduler
from team.journal import command, snapshot
from tests.unit.test_processor_outcomes import run as classify, stub_side_effects
from tests.unit.test_team_commands import make_task, setup_team
from tests.unit.test_team_runtime import config


async def failed_request(monkeypatch, error):
    calls, retries = [], []

    async def attempt():
        calls.append(1)
        return await classify(monkeypatch, raises=error)

    async def checkpoint(number, maximum, delay, result):
        retries.append((number, maximum, delay, result.retry_reason))

    async def no_sleep(_delay):
        pass

    result, count = await _run_provider_attempts(attempt, checkpoint, max_retries=5, sleep=no_sleep)
    assert result.outcome is StepOutcome.RETRY
    assert count == 5 and len(calls) == 6 and len(retries) == 5
    if error.status_code == 429:
        assert {entry[2] for entry in retries} == {0.05}
    return result


def binding(run, actor, root, member, project):
    return runtime_binding.RuntimeBinding(run, member, actor.owner_user_id, actor.workspace_id,
        project, "coordinator" if member == root else "member", {}, {})


@pytest.mark.parametrize("status", [429, 503])
async def test_exhausted_provider_retry_blocks_one_attempt_and_other_team_continues(config, monkeypatch, status):
    run, actor, server, root, members = await setup_team()
    other, owner, other_server, _, other_members = await setup_team(scope=actor)
    task = await make_task(run, server, members[0], deliverable=True)
    other_task = await make_task(other, other_server, other_members[0], deliverable=True)
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    await command(other, other_server, "dispatch", {}, commands.dispatch_ready)
    lease, _ = await inbox._reserve_and_claim(members[0], actor.owner_user_id)
    state = await snapshot(run, actor)
    token = runtime_binding._current.set(binding(run, actor, root, members[0], state["run"]["project_id"]))
    try:
        await lifecycle.turn_started(lease)
        await failed_request(monkeypatch, RetryableError("Temporary provider failure", status, {"retry-after-ms": "50"}))
        await lifecycle.turn_ended(lease, text="", outcome="error")
        await inbox.settle_claimed_inbox_items(lease, result_message_id=None, outcome="error")
    finally:
        await lease.release(session_status="error")
        runtime_binding._current.reset(token)
    await scheduler.tick(run, actor)
    failed = await snapshot(run, actor)
    assert failed["tasks"][task["id"]]["state"] == "blocked"
    assert len(failed["attempts"]) == 1
    await scheduler.tick(run, actor)
    assert len((await snapshot(run, actor))["attempts"]) == 1

    async def submit(writer):
        item = writer.state["tasks"][other_task["id"]]
        return await commands.update_task(writer, item["id"], item["revision"], "submit", summary="Independent result preserved")
    completed = await command(other, other_server, "result", {}, submit)
    assert completed["task"]["state"] == "review"
    assert (await snapshot(other, owner))["run"]["state"] == "running"
    assert failed == await snapshot(run, actor, rebuild=True)


async def test_repeated_coordinator_429_pauses_only_its_team_and_keeps_accepted_results(config, monkeypatch):
    run, actor, server, root, members = await setup_team()
    other, owner, _, _, _ = await setup_team(scope=actor)
    task = await make_task(run, server, members[0])
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    await command(run, server, "result", {}, lambda writer: commands.update_task(
        writer, task["id"], 2, "submit", summary="Already accepted result"))
    state = await snapshot(run, actor)
    for number in range(1, 4):
        lease = await driver.reserve_run(root, actor.owner_user_id)
        token = runtime_binding._current.set(binding(run, actor, root, root, state["run"]["project_id"]))
        try:
            await lifecycle.turn_started(lease)
            await failed_request(monkeypatch, RetryableError("Rate limited", 429, {"retry-after-ms": "50"}))
            await lifecycle.turn_ended(lease, text="", outcome="error")
        finally:
            await lease.release(session_status="error")
            runtime_binding._current.reset(token)
        await scheduler.tick(run, actor)
        state = await snapshot(run, actor)
        assert state["members"][root]["consecutive_failures"] == number
        assert len([notice for notice in state["notices"] if notice["code"] == "COORDINATOR_EXECUTION_FAILED"]) == number
    assert state["run"]["state"] == "paused" and state["run"]["pause_reason"] == "coordinator_errors"
    assert state["tasks"][task["id"]]["state"] == "succeeded"
    assert (await snapshot(other, owner))["run"]["state"] == "running"
    assert state == await snapshot(run, actor, rebuild=True)
