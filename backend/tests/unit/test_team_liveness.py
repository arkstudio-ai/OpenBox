from datetime import timedelta

from sqlalchemy import select

from agent import driver, inbox
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from team import capacity, commands, lifecycle, liveness, runtime_binding, scheduler
from team.journal import command, snapshot, utcnow
from team.runtime import runtime
from tests.unit.test_team_commands import make_task, setup_team
from tests.unit.test_team_runtime import config


async def observe(run, actor):
    state = await snapshot(run, actor)
    async with get_db_session() as db:
        return state, await runtime.observe(db, state["members"], actor.owner_user_id)


async def test_driver_capacity_preserves_input_and_backs_off_without_blocking_normal_chat(config, monkeypatch):
    config[0].team_max_running_members = 1
    _, first, _, _, members_one = await setup_team()
    run, actor, server, root, members = await setup_team()
    await make_task(run, server, members[0], deliverable=True)
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    occupied = await driver.reserve_run(members_one[0], first.owner_user_id)
    now = utcnow()
    monkeypatch.setattr(capacity, "utcnow", lambda: now)
    try:
        assert await inbox._reserve_and_claim(members[0], actor.owner_user_id) is None
        before = await snapshot(run, actor)
        assert before["run"]["capacity_failures"] == 1
        assert before["run"]["capacity_retry_at"] == (now + timedelta(seconds=10)).isoformat()
        assert not await scheduler.can_wake(members[0], actor.owner_user_id)
        assert await scheduler.can_wake(root, actor.owner_user_id)
        # An active coordinator consumes team capacity; ordinary chat is a
        # separate build session in the same project.
        ordinary_id = "ordinary-" + root
        async with get_db_session() as db:
            row = await db.get(Session, root)
            db.add(Session(id=ordinary_id, user_id=actor.owner_user_id, workspace_id=actor.workspace_id,
                project_id=row.project_id, agent="build", created_at=utcnow(), updated_at=utcnow()))
        ordinary = await driver.reserve_run(ordinary_id, actor.owner_user_id)
        await ordinary.release()
        await scheduler.tick(run, actor)
        assert await snapshot(run, actor) == before
        now += timedelta(seconds=11)
        assert await inbox._reserve_and_claim(members[0], actor.owner_user_id) is None
        assert (await snapshot(run, actor))["run"]["capacity_retry_at"] == (now + timedelta(seconds=20)).isoformat()
    finally:
        await occupied.release()
    now += timedelta(seconds=21)
    claimed, batch = await inbox._reserve_and_claim(members[0], actor.owner_user_id)
    state = await snapshot(run, actor)
    token = runtime_binding._current.set(runtime_binding.RuntimeBinding(run, members[0], actor.owner_user_id,
        actor.workspace_id, state["run"]["project_id"], "member", {}, {}))
    try:
        await lifecycle.turn_started(claimed)
        assert (await snapshot(run, actor))["run"]["capacity_retry_at"] is None
        assert len(batch.receipts) == 1
        assert len((await snapshot(run, actor))["attempts"]) == 1
        await inbox.settle_claimed_inbox_items(claimed, result_message_id=None, outcome="test")
    finally:
        await claimed.release()
        runtime_binding._current.reset(token)


async def test_stall_uses_task_state_progress_and_second_interval_pauses(config, monkeypatch):
    run, actor, server, _, members = await setup_team()
    task = await make_task(run, server, members[0], deliverable=True)
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    lease = await driver.reserve_run(members[0], actor.owner_user_id)
    config[0].team_stall_seconds = 30
    now = utcnow() + timedelta(seconds=31)
    monkeypatch.setattr(liveness, "utcnow", lambda: now)
    try:
        await scheduler.tick(run, actor)
        first = await snapshot(run, actor)
        assert first["run"]["stall_count"] == 1 and first["run"]["state"] == "running"
        await scheduler.tick(run, actor)
        assert (await snapshot(run, actor))["seq"] == first["seq"]
        # A descriptive edit is not a task-state transition or new artifact.
        async def edit(writer):
            item = writer.state["tasks"][task["id"]]
            commands.task_status(writer, item, description="Clarified wording")
            return {}
        await command(run, server, "metadata", {}, edit)
        now += timedelta(seconds=31)
        await scheduler.tick(run, actor)
        state = await snapshot(run, actor)
        assert state["run"]["state"] == "pausing" and state["run"]["pause_reason"] == "stalled"
        assert state["run"]["stall_count"] == 2
    finally:
        await lease.release()


async def test_deadlock_nudge_is_durable_and_user_question_is_not_a_stall(config):
    run, actor, server, root, members = await setup_team()
    task = await make_task(run, server, members[0], deliverable=True)
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    async def block(writer):
        item = writer.state["tasks"][task["id"]]
        return await commands.update_task(writer, item["id"], item["revision"], "block", summary="Need a source")
    await command(run, server, "block", {}, block)
    await scheduler.tick(run, actor)
    async with get_db_session() as db:
        for row in (await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id.in_([root, *members])))).all():
            row.state, row.canceled_at = "canceled", utcnow()
        db.add(QuestionCheckpoint(id="wait-" + run, session_id=root, user_id=actor.owner_user_id,
            generation=1, status="pending", questions=[], draft=[], continuation={}, applied=False,
            created_at=utcnow(), updated_at=utcnow()))
    state, drivers = await observe(run, actor)
    async with get_db_session() as db:
        assert not await liveness.candidates(db, state, drivers)
        (await db.get(QuestionCheckpoint, "wait-" + run)).applied = True
    await scheduler.tick(run, actor)
    state = await snapshot(run, actor)
    assert state["members"][root]["nudged"]
    assert sum(row["code"] == "TEAM_NO_EXECUTABLE_WORK" for row in state["notices"]) == 1
    await scheduler.tick(run, actor)
    assert (await snapshot(run, actor))["seq"] == state["seq"]


async def test_repeated_peer_exchange_and_failed_dependency_notify_once(config):
    run, actor, server, root, members = await setup_team()
    task = await make_task(run, server, members[0])
    dependent = await make_task(run, server, members[1], key="dependent", dependencies=[task["id"]], deliverable=True)
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    async def exchange(writer):
        for index in range(6):
            await commands.queue_message(writer, from_member_id=members[index % 2],
                to_member_id=members[1 - index % 2], body="Discuss the same unresolved issue", task_id=task["id"])
        return {}
    await command(run, server, "exchange", {}, exchange)
    await scheduler.tick(run, actor)
    state = await snapshot(run, actor)
    assert sum(row["code"] == "TEAM_REPEATED_EXCHANGE" for row in state["notices"]) == 1
    await scheduler.tick(run, actor)
    assert (await snapshot(run, actor))["seq"] == state["seq"]
    async def fail(writer):
        item = writer.state["tasks"][task["id"]]
        return await commands.update_task(writer, item["id"], item["revision"], "fail", summary="The required input is unavailable")
    await command(run, server, "fail", {}, fail)
    await scheduler.tick(run, actor)
    state = await snapshot(run, actor)
    notice = next(row for row in state["notices"] if row["code"] == "TEAM_DEPENDENCY_BLOCKED")
    assert notice["tasks"] == [dependent["id"]]
    assert state["tasks"][dependent["id"]]["state"] == "pending"
    assert state == await snapshot(run, actor, rebuild=True)


async def test_other_task_progress_does_not_hide_six_messages_on_unchanged_task(config):
    run, actor, server, _, members = await setup_team()
    task = await make_task(run, server, members[0])
    async def exchange(writer):
        for index in range(6):
            await commands.queue_message(writer, from_member_id=members[index % 2],
                to_member_id=members[1 - index % 2], body="Still discussing", task_id=task["id"])
        return {}
    await command(run, server, "same-task-exchange", {}, exchange)
    await make_task(run, server, members[1], key="unrelated-progress")
    state, drivers = await observe(run, actor)
    async with get_db_session() as db:
        notices = await liveness.candidates(db, state, drivers)
    repeats = [row for row in notices if row["code"] == "TEAM_REPEATED_EXCHANGE"]
    assert len(repeats) == 1 and repeats[0]["task_id"] == task["id"]
