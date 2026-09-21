"""Owner supplements preserve ordinary-user provenance and the team fence."""
from dataclasses import replace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.part import Part
from team import commands, scheduler
from team.errors import TeamError
from team.journal import command, snapshot
from team.owner_messages import OwnerMessage, send
from tests.unit.test_team_commands import setup_team


@pytest.fixture(autouse=True)
def no_background_execution(monkeypatch):
    monkeypatch.setattr(scheduler, "schedule", lambda *args: None)


async def test_owner_supplement_is_idempotent_real_user_input_only_to_root():
    run, actor, _, root, members = await setup_team()
    body = OwnerMessage(text="Use the corrected quantity: 7.")
    result = await send(run, actor, "quantity", body)
    state = await snapshot(run, actor)
    assert await send(run, actor, "quantity", body) == result
    assert await snapshot(run, actor, rebuild=True) == state
    async with get_db_session() as db:
        rows = list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id.in_([root, *members])))).all())
        assert len(rows) == 1
        assert rows[0].id == result["id"] and rows[0].session_id == root
        assert rows[0].source_type is None and rows[0].prompt == body.text
    lease = await reserve_run(root, user_id=actor.owner_user_id)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        assert len(batch.messages) == 1 and batch.messages[0].role == "user"
        async with get_db_session() as db:
            text = (await db.scalars(select(Part).where(Part.message_id == batch.messages[0].id, Part.type == "text"))).one()
            assert text.data.get("synthetic") is not True
            assert not text.data.get("team_source")
    finally:
        await lease.release()
    with pytest.raises(TeamError) as conflict:
        await send(run, actor, "quantity", OwnerMessage(text="A different correction."))
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


async def test_paused_supplements_remain_queued_without_resuming_or_granting_authority():
    run, actor, server, root, _ = await setup_team()
    async def pause(writer):
        commands.run_status(writer, "pausing")
        commands.run_status(writer, "paused")
        return {}
    await command(run, server, "pause", {}, pause)
    before = await snapshot(run, actor)
    result = await send(run, actor, "pending", OwnerMessage(text="Here is more context; await my explicit resume."))
    after = await snapshot(run, actor)
    assert after["run"]["state"] == "paused" and after["grant"] == before["grant"]
    assert after["run"]["revision"] == before["run"]["revision"]
    assert (await inbox.get_inbox_item(result["id"], user_id=actor.owner_user_id)).state == "accepted"
    assert not await scheduler.can_wake(root, actor.owner_user_id)


async def test_foreign_scope_member_impersonation_and_unknown_attachments_cannot_write():
    run, actor, server, root, _ = await setup_team()
    before = await snapshot(run, actor)
    for invalid, status in [(replace(actor, owner_user_id="other"), 404),
                            (replace(actor, workspace_id="other"), 404), (server, 403),
                            (replace(actor, kind="member", member_id=root), 403)]:
        with pytest.raises(TeamError) as denied:
            await send(run, invalid, "foreign", OwnerMessage(text="Impersonation"))
        assert denied.value.status == status
    with pytest.raises(TeamError) as attachment:
        await send(run, actor, "missing-asset", OwnerMessage(text="Read it", attachments=["not-owned"]))
    assert attachment.value.code == "INVALID_OWNER_MESSAGE"
    with pytest.raises(ValidationError):
        OwnerMessage.model_validate({"text": "Wrong target", "to_member_id": "worker", "source_type": "team_control"})
    assert await snapshot(run, actor) == before
    async with get_db_session() as db:
        assert not (await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == root))).all()


async def test_journal_failure_rolls_back_input_and_closed_run_only_replays_receipt(monkeypatch):
    from team.journal import Writer
    run, actor, server, root, _ = await setup_team()
    original = Writer.finish
    def fail(self, result):
        raise RuntimeError("injected crash before commit")
    monkeypatch.setattr(Writer, "finish", fail)
    with pytest.raises(RuntimeError):
        await send(run, actor, "lost-response", OwnerMessage(text="Review the correction."))
    async with get_db_session() as db:
        assert not (await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == root))).all()
    monkeypatch.setattr(Writer, "finish", original)
    body = OwnerMessage(text="Review the correction.")
    result = await send(run, actor, "lost-response", body)
    async def close(writer):
        commands.run_status(writer, "canceling")
        commands.run_status(writer, "canceled")
        return {}
    await command(run, server, "close", {}, close)
    assert await send(run, actor, "lost-response", body) == result
    with pytest.raises(TeamError) as closed:
        await send(run, actor, "late", body)
    assert closed.value.code == "TEAM_CLOSED"


async def test_owner_message_http_requires_key_and_rejects_extra_recipient():
    import httpx
    from fastapi import FastAPI
    from api.agent_teams import router
    from auth.middleware import get_current_user
    from auth.workspace import get_workspace
    run, actor, _, root, _ = await setup_team()
    app = FastAPI()
    app.include_router(router)
    user = {"user_id": actor.owner_user_id, "workspace_id": actor.workspace_id}
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_workspace] = lambda: user
    path = f"/api/team-runs/{run}/messages"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post(path, json={"text": "Supplement"})).status_code == 422
        rejected = await client.post(path, headers={"Idempotency-Key": "http-message"}, json={"text": "Supplement", "to_member_id": "other"})
        assert rejected.status_code == 422
        response = await client.post(path, headers={"Idempotency-Key": "http-message"}, json={"text": "Supplement"})
        assert response.status_code == 202 and response.json()["session_id"] == root
        replay = await client.post(path, headers={"Idempotency-Key": "http-message"}, json={"text": "Supplement"})
        assert replay.json() == response.json()
