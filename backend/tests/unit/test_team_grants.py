from dataclasses import replace

import pytest

from team import commands
from team.errors import TeamError
from team.grants import GrantChange, update
from team.journal import command, snapshot
from tests.unit.test_team_commands import setup_team


async def fixture():
    run, actor, server, root, members = await setup_team()
    async def pause(writer):
        writer.append("team.grant", "grant", {**writer.state["grant"], "id": run, "version": 2,
            "delegable_tools": ["read", "write", "image_gen"], "paid_tools": {}, "permission_rules": []})
        commands.run_status(writer, "pausing", pause_reason="budget_exceeded")
        commands.run_status(writer, "paused")
        return {}
    await command(run, server, "pause", {}, pause)
    return run, actor, server


async def test_paused_owner_can_update_limits_and_exact_scope_without_resuming():
    run, actor, _ = await fixture()
    before = await snapshot(run, actor)
    change = GrantChange(expected_revision=before["run"]["revision"], max_coordinator_turns=80, max_wall_time_seconds=8000,
        permission_rules=[{"permission": "edit", "pattern": "approved/output.txt", "action": "allow"}])
    first = await update(run, actor, "owner-grant", change)
    assert await update(run, actor, "owner-grant", change) == first
    after = await snapshot(run, actor)
    assert after["run"]["state"] == "paused"
    assert after["run"]["revision"] == before["run"]["revision"] + 1
    assert after["grant"]["version"] == 3
    assert after["grant"]["permission_rules"] == change.model_dump(mode="json")["permission_rules"]
    assert after["policy"]["max_coordinator_turns"] == 80
    assert after == await snapshot(run, actor, rebuild=True)
    with pytest.raises(TeamError, match="revision"):
        await update(run, actor, "stale-dialog", change)


async def test_grant_cannot_cross_owner_or_add_tools():
    run, actor, server = await fixture()
    state = await snapshot(run, actor)
    revision = state["run"]["revision"]
    with pytest.raises(TeamError) as denied:
        await update(run, replace(actor, owner_user_id="someone-else"), "foreign", GrantChange(expected_revision=revision, max_coordinator_turns=80))
    assert denied.value.status == 404
    with pytest.raises(TeamError) as denied:
        await update(run, server, "server", GrantChange(expected_revision=revision, max_coordinator_turns=80))
    assert denied.value.status == 403
    for changes in ({"permission_rules": [{"permission": "bash", "pattern": "*", "action": "allow"}]},
                    {"paid_tools": {"video_generate": {"per_call": "1", "total": "2"}}}):
        with pytest.raises(TeamError):
            await update(run, actor, "expand", GrantChange(expected_revision=revision, **changes))
    assert await snapshot(run, actor) == state


async def test_revoking_grant_never_releases_inflight_paid_work():
    run, actor, server = await fixture()
    async def reserve(writer):
        writer.append("team.grant", "grant", {**writer.state["grant"], "id": run, "version": 3,
            "paid_tools": {"image_gen": {"per_call": "6", "total": "10"}}})
        writer.append("team.budget.reserved", "reservation", {"id": "unknown-call", "tool": "image_gen", "amount": "6"})
        return {}
    await command(run, server, "unknown", {}, reserve)
    state = await snapshot(run, actor)
    await update(run, actor, "revoke-future", GrantChange(expected_revision=state["run"]["revision"], paid_tools={}, permission_rules=[]))
    after = await snapshot(run, actor)
    assert after["grant"]["paid_tools"] == {}
    assert after["reservations"] == state["reservations"]
    assert after["run"]["state"] == "paused"
