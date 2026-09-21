from copy import deepcopy

import pytest

from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.team import TeamRun
from team import commands, projection
from team.errors import TeamError
from team.journal import command, snapshot, utcnow
from tests.unit.test_team_commands import make_task, setup_team


async def test_task_navigation_can_resolve_beyond_first_page_and_filter_attempts():
    run_id, actor, server, _, members = await setup_team()

    async def create_many(writer):
        ids = []
        for number in range(62):
            result = await commands.create_task(writer, {
                "title": f"Task {number}", "description": "Check a source",
                "expected_output": "Evidence", "acceptance_criteria": "Cite source",
                "owner_member_id": members[number % 2], "priority": 100 if number == 61 else 0,
            })
            ids.append(result["task"]["id"])
        return {"ids": ids}

    ids = (await command(run_id, server, "many", {}, create_many))["ids"]
    await command(run_id, server, "dispatch", {}, commands.dispatch_ready)
    first = await projection.collection(run_id, actor, "tasks")
    assert len(first["items"]) == 50 and first["next_offset"] == 50
    assert ids[-1] not in {task["id"] for task in first["items"]}
    target = await projection.collection(run_id, actor, "tasks", task_id=ids[-1])
    assert target["total"] == 1 and target["items"][0]["state"] == "running"
    attempts = await projection.collection(run_id, actor, "attempts", task_id=ids[-1])
    assert attempts["total"] == 1 and attempts["items"][0]["task_id"] == ids[-1]
    assert (await projection.collection(run_id, actor, "attempts", task_id=ids[-2]))["items"] == []
    with pytest.raises(TeamError, match="Task filtering"):
        await projection.collection(run_id, actor, "members", task_id=ids[-1])


async def test_bounded_snapshot_preserves_current_tasks_and_counts_all_completed_tasks():
    run_id, actor, server, _, members = await setup_team()
    original = await make_task(run_id, server, members[0])
    state = await snapshot(run_id, actor)
    # A large snapshot is pure projection input; the journal's task limit is
    # covered separately. Completed tasks outside the window still count.
    state["tasks"] = {f"task-{i}": {**deepcopy(original), "id": f"task-{i}", "state": "succeeded"}
                      for i in range(225)}
    state["tasks"]["last"] = {**original, "id": "last", "state": "running", "current_attempt": "active"}
    state["members"][members[0]]["current_attempt"] = "active"
    public = projection.public_state(state)
    assert len(public["tasks"]) == 200 and public["tasks"][0]["id"] == "last"
    assert public["task_count"] == 226 and public["completed_task_count"] == 225


async def test_artifact_preview_uses_live_owned_asset_without_storage_keys():
    from team.artifacts import snapshot_key
    run_id, actor, server, _, members = await setup_team()
    task = await make_task(run_id, server, members[0])
    await command(run_id, server, "dispatch", {}, commands.dispatch_ready)
    asset_id = f"asset-{run_id}"
    async with get_db_session() as db:
        run = await db.get(TeamRun, run_id)
        db.add(FileAsset(id=asset_id, user_id=actor.owner_user_id, workspace_id=actor.workspace_id,
                         project_id=run.project_id, session_id=members[0], name="report.txt",
                         oss_key=snapshot_key(actor.workspace_id, run_id, "a" * 64), mime="text/plain", size=20,
                         status="ready", created_at=utcnow()))

    async def submit(writer):
        current = writer.state["tasks"][task["id"]]
        return await commands.update_task(writer, current["id"], current["revision"], "submit",
            summary="Report ready", artifacts=[{"file_asset_id": asset_id, "content_digest": "a" * 64, "name": "Report"}])

    await command(run_id, server, "submit", {}, submit)
    page = await projection.collection(run_id, actor, "artifacts", task_id=task["id"])
    assert page["items"][0]["asset"] == {"id": asset_id, "name": "report.txt", "mime": "text/plain", "size": 20}
    assert "private/storage/key" not in str(page)
    async with get_db_session() as db:
        (await db.get(FileAsset, asset_id)).project_id = "outside-this-project"
    assert (await projection.collection(run_id, actor, "artifacts"))["items"][0]["asset"] is None
