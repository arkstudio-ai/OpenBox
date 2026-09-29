"""Ask assets survive drafts and enter the ordinary, tenant-scoped inbox."""
import pytest
from sqlalchemy import select

import db.base as database
from db.models.agent_inbox import AgentInboxItem
from db.models.file_asset import FileAsset
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from question import question as q, runtime
from question.continuation import apply_answers
from tests.unit.test_durable_questions import checkpoint, read, state  # noqa: F401


async def asset(asset_id="asset-a", **changes):
    values = dict(id=asset_id, user_id="u1", workspace_id="w1", name="portrait.png",
                  mime="image/png", size=100, oss_key=f"assets/u1/{asset_id}/portrait.png",
                  status="ready", is_deleted=False, source="user", created_at=runtime.now())
    values.update(changes)
    async with database.get_db_session() as db:
        db.add(FileAsset(**values))


async def pending(**kwargs):
    return await checkpoint(questions=[q.Question(question="Provide a presenter photo", allow_attachments=True)], **kwargs)


async def inbox_rows():
    async with database.get_db_session() as db:
        return list((await db.scalars(select(AgentInboxItem))).all())


async def test_file_only_answer_survives_draft_and_reaches_model_file_parts_once(state):
    from agent.driver import reserve_run
    from agent.inbox import claim_inbox_boundary

    await asset()
    request_id = await pending()
    saved = await q.save_draft(request_id, [q.DraftAnswer(attachments=["asset-a"])], 0, "u1")
    assert saved.draft[0].attachments == ["asset-a"]
    assert (await q.get_request(request_id, "u1")).draft[0].attachments == ["asset-a"]
    assert await inbox_rows() == []  # A saved draft does not submit anything.

    await q.reply(request_id, [[]], "u1", attachments=[["asset-a"]])
    await q.reply(request_id, [[]], "u1", attachments=[["asset-a"]])
    assert await inbox_rows() == []  # Applied only when the continuation owns the session.
    await apply_answers("s1", "u1")
    await apply_answers("s1", "u1")
    rows = await inbox_rows()
    assert len(rows) == 1
    assert rows[0].attachments == ["asset-a"]
    assert rows[0].target == "next-step"
    assert rows[0].prompt == "Provide a presenter photo\n📎 portrait.png"
    tool = await read(Part, "p1")
    assert tool.data["metadata"]["attachments"][0][0]["asset_id"] == "asset-a"

    lease = await reserve_run("s1", "u1")
    try:
        batch = await claim_inbox_boundary(lease, step=1, include_next_turn=False)
        assert batch.attachment_ids == ("asset-a",)
        async with database.get_db_session() as db:
            parts = list((await db.scalars(select(Part).where(Part.type == "file"))).all())
        assert len(parts) == 1
        assert parts[0].data["asset_id"] == "asset-a"
        assert parts[0].data["oss_key"] == "assets/u1/asset-a/portrait.png"
        assert parts[0].data["relation"]["role"] == "input"
    finally:
        await lease.release(session_status="idle")


async def test_http_resource_question_restores_draft_and_submits_file_only_answer(state):
    import httpx
    from tests.unit.test_durable_question_failures import application

    await asset()
    request_id = await pending()
    path = f"/api/agent/question/{request_id}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="http://test") as client:
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json()["questions"][0]["allow_attachments"] is True
        response = await client.put(path + "/draft", json={
            "draft": [{"attachments": ["asset-a"]}], "revision": 0,
        })
        assert response.status_code == 200
        restored = await client.get(path)
        assert restored.json()["draft"][0]["attachments"] == ["asset-a"]
        response = await client.post(path, json={"answers": [[]], "attachments": [["asset-a"]]})
        assert response.status_code == 200
        assert response.json()["status"] == "answered"
    await apply_answers("s1", "u1")
    assert (await inbox_rows())[0].attachments == ["asset-a"]


@pytest.mark.parametrize("changes", [
    {"user_id": "u2"}, {"status": "pending"}, {"is_deleted": True},
])
async def test_unavailable_or_foreign_assets_do_not_resolve_or_save_question(state, changes):
    await asset(**changes)
    request_id = await pending()
    with pytest.raises(ValueError, match="unavailable"):
        await q.reply(request_id, [[]], "u1", attachments=[["asset-a"]])
    with pytest.raises(ValueError, match="unavailable"):
        await q.save_draft(request_id, [q.DraftAnswer(attachments=["asset-a"])], 0, "u1")
    row = await read(QuestionCheckpoint, request_id)
    assert row.status == "pending" and row.draft_revision == 0


async def test_same_owner_assets_cannot_cross_workspace(state):
    from db.models.workspace import Workspace
    async with database.get_db_session() as db:
        db.add(Workspace(id="w2", name="Other", owner_user_id="u1", created_at=runtime.now(), updated_at=runtime.now()))
    await asset(workspace_id="w2")
    request_id = await pending()
    with pytest.raises(ValueError, match="unavailable"):
        await q.reply(request_id, [[]], "u1", attachments=[["asset-a"]])


async def test_answer_idempotency_includes_assets_and_question_mapping(state):
    await asset()
    await asset("asset-b")
    request_id = await pending()
    await q.reply(request_id, [["Use this"]], "u1", attachments=[["asset-a"]])
    for changed in ([["asset-b"]], [[]]):
        with pytest.raises(q.QuestionConflict):
            await q.reply(request_id, [["Use this"]], "u1", attachments=changed)


async def test_only_opted_in_questions_accept_files_and_require_all_other_answers(state):
    await asset()
    request_id = await checkpoint(questions=[q.Question(question="Choose a picture", allow_attachments=True),
                                             q.Question(question="Choose a duration")])
    with pytest.raises(ValueError, match="Answer every"):
        await q.reply(request_id, [[], []], "u1", attachments=[["asset-a"], []])
    with pytest.raises(ValueError, match="does not accept"):
        await q.reply(request_id, [["yes"], ["30s"]], "u1", attachments=[[], ["asset-a"]])
    await q.reply(request_id, [[], ["30s"]], "u1", attachments=[["asset-a"], []])
    await apply_answers("s1", "u1")
    tool = await read(Part, "p1")
    assert tool.data["metadata"]["answers"] == [[], ["30s"]]
    assert tool.data["metadata"]["attachments"][1] == []


@pytest.mark.parametrize("ids", [["asset-a", "asset-a"], [""], ["x" * 65], [f"a-{i}" for i in range(33)]])
async def test_invalid_asset_lists_are_rejected_before_acceptance(state, ids):
    request_id = await pending()
    with pytest.raises(ValueError):
        await q.reply(request_id, [[]], "u1", attachments=[ids])
    assert (await read(QuestionCheckpoint, request_id)).status == "pending"


async def test_skipped_question_does_not_send_saved_files(state):
    await asset()
    request_id = await pending()
    await q.save_draft(request_id, [q.DraftAnswer(attachments=["asset-a"])], 0, "u1")
    await q.reject(request_id, "u1")
    await apply_answers("s1", "u1")
    assert await inbox_rows() == []


async def test_answer_and_asset_delivery_intent_roll_back_together(state, monkeypatch):
    from agent import inbox
    await asset()
    request_id = await pending()
    await q.reply(request_id, [[]], "u1", attachments=[["asset-a"]])
    original = inbox.accept_inbox_item_locked

    async def fail_after_enqueue(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("simulated worker loss")

    with monkeypatch.context() as scoped:
        scoped.setattr(inbox, "accept_inbox_item_locked", fail_after_enqueue)
        with pytest.raises(RuntimeError, match="worker loss"):
            await apply_answers("s1", "u1")
    assert await inbox_rows() == []
    assert not (await read(QuestionCheckpoint, request_id)).applied
    await apply_answers("s1", "u1")
    assert len(await inbox_rows()) == 1
