"""The model-facing submit contract reaches the owned artifact projection."""
import json
import hashlib

import pytest

from db.base import get_db_session
from db.models.file_asset import FileAsset
from team import projection, scheduler
from team.journal import snapshot, utcnow
from tests.unit.test_team_paid_tools import paid
from tool.team_tools import TaskUpdate, task_update


@pytest.fixture
def artifact_store(monkeypatch):
    from core import oss
    class Store:
        def __init__(self):
            self.objects = {"private/report.txt": b"Report bytes"}
            self.reads = []

        async def get_object_chunks(self, key):
            self.reads.append(key)
            yield self.objects[key][:5]
            yield self.objects[key][5:]

        async def put_object_file(self, key, path, **kwargs):
            assert kwargs["forbid_overwrite"] is True
            self.objects.setdefault(key, path.read_bytes())
    store = Store()
    monkeypatch.setattr(oss, "get_oss", lambda: store)
    return store


@pytest.mark.parametrize("foreign", [False, True])
async def test_member_tool_registers_owned_assets_and_rejects_foreign_assets_atomically(paid, artifact_store, monkeypatch, foreign):
    monkeypatch.setattr(scheduler, "schedule", lambda *_: None)
    asset_id = f"asset-{paid.run}"
    async with get_db_session() as db:
        db.add(FileAsset(id=asset_id, user_id=paid.actor.owner_user_id, workspace_id=paid.actor.workspace_id,
            project_id="another-project" if foreign else paid.ctx.project_id, session_id=paid.ctx.session_id,
            name="report.txt", mime="text/plain", size=12, source="agent", status="ready",
            oss_key="private/report.txt", created_at=utcnow()))
    before = await snapshot(paid.run, paid.actor)
    args = TaskUpdate.model_validate({"task_id": paid.attempt["task_id"], "action": "submit", "summary": "Report ready",
        "artifacts": [{"file_asset_id": asset_id, "name": "Report", "summary": "Validated calculation"}]})
    result = await task_update(args, paid.ctx)
    if foreign:
        assert result.metadata["code"] == "INVALID_ARTIFACT"
        assert await snapshot(paid.run, paid.actor) == before
        assert artifact_store.reads == []
        return
    assert result.metadata["turn_yield"] is True
    returned = json.loads(result.output)
    task = returned["task"]
    assert task["state"] == "succeeded"
    page = await projection.collection(paid.run, paid.actor, "artifacts")
    assert page["total"] == 1
    item = page["items"][0]
    assert returned["artifact_ids"] == [item["id"]]
    assert item["asset"]["id"] != asset_id
    assert item["source_file_asset_id"] == asset_id
    assert item["content_digest"] == hashlib.sha256(b"Report bytes").hexdigest()
    assert page["items"][0]["summary"] == "Validated calculation"
    assert "private/report.txt" not in str(page)
    from tool.team_tools import TeamView, view
    paid.ctx.part_id = "read-registered-artifact"
    observed = json.loads((await view(TeamView(), paid.ctx)).output)
    assert observed["artifacts"][0]["id"] == item["id"]
    paid.ctx.part_id = "read-artifact-page"
    listed = json.loads((await view(TeamView(section="artifacts", task_id=task["id"]), paid.ctx)).output)
    assert listed["items"][0]["id"] == item["id"]
    assert listed["total"] == 1 and listed["next_offset"] is None
    # An upload URL can still overwrite the source, never the accepted bytes.
    artifact_store.objects["private/report.txt"] = b"tampered"
    async with get_db_session() as db:
        frozen = await db.get(FileAsset, item["file_asset_id"])
        assert artifact_store.objects[frozen.oss_key] == b"Report bytes"


@pytest.mark.parametrize("size,claimed", [(12, "a" * 64), (5, None), (15, None)])
async def test_false_digest_or_changed_size_never_commits_an_artifact(paid, artifact_store, size, claimed):
    from team.artifacts import prepare
    from team.errors import TeamError
    asset_id = f"asset-{paid.run}"
    async with get_db_session() as db:
        db.add(FileAsset(id=asset_id, user_id=paid.actor.owner_user_id, workspace_id=paid.actor.workspace_id,
            project_id=paid.ctx.project_id, session_id=paid.ctx.session_id, name="report.txt", mime="text/plain",
            size=size, source="agent", status="ready", oss_key="private/report.txt", created_at=utcnow()))
    with pytest.raises(TeamError, match="bytes"):
        await prepare(paid.run, paid.actor, [{"file_asset_id": asset_id, "content_digest": claimed, "name": "Report"}])
    assert list(artifact_store.objects) == ["private/report.txt"]
    assert not (await snapshot(paid.run, paid.actor))["artifacts"]
