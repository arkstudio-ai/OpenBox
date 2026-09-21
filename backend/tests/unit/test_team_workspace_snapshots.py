"""Workspace boundaries remain fenced, recoverable and visible to the owner."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from db.base import get_db_session
from db.models.team import TeamRun
from team import commands, scheduler, workspace_snapshots as files
from team.errors import TeamError
from team.journal import command, snapshot, utcnow
from tests.unit.test_team_commands import make_task, setup_team
from tests.unit.test_team_runtime import config


async def prepared():
    run, actor, server, root, members = await setup_team()
    async def pending(writer):
        commands.run_status(writer, writer.state["run"]["state"], workspace_snapshots={
            "start": {"status": "pending"}, "end": {"status": "pending"}})
        return {}
    await command(run, server, "snapshots-enabled", {}, pending)
    return run, actor, server, root, members


async def test_concurrent_capture_holds_no_journal_lock_and_persists_only_one_boundary(monkeypatch):
    run, actor, server, root, _ = await prepared()
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def capture(root_id, user_id, sandbox=None):
        calls.append((root_id, user_id))
        started.set()
        await release.wait()
        return "a" * 40
    monkeypatch.setattr(files, "capture", capture)
    job = asyncio.create_task(files.ensure(run, actor, "start"))
    await started.wait()
    try:
        assert not await files.ensure(run, actor, "start")
        async def notice(writer):
            writer.append("team.notice", "notice", {"id": "during-capture", "code": "FIXTURE"})
            return {}
        await asyncio.wait_for(command(run, server, "notice-during-capture", {}, notice), 1)
    finally:
        release.set()
        assert await job
    assert await files.ensure(run, actor, "start")
    assert calls == [(root, actor.owner_user_id)]
    async with get_db_session() as db:
        assert (await db.get(TeamRun, run)).start_snapshot == "a" * 40
    assert await snapshot(run, actor) == await snapshot(run, actor, rebuild=True)


async def test_expired_claim_recovers_and_late_worker_cannot_replace_new_snapshot(monkeypatch):
    run, actor, server, _, _ = await prepared()
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def capture(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return "a" * 40
        return "b" * 40
    monkeypatch.setattr(files, "capture", capture)
    first = asyncio.create_task(files.ensure(run, actor, "start"))
    await started.wait()
    async def expire(writer):
        records = dict(writer.state["run"]["workspace_snapshots"])
        records["start"] = {**records["start"], "lease_until": (utcnow()-timedelta(seconds=1)).isoformat()}
        commands.run_status(writer, writer.state["run"]["state"], workspace_snapshots=records)
        return {}
    try:
        await command(run, server, "expire-capture", {}, expire)
        assert await files.ensure(run, actor, "start")
    finally:
        release.set()
        assert await first
    assert (await snapshot(run, actor))["run"]["workspace_snapshots"]["start"]["hash"] == "b" * 40


async def test_closing_retains_project_until_end_capture_and_uses_stored_range(config, monkeypatch):
    run, actor, server, root, _ = await prepared()
    async def start(*args):
        return "a" * 40
    monkeypatch.setattr(files, "capture", start)
    await files.ensure(run, actor, "start")
    async def cancel(writer):
        commands.run_status(writer, "canceling")
        return {}
    await command(run, server, "cancel", {}, cancel)
    entered, release = asyncio.Event(), asyncio.Event()
    async def end(*args):
        entered.set()
        await release.wait()
        return "b" * 40
    monkeypatch.setattr(files, "capture", end)
    closing = asyncio.create_task(scheduler.tick(run, actor))
    await entered.wait()
    try:
        async with get_db_session() as db:
            row = await db.get(TeamRun, run)
            assert row.state == "canceling" and row.project_active == 1
    finally:
        release.set()
        await closing
    state = await snapshot(run, actor)
    assert state["run"]["state"] == "canceled"
    async with get_db_session() as db:
        row = await db.get(TeamRun, run)
        assert row.project_active is None and (row.start_snapshot, row.end_snapshot) == ("a"*40, "b"*40)
    from snapshot import snapshot as snapshots
    calls = []
    async def diff(before, after, **kwargs):
        calls.append((before, after, kwargs))
        return [{"path": "answer.txt", "additions": 1, "deletions": 0}]
    monkeypatch.setattr(snapshots, "diff_full", diff)
    assert (await files.diff(run, actor, full=True))[0]["path"] == "answer.txt"
    assert calls == [("a"*40, "b"*40, {"session_id": root, "user_id": actor.owner_user_id})]
    with pytest.raises(TeamError) as error:
        await files.diff(run, replace(actor, workspace_id="another-workspace"), full=True)
    assert error.value.status == 404


async def test_failed_capture_is_visible_and_never_reported_as_no_file_changes(monkeypatch):
    run, actor, _, _, _ = await prepared()
    async def broken(*args):
        raise OSError("sandbox unavailable")
    monkeypatch.setattr(files, "capture", broken)
    assert await files.ensure(run, actor, "start")
    assert await files.ensure(run, actor, "start")
    state = await snapshot(run, actor)
    assert state["run"]["workspace_snapshots"]["start"]["status"] == "unavailable"
    assert sum(n["code"] == "WORKSPACE_SNAPSHOT_UNAVAILABLE" for n in state["notices"]) == 1
    with pytest.raises(TeamError) as error:
        await files.diff(run, actor)
    assert error.value.status == 409


async def test_existing_run_without_before_image_is_not_retroactively_snapshotted(monkeypatch):
    run, actor, _, _, _ = await setup_team()
    async def unexpected(*args):
        raise AssertionError("Old runs cannot gain a fictitious before-image")
    monkeypatch.setattr(files, "capture", unexpected)
    assert await files.ensure(run, actor, "start")
    with pytest.raises(TeamError, match="Both workspace snapshots"):
        await files.diff(run, actor)


@pytest.mark.parametrize("closing,terminal", [("completing", "completed"), ("canceling", "canceled")])
async def test_scheduled_closing_does_not_inherit_released_model_authority(config, monkeypatch, closing, terminal):
    from agent import driver
    from question import runtime as questions
    from team import runtime_binding

    run, actor, server, root, members = await prepared()
    task = await make_task(run, server, members[0], deliverable=True)
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    async def update(writer, action):
        current = writer.state["tasks"][task["id"]]
        return await commands.update_task(writer, task["id"], current["revision"], action, summary="Verified result")
    await command(run, server, "submit", {}, lambda writer: update(writer, "submit"))
    await command(run, server, "accept", {}, lambda writer: update(writer, "accept"))
    async def close(writer):
        commands.run_status(writer, closing, final_summary="Verified result")
        return {}
    await command(run, server, "closing-after-model", {}, close)
    observed = []
    async def capture(root_id, owner_id, sandbox=None):
        # These are the same execution-plane guards that refused the real
        # end snapshot after a coordinator's Driver had released its lease.
        await questions.assert_current("tool")
        assert driver.current_run_transport_lease() is None
        assert runtime_binding.current_binding() is None
        observed.append((root_id, owner_id))
        return "c" * 40
    monkeypatch.setattr(files, "capture", capture)
    lease = SimpleNamespace(_closed=True, session_id=root, run_id="released", generation=1)
    lease_token = driver.bind_current_lease(lease)
    ticket = questions.RunTicket(root, actor.owner_user_id, 1, "released-question-run")
    question_token = questions.current_run.set(ticket)
    binding_token = runtime_binding._current.set(object())
    try:
        scheduler.schedule(run, actor)
        await asyncio.wait_for(scheduler._scheduled[run], 5)
        assert questions.current_run.get() == ticket
        assert driver._current_lease.get() is lease
    finally:
        runtime_binding._current.reset(binding_token)
        questions.current_run.reset(question_token)
        driver.reset_current_lease(lease_token)
    assert observed == [(root, actor.owner_user_id)]
    async with get_db_session() as db:
        row = await db.get(TeamRun, run)
        assert row.state == terminal and row.end_snapshot == "c" * 40
