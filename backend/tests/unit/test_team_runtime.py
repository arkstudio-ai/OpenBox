from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.session import Session
from team import commands, lifecycle, runtime_binding, scheduler
from team.journal import command, snapshot, utcnow
from tests.unit.test_team_commands import make_task, setup_team
from tests.unit.test_subagent_composition import _config


@pytest.fixture
def config(monkeypatch):
    from core import config as config_module
    from team import runtime
    selected = _config("openai/test")
    selected.team_wake_debounce_seconds = 0
    selected.team_max_running_members = 3
    selected.team_reserved_agent_slots = 2
    selected.max_concurrent_agents = 5
    monkeypatch.setattr(config_module, "get_config", lambda: selected)
    monkeypatch.setattr(runtime_binding, "get_config", lambda: selected)
    wakes = []
    async def wake(member_id, user_id):
        wakes.append(member_id)
    monkeypatch.setattr(runtime.runtime, "wake", wake)
    return selected, wakes


async def test_cold_member_loads_frozen_authority_and_new_generation_binds_attempt(config):
    from agent.driver import reserve_run
    run_id, actor, server, root, members = await setup_team()
    await make_task(run_id, server, members[0])
    await scheduler.tick(run_id, actor)
    assert config[1] == [members[0]]
    async with get_db_session() as db:
        session = await db.get(Session, members[0])
    authority = await runtime_binding.load_binding(session)
    assert authority.composition.model == "openai/test"
    assert authority.composition.persona.startswith("Read the sources")
    lease = await reserve_run(members[0], actor.owner_user_id)
    try:
        await lifecycle.turn_started(lease)
        state = await snapshot(run_id, actor)
        attempt = next(iter(state["attempts"].values()))
        assert (attempt["driver_run_id"], attempt["generation"]) == (lease.run_id, lease.generation)
        assert state["members"][members[0]]["execution_state"] == "running"
    finally:
        await lease.release()


async def test_worker_member_limit_spans_users_and_preserves_normal_chat(config):
    from agent.driver import reserve_run, DriverQuotaExceededError
    config[0].team_max_running_members = 1
    _, first, _, _, members_one = await setup_team()
    _, second, _, root_two, members_two = await setup_team()
    lease = await reserve_run(members_one[0], first.owner_user_id)
    try:
        with pytest.raises(DriverQuotaExceededError):
            await reserve_run(members_two[0], second.owner_user_id)
        async with get_db_session() as db:
            root_row = await db.get(Session, root_two)
            normal_id = "ordinary-" + root_two
            db.add(Session(id=normal_id, user_id=second.owner_user_id, workspace_id=second.workspace_id,
                project_id=root_row.project_id, agent="build", created_at=utcnow(), updated_at=utcnow()))
        normal = await reserve_run(normal_id, second.owner_user_id)
        await normal.release()
    finally:
        await lease.release()
    next_lease = await reserve_run(members_two[0], second.owner_user_id)
    await next_lease.release()


async def test_coordinator_counts_toward_team_capacity_and_two_ordinary_slots_remain(config):
    from agent.driver import reserve_run, DriverQuotaExceededError
    run, actor, _, root, members = await setup_team()
    _, _, _, other_root, _ = await setup_team(scope=actor)
    async with get_db_session() as db:
        root_row = await db.get(Session, root)
        ordinary = ["ordinary-a-" + root, "ordinary-b-" + root]
        for sid in ordinary:
            db.add(Session(id=sid, user_id=actor.owner_user_id, workspace_id=actor.workspace_id,
                project_id=root_row.project_id, agent="build", created_at=utcnow(), updated_at=utcnow()))
    leases = []
    try:
        for sid in [root, *members]:
            leases.append(await reserve_run(sid, actor.owner_user_id))
        with pytest.raises(DriverQuotaExceededError):
            await reserve_run(other_root, actor.owner_user_id)
        for sid in ordinary:
            leases.append(await reserve_run(sid, actor.owner_user_id))
        assert len(leases) == 5
    finally:
        for lease in leases:
            await lease.release()


async def test_lower_run_concurrency_also_applies_when_coordinator_wakes(config):
    from agent.driver import reserve_run, DriverQuotaExceededError
    from db.models.team import TeamRun
    run, actor, _, root, members = await setup_team()
    async with get_db_session() as db:
        row = await db.get(TeamRun, run)
        row.policy_snapshot = {**row.policy_snapshot, "max_concurrent_members": 1}
    child = await reserve_run(members[0], actor.owner_user_id)
    try:
        with pytest.raises(DriverQuotaExceededError):
            await reserve_run(root, actor.owner_user_id)
    finally:
        await child.release()
    coordinator = await reserve_run(root, actor.owner_user_id)
    try:
        with pytest.raises(DriverQuotaExceededError):
            await reserve_run(members[0], actor.owner_user_id)
    finally:
        await coordinator.release()


async def test_runtime_observation_is_owned_and_late_interrupt_cannot_stop_replacement(config):
    from agent.driver import reserve_run
    from team.runtime import runtime
    _, actor, _, _, members = await setup_team()
    first = await reserve_run(members[0], actor.owner_user_id)
    async with get_db_session() as db:
        original = (await runtime.observe(db, members, actor.owner_user_id))[members[0]]
        assert original.live
        assert not await runtime.observe(db, members, "different-owner")
    await first.release()
    replacement = await reserve_run(members[0], actor.owner_user_id)
    try:
        assert not await runtime.interrupt(members[0], actor.owner_user_id,
            expected_run_id=original.run_id, expected_generation=original.generation)
        assert not replacement.abort.is_set()
        assert not await replacement.abort_was_requested()
        assert await runtime.interrupt(members[0], actor.owner_user_id,
            expected_run_id=replacement.run_id, expected_generation=replacement.generation)
        assert await replacement.abort_was_requested()
    finally:
        await replacement.release()


async def test_pause_keeps_inputs_unclaimed_resume_can_wake_and_cancel_converges(config):
    run_id, actor, server, root, members = await setup_team()
    await make_task(run_id, server, members[0])
    await scheduler.tick(run_id, actor)
    async def pause(writer):
        commands.run_status(writer, "pausing", pause_reason="user")
        return {}
    await command(run_id, server, "pause", {}, pause)
    assert not await scheduler.can_wake(members[0], actor.owner_user_id)
    await scheduler.tick(run_id, actor)
    state = await snapshot(run_id, actor)
    assert state["run"]["state"] == "paused"
    async with get_db_session() as db:
        items = (await db.execute(select(AgentInboxItem).where(AgentInboxItem.session_id == members[0]))).scalars().all()
    assert items and all(row.state == "accepted" for row in items)
    async def cancel(writer):
        commands.run_status(writer, "canceling")
        return {}
    await command(run_id, server, "cancel", {}, cancel)
    await scheduler.tick(run_id, actor)
    assert (await snapshot(run_id, actor))["run"]["state"] == "canceled"
    async with get_db_session() as db:
        queued = (await db.execute(select(AgentInboxItem).where(AgentInboxItem.session_id == members[0]))).scalars().all()
        assert all(row.state == "canceled" for row in queued)
        root_row = await db.get(Session, root)
        assert root_row.agent == "build"


async def test_simultaneous_results_coalesce_into_one_root_input(config):
    run_id, actor, server, root, members = await setup_team()
    async def enqueue(writer):
        for member in members:
            await commands.queue_message(writer, from_member_id=member, to_member_id=root,
                body=f"Result from {member}", kind="result")
        return {}
    await command(run_id, server, "results", {}, enqueue)
    await scheduler.tick(run_id, actor)
    async with get_db_session() as db:
        inputs = (await db.execute(select(AgentInboxItem).where(AgentInboxItem.session_id == root))).scalars().all()
    assert len(inputs) == 1
    assert all(member in inputs[0].prompt for member in members)
    state = await snapshot(run_id, actor)
    assert {message["inbox_id"] for message in state["messages"].values()} == {inputs[0].id}
    assert config[1].count(root) == 1


async def test_result_notification_carries_current_task_revision_for_acceptance(config):
    import json
    run_id, actor, server, root, members = await setup_team()
    task = await make_task(run_id, server, members[0], deliverable=True)
    await command(run_id, server, "dispatch", {}, commands.dispatch_ready)
    async def submit(writer):
        current = writer.state["tasks"][task["id"]]
        return await commands.update_task(writer, task["id"], current["revision"], "submit", summary="Verified result")
    result = await command(run_id, server, "submit", {}, submit)
    await scheduler.tick(run_id, actor)
    async with get_db_session() as db:
        inputs = (await db.execute(select(AgentInboxItem).where(AgentInboxItem.session_id == root))).scalars().all()
    assert len(inputs) == 1
    updates = json.loads(inputs[0].prompt.split("\n", 2)[2])["team_updates"]
    assert updates[0]["task"] == {key: result["task"][key]
        for key in ("id", "state", "revision", "current_attempt", "deliverable")}
    assert updates[0]["task"]["state"] == "review"


async def test_message_delivery_recovery_is_exact_and_progress_never_wakes(config):
    run_id, actor, server, root, members = await setup_team()
    async def enqueue(writer):
        await commands.queue_message(writer, from_member_id=members[0], to_member_id=members[1], body="Draft for review")
        await commands.queue_message(writer, from_member_id=members[0], to_member_id=root, body="Still reading", kind="progress")
        return {}
    await command(run_id, server, "queued-before-crash", {}, enqueue)
    await scheduler.tick(run_id, actor)
    state = await snapshot(run_id, actor)
    assert sorted(m["state"] for m in state["messages"].values()) == ["delivered", "recorded"]
    assert root not in config[1]
    await scheduler.tick(run_id, actor)
    assert (await snapshot(run_id, actor))["seq"] == state["seq"]
    async with get_db_session() as db:
        items = (await db.execute(select(AgentInboxItem).where(AgentInboxItem.session_id == members[1]))).scalars().all()
    assert len(items) == 1


async def test_natural_worker_answer_gets_one_nudge_then_implicit_review(config):
    from agent.driver import reserve_run
    run_id, actor, server, root, members = await setup_team()
    task = await make_task(run_id, server, members[0])
    await scheduler.tick(run_id, actor)
    async with get_db_session() as db:
        member_session = await db.get(Session, members[0])
    await runtime_binding.load_binding(member_session)
    for index in range(2):
        lease = await reserve_run(members[0], actor.owner_user_id)
        try:
            await lifecycle.turn_started(lease)
            await lifecycle.turn_ended(lease, text="Evidence supports option A.", outcome="succeeded")
        finally:
            await lease.release()
    state = await snapshot(run_id, actor)
    assert state["tasks"][task["id"]]["state"] == "review"
    assert next(iter(state["attempts"].values()))["implicit"] is True


@pytest.mark.parametrize("status", ["pending", "answered", "rejected"])
async def test_coordinator_question_wait_never_nudges_or_pauses_the_team(config, status):
    from agent.driver import reserve_run
    from db.models.question import QuestionCheckpoint
    run_id, actor, server, root, _ = await setup_team()
    state = await snapshot(run_id, actor)
    token = runtime_binding._current.set(runtime_binding.RuntimeBinding(run_id, root,
        actor.owner_user_id, actor.workspace_id, state["run"]["project_id"], "coordinator", {}, {}))
    async with get_db_session() as db:
        db.add(QuestionCheckpoint(id="question-" + run_id, session_id=root, user_id=actor.owner_user_id,
            generation=1, status=status, questions=[], draft=[], continuation={}, applied=False,
            created_at=utcnow(), updated_at=utcnow()))
    async def nudge(writer):
        commands.member_status(writer, root, nudged=True)
        return {}
    await command(run_id, server, "earlier-nudge", {}, nudge)
    lease = await reserve_run(root, actor.owner_user_id)
    try:
        await lifecycle.turn_started(lease)
        await lifecycle.turn_ended(lease, text="", outcome="succeeded")
    finally:
        await lease.release()
        runtime_binding._current.reset(token)
    state = await snapshot(run_id, actor)
    assert state["run"]["state"] == "running"
    assert state["members"][root]["execution_state"] == "waiting"
    assert state["members"][root]["nudged"] is False
    async with get_db_session() as db:
        assert not await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id == root))


async def test_pause_retains_answer_delivery_and_resume_repairs_an_interrupted_continuation(config, monkeypatch):
    from db.models.question import QuestionCheckpoint, SessionExecution
    from question import continuation
    from team.errors import TeamError
    from team.service import control
    monkeypatch.setattr(scheduler, "schedule", lambda *_: None)
    run_id, actor, server, root, _ = await setup_team()
    async with get_db_session() as db:
        db.add(SessionExecution(session_id=root, user_id=actor.owner_user_id, generation=1,
            resume_pending=True, updated_at=utcnow()))
        db.add(QuestionCheckpoint(id="question-" + run_id, session_id=root, user_id=actor.owner_user_id,
            generation=1, status="answered", answers=[["应用调整"]], questions=[], draft=[],
            continuation={"kind": "team_lineup", "mode": "amend", "run_id": run_id}, applied=False,
            created_at=utcnow(), updated_at=utcnow()))
    async def paused(*_):
        raise TeamError("TEAM_PAUSED", "Resume the paused run first")
    monkeypatch.setattr(continuation, "apply_answers", paused)
    await continuation.QuestionContinuationWorker()._resume_candidate(root, actor.owner_user_id, 1)
    async with get_db_session() as db:
        execution = await db.get(SessionExecution, root)
        assert execution.resume_pending and execution.resume_error is None
        assert execution.next_attempt_at is not None
        # A process running the older code may already have marked it failed.
        execution.resume_pending, execution.resume_error = False, "Team paused"
    async def pause(writer):
        commands.run_status(writer, "pausing", pause_reason="user")
        commands.run_status(writer, "paused")
        return {}
    await command(run_id, server, "paused", {}, pause)
    state = await snapshot(run_id, actor)
    await control(run_id, actor, "resume", "resume", state["run"]["revision"])
    async with get_db_session() as db:
        execution = await db.get(SessionExecution, root)
        assert execution.resume_pending and execution.resume_error is None
        assert execution.next_attempt_at is None
        checkpoint = await db.get(QuestionCheckpoint, "question-" + run_id)
        assert checkpoint.answers == [["应用调整"]] and checkpoint.applied is False
