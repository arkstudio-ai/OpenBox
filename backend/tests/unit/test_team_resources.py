"""A desktop workflow owns its workspace until its outcome is known."""
import asyncio

from team import commands
from team.journal import command
from tests.unit.test_team_commands import make_task, setup_team


async def test_workspace_desktop_lock_spans_projects_and_waiting_members():
    first, actor, server, _, members = await setup_team()
    second, _, other_server, _, others = await setup_team(scope=actor)
    third, _, foreign_server, _, foreign = await setup_team()
    tasks = [await make_task(first, server, members[0], exclusive_group="desktop"),
        await make_task(second, other_server, others[0], exclusive_group="desktop")]
    await make_task(third, foreign_server, foreign[0], exclusive_group="desktop")
    results = await asyncio.gather(command(first, server, "dispatch", {}, commands.dispatch_ready),
        command(second, other_server, "dispatch", {}, commands.dispatch_ready))
    assert sum(len(row["dispatched"]) for row in results) == 1
    winner = 0 if results[0]["dispatched"] else 1
    run, leader, worker = [(first, server, members[0]), (second, other_server, others[0])][winner]
    waiting_run, waiting_actor = [(first, server), (second, other_server)][1 - winner]
    async def wait(writer):
        commands.member_status(writer, worker, execution_state="waiting")
        return {"waiting": True}
    await command(run, leader, "wait", {}, wait)
    assert not (await command(waiting_run, waiting_actor, "still-busy", {}, commands.dispatch_ready))["dispatched"]
    assert len((await command(third, foreign_server, "own-workspace", {}, commands.dispatch_ready))["dispatched"]) == 1
    async def submit(writer):
        task = writer.state["tasks"][tasks[winner]["id"]]
        return await commands.update_task(writer, task["id"], task["revision"], "submit", summary="Desktop workflow complete.")
    await command(run, leader, "submit", {}, submit)
    assert len((await command(waiting_run, waiting_actor, "released", {}, commands.dispatch_ready))["dispatched"]) == 1


async def test_unknown_desktop_effect_holds_workspace_until_reconciled():
    first, actor, server, _, members = await setup_team()
    second, _, other_server, _, others = await setup_team(scope=actor)
    task = await make_task(first, server, members[0], exclusive_group="desktop")
    await make_task(second, other_server, others[0], exclusive_group="desktop")
    await command(first, server, "dispatch", {}, commands.dispatch_ready)
    async def unknown(writer):
        running = writer.state["tasks"][task["id"]]
        attempt = writer.state["attempts"][running["current_attempt"]]
        writer.append("team.attempt", "attempt", {**attempt, "state": "outcome_unknown"})
        commands.task_status(writer, running, state="outcome_unknown")
        return {"unknown": True}
    await command(first, server, "unknown", {}, unknown)
    assert not (await command(second, other_server, "blocked", {}, commands.dispatch_ready))["dispatched"]
