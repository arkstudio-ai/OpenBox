"""A real failed Inbox delivery can be retried in place through the public route."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime, timezone

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from agent import inbox
from agent.driver import reserve_run
from api import sessions as routes
from assistant.linking import candidate, link_existing
from assistant.service import ensure_main_session
from core.identifier import ascending
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.session import Session
from sandbox.assets import AssetDeliveryError
from session.agent_event_log import load_canonical_model_surface, verify_agent_event_parity
from models.message import MessageInfo
from session.session import create_assistant_message, create_session, create_user_message, update_message_info
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def failed_input(monkeypatch, *, linked):
    owner, other, workspace = await accounts()
    session = await create_session(user_id=owner, workspace_id=workspace, model="test/model")
    task = None
    if linked:
        main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
        async with get_db_session() as db:
            version = (await candidate(db, await db.get(Session, session.id)))["version"]
        task = await link_existing(user_id=owner, workspace_id=workspace, main_id=main.id,
            session_id=session.id, expected_version=version, idempotency_key="watch-original")
    asset = ascending("asset")
    async with get_db_session() as db:
        db.add(FileAsset(id=asset, user_id=owner, workspace_id=workspace, project_id=session.project_id,
            name="店内照片.jpg", mime="image/jpeg", oss_key=f"fixture/{asset}", size=5,
            status="ready", source="user", transient=False, is_deleted=False, created_at=datetime.now(timezone.utc)))
    body = routes.PromptBody(text="这张就是店里的照片。时长、字幕和音乐都不要改。", attachments=[asset],
        delivery="followup", client_message_id="original-photo")
    receipt = (await routes._accept_managed_prompt(session, body, owner) if linked else
        await inbox.accept_inbox_item(session_id=session.id, user_id=owner, delivery="followup",
            prompt=body.text, attachments=[asset], origin="human", origin_ref={"actor_user_id": owner},
            client_id="original-photo"))
    lease = await reserve_run(session.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    async def unavailable(*args, **kwargs):
        raise AssetDeliveryError(expected_asset_ids=[asset], missing_asset_ids=[],
            code="sandbox_not_ready", retryable=True)
    monkeypatch.setattr("sandbox.assets.deliver_asset_ids", unavailable)
    for _ in range(2):
        with pytest.raises(inbox.InboxAttachmentDeliveryPending):
            await inbox._deliver_claimed_batch(lease, batch)
    failed = await inbox._deliver_claimed_batch(lease, batch)
    await lease.release(session_status="error")
    monkeypatch.setattr(routes, "_require_session_owned", AsyncMock(return_value=session))
    monkeypatch.setattr(routes, "check_concurrent_agents", AsyncMock())
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lambda *args: None)
    monkeypatch.setattr(inbox, "wake_inbox_session", AsyncMock(return_value=None))
    # This route must never remove the error and run the excluded old prompt.
    monkeypatch.setattr(routes.session_mod, "delete_messages_from", AsyncMock(
        side_effect=AssertionError("delivery retry deleted original evidence")))
    return SimpleNamespace(owner=owner, other=other, session=session, task=task,
        asset=asset, original=receipt.id, error=failed.result_message_id)


@pytest.mark.parametrize("linked", [False, True])
async def test_retry_reuses_task_exact_assets_and_one_new_submission(monkeypatch, linked):
    value = await failed_input(monkeypatch, linked=linked)
    first = await routes.regenerate_message(value.session.id, value.error, None, {"user_id": value.owner})
    duplicate = await routes.regenerate_message(value.session.id, value.error, None, {"user_id": value.owner})
    assert first["inboxId"] == duplicate["inboxId"] != value.original
    async with get_db_session() as db:
        original = await db.get(AgentInboxItem, value.original)
        retry = await db.get(AgentInboxItem, first["inboxId"])
        assert retry.prompt == original.prompt and retry.attachments == original.attachments == [value.asset]
        assert original.outcome == "delivery_error" and original.delivery_attempts == 3
        assert retry.delivery_attempts == 0 and retry.state == "accepted"
        assert await db.get(Message, value.error) is not None
        assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(
            AgentInboxItem.session_id == value.session.id)) == 2
        if linked:
            assert (await db.get(AssistantTask, value.task["task_id"])).execution_session_id == value.session.id
    lease = await reserve_run(value.session.id, value.owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        delivered = []
        async def success(_session, _owner, assets, **kwargs):
            delivered.append(assets)
        monkeypatch.setattr("sandbox.assets.deliver_asset_ids", success)
        assert (await inbox._deliver_claimed_batch(lease, batch)).should_run_provider
        surface = await load_canonical_model_surface(value.session.id, user_id=value.owner,
            run_fence=(value.session.id, lease.run_id, lease.generation), repair_tail=False)
        ids = {message.id for message in surface.messages}
        assert batch.messages[0].id in ids and original.message_id not in ids
        assert value.error not in ids and delivered == [[value.asset]]
        fence = (value.session.id, lease.run_id, lease.generation)
        answer = await create_assistant_message(value.session.id, batch.messages[0].id,
            model_id="test/model", agent="build", user_id=value.owner, run_fence=fence)
        await update_message_info(MessageInfo(id=answer.id, session_id=value.session.id, role="assistant",
            finish="stop"), user_id=value.owner, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=answer.id, outcome="succeeded")
        assert (await verify_agent_event_parity(value.session.id, user_id=value.owner)).ok
        replay = await routes.regenerate_message(value.session.id, value.error, None, {"user_id": value.owner})
        assert replay["inboxId"] == first["inboxId"]
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["new_input", "permanent", "foreign"])
async def test_retry_refuses_stale_permanent_and_foreign_sources(monkeypatch, change):
    value = await failed_input(monkeypatch, linked=False)
    if change == "new_input":
        await create_user_message(value.session.id, "现在改做另一个需求", user_id=value.owner)
    else:
        async with get_db_session() as db:
            original = await db.get(AgentInboxItem, value.original)
            if change == "permanent":
                original.error = {**original.error, "retryable": False}
            else:
                original.user_id = value.other
    with pytest.raises(HTTPException) as caught:
        await routes.regenerate_message(value.session.id, value.error, None, {"user_id": value.owner})
    assert caught.value.status_code == 409
    async with get_db_session() as db:
        assert await db.get(Message, value.error) is not None
        assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(
            AgentInboxItem.session_id == value.session.id)) == 1


async def test_old_regenerate_of_excluded_input_cannot_poison_next_recovery(monkeypatch):
    value = await failed_input(monkeypatch, linked=False)
    # Reproduce an old client's destructive retry before this fix. Only the
    # error is removed; the never-run user message remains audit-only.
    monkeypatch.undo()
    from session.session import delete_messages_from
    await delete_messages_from(value.session.id, value.error, user_id=value.owner)
    first = await load_canonical_model_surface(value.session.id, user_id=value.owner)
    second = await load_canonical_model_surface(value.session.id, user_id=value.owner)
    assert not first.messages and not second.messages
    assert (await verify_agent_event_parity(value.session.id, user_id=value.owner)).ok
