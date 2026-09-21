"""Incoming mail must not turn an actively running member into a queued one."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from agent.driver import reserve_run
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from team import commands, lifecycle, runtime_binding, scheduler
from team.journal import command, snapshot, utcnow
from tests.unit.test_team_commands import make_task, setup_team
from tests.unit.test_team_runtime import config


@pytest.mark.parametrize("recipient", ["member", "coordinator"])
@pytest.mark.parametrize("prior_state", ["running", "waiting"])
async def test_mail_preserves_busy_state_and_wakes_waiters_without_duplicate_input(config, recipient, prior_state):
    config[0].team_stall_seconds = 600
    run, actor, server, root, members = await setup_team()
    target = root if recipient == "coordinator" else members[1]
    await make_task(run, server, members[1])
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    state = await snapshot(run, actor)
    binding = runtime_binding.RuntimeBinding(run, target, actor.owner_user_id, actor.workspace_id,
        state["run"]["project_id"], recipient, {}, {})
    token = runtime_binding._current.set(binding)
    lease = await reserve_run(target, actor.owner_user_id)
    try:
        await lifecycle.turn_started(lease)
        if prior_state == "waiting":
            async def wait(writer):
                commands.member_status(writer, target, execution_state="waiting", wait_after_seq=writer.state["seq"],
                    wait_deadline=(utcnow() + timedelta(minutes=5)).isoformat())
                return {}
            await command(run, server, "waiting-before-mail", {}, wait)
        async def send(writer):
            return await commands.queue_message(writer, from_member_id=members[0],
                to_member_id=target, body="Peer evidence arrived during this generation")
        await command(run, server, "peer-mail", {}, send)
        await scheduler.tick(run, actor)
        delivered = await snapshot(run, actor)
        expected = "running" if prior_state == "running" else "queued"
        assert delivered["members"][target]["execution_state"] == expected
        assert delivered["members"][target]["wait_after_seq"] is None
        assert {message["state"] for message in delivered["messages"].values()} == {"delivered"}
        async with get_db_session() as db:
            driver = await db.get(AgentDriverState, target)
            assert driver.run_id == lease.run_id and driver.generation == lease.generation
            items = (await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == target))).all()
            assert sum("Peer evidence arrived" in item.prompt for item in items) == 1
        await scheduler.tick(run, actor)
        assert (await snapshot(run, actor))["seq"] == delivered["seq"]
    finally:
        await lease.release()
        runtime_binding._current.reset(token)
