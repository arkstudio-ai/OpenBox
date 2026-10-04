"""Resource metadata provenance and attachment commands on actual SQL/Inbox paths."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from agent import inbox
from agent.driver import reserve_run
from assistant.assets import attach_assets, list_assets
from assistant.business_context import validate
from assistant.commands import ToolSource, tool_command_key
from assistant.evidence import validate_message_sources
from assistant.policy import AssistantError
from assistant.results import deliver_task_result
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from db.models.file_asset import FileAsset
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from models.message import TextPart
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_api import client_for
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_context_sources import finish
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import call_tool, read_turn
from tests.unit.test_assistant_reporting import prepare_report
from tests.unit.test_assistant_results import result_ready
from tool.assistant_tools import assistant_tools


async def asset_for(user, workspace, **changes):
    key = "asset_" + uuid4().hex
    row = FileAsset(id=key, user_id=user, workspace_id=workspace,
        name="Private report.txt", mime="text/plain", size=128, oss_key="test-only/" + key,
        status="ready", source="user", transient=False, is_deleted=False,
        created_at=datetime.now(timezone.utc))
    for name, value in changes.items():
        setattr(row, name, value)
    async with get_db_session() as db:
        db.add(row)
    return row


async def ready_task():
    user, workspace, main, original, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        revision = (await db.get(AssistantTask, original["task_id"])).control_revision
    asset = await asset_for(user, workspace, session_id=main.id, project_id=main.project_id)
    args = dict(user_id=user, workspace_id=workspace, main_id=main.id, task_id=original["task_id"],
        text="Use this original resource for the next draft", attachment_ids=[asset.id],
        expected_revision=revision, idempotency_key="attach-1")
    return asset, original, args


async def test_inventory_is_bounded_private_metadata_without_signing_or_side_effects(monkeypatch):
    user, other, workspace, main, _ = await setup_task()
    first = await asset_for(user, workspace, name="literal%_report.txt", project_id=main.project_id)
    second = await asset_for(user, workspace, source="agent")
    for changes in ({"status": "pending"}, {"is_deleted": True}, {"transient": True}):
        await asset_for(user, workspace, **changes)
    await asset_for(other, workspace, name="Another person's resource")
    def forbidden(*args, **kwargs):
        raise AssertionError("Metadata listing must not sign or wake")
    monkeypatch.setattr("core.oss.get_oss", forbidden)
    monkeypatch.setattr(inbox, "schedule_inbox_wake", forbidden)
    scope = dict(user_id=user, workspace_id=workspace, main_id=main.id)
    page = await list_assets(**scope, limit=1)
    next_page = await list_assets(**scope, limit=1, cursor=page["next_cursor"])
    assert {x["id"] for x in page["items"] + next_page["items"]} == {first.id, second.id}
    assert next_page["next_cursor"] is None
    assert [x["id"] for x in (await list_assets(**scope, query="%_", project_id=main.project_id))["items"]] == [first.id]
    assert [x["id"] for x in (await list_assets(**scope, source="agent"))["items"]] == [second.id]
    assert not {"url", "oss_key", "sandboxPath", "text"} & page["items"][0].keys()
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(AssistantCommand.actor_user_id == user)) == 0
        assert await db.scalar(select(func.count()).select_from(Session).where(Session.user_id == user)) == 1


async def test_two_requests_and_process_restart_replay_one_original_attachment_input():
    asset, original, args = await ready_task()
    a, b = await asyncio.gather(attach_assets(**args), attach_assets(**args))
    assert a == b and a["state"] == "accepted" and a["execution_session_id"] == original["execution_session_id"]
    async with get_db_session() as db:
        url = db.get_bind().url
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
            AssistantCommand.actor_user_id == args["user_id"], AssistantCommand.action == "asset_attach")) == 1
        command = await db.get(AssistantCommand, a["command_id"])
        queued = await db.get(AgentInboxItem, a["inbox_id"])
        submission = await db.get(TaskSubmission, a["submission_id"])
        assert queued.attachments == [asset.id] and queued.origin == "human" and submission.disposition == "accepted"
        assert command.state == "accepted" and (await db.get(FileAsset, asset.id)).session_id == args["main_id"]
    await close_engine()
    init_engine(url.render_as_string(hide_password=False))
    assert await attach_assets(**args) == a
    with pytest.raises(AssistantError) as changed:
        await attach_assets(**{**args, "text": "different input"})
    assert changed.value.code == "ASSISTANT_COMMAND_CONFLICT"
    with pytest.raises(AssistantError) as stale:
        await attach_assets(**{**args, "idempotency_key": "another"})
    assert stale.value.code == "ASSISTANT_REVISION_CONFLICT"


@pytest.mark.parametrize("change", ["deleted", "pending", "foreign_owner", "foreign_workspace", "missing", "shared_target", "membership"])
async def test_attachment_rechecks_current_source_and_private_target_without_partial_input(change):
    asset, original, args = await ready_task()
    async with get_db_session() as db:
        row = await db.get(FileAsset, asset.id)
        if change == "deleted": row.is_deleted = True
        if change == "pending": row.status = "pending"
        if change == "foreign_owner":
            row.user_id = await db.scalar(select(WorkspaceMember.user_id).where(
                WorkspaceMember.workspace_id == args["workspace_id"], WorkspaceMember.user_id != args["user_id"]))
        if change == "foreign_workspace":
            from db.models.user import User
            row.workspace_id = await db.scalar(select(User.default_workspace_id).where(User.id != args["user_id"]).limit(1))
        if change == "shared_target": (await db.get(Session, original["execution_session_id"])).visibility = "workspace"
        if change == "membership": (await db.get(WorkspaceMember, (args["workspace_id"], args["user_id"]))).status = "removed"
        if change == "missing": args["attachment_ids"] = ["missing"]
    with pytest.raises((AssistantError, inbox.InboxAttachmentError)):
        await attach_assets(**args)
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
            AssistantCommand.actor_user_id == args["user_id"], AssistantCommand.action == "asset_attach")) == 0
        assert (await db.get(AssistantTask, original["task_id"])).control_revision == args["expected_revision"]
        assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(
            AgentInboxItem.session_id == original["execution_session_id"])) == 1


async def test_claim_keeps_file_identity_and_delivers_result_from_original_session(monkeypatch):
    asset, original, args = await ready_task()
    receipt = await attach_assets(**args)
    lease = await reserve_run(original["execution_session_id"], args["user_id"])
    delivered = []
    async def delivery(session_id, user_id, asset_ids, **kwargs):
        delivered.append((session_id, user_id, asset_ids))
        return ["/workspace/uploads/Private report.txt"]
    monkeypatch.setattr("sandbox.assets.deliver_asset_ids", delivery)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True, deliver_attachments=True)
        assert batch.attachment_ids == (asset.id,)
        assert delivered == [(lease.session_id, args["user_id"], [asset.id])]
        file = next(p for p in batch.messages[0].parts if p.type == "file")
        assert file.asset_id == asset.id and batch.receipts[0].id == receipt["inbox_id"]
        fence = (lease.session_id, lease.run_id, lease.generation)
        message = await create_assistant_message(lease.session_id, batch.messages[0].id,
            model_id="test/model", agent="build", user_id=lease.user_id, run_fence=fence)
        await save_part(TextPart(session_id=lease.session_id, message_id=message.id, text="Attachment processing completed."),
            user_id=lease.user_id, is_new=True, run_fence=fence)
        message.finish = "stop"
        await update_message_info(message, user_id=lease.user_id, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
        async with get_db_session() as db:
            result = await db.scalar(select(TaskResult).where(TaskResult.result_message_id == message.id))
            assert result.consumed_inbox_ids == [receipt["inbox_id"]]
            assert (await db.get(FileAsset, asset.id)).session_id == args["main_id"]
        report = await deliver_task_result(result.id)
        assert report["inbox_id"]
        async with get_db_session() as db:
            assert (await db.get(TaskResult, result.id)).delivery_state == "accepted"
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["deleted", "owner", "object", "membership"])
async def test_asset_metadata_evidence_is_revalidated_for_provider_and_saved_answers(change):
    ctx, lease, answer, _, _ = await read_turn()
    asset = await asset_for(ctx.user_id, ctx.workspace_id, name="PRIVATE_ASSET_MARKER.txt")
    try:
        result, _, _ = await call_tool(ctx, "assets.list", {})
        assert asset.id in result.output and "test-only/" not in result.output
        await consume_context(ctx)
        old = deepcopy(ctx._assistant_context["business_reads"][0])
        await finish(ctx, lease, answer, "PRIVATE_ASSET_DERIVATION")
        async with get_db_session() as db:
            row = await db.get(FileAsset, asset.id)
            if change == "deleted": row.is_deleted = True
            if change == "object": row.oss_key = "a-replacement-object"
            if change == "owner":
                row.user_id = await db.scalar(select(WorkspaceMember.user_id).where(
                    WorkspaceMember.workspace_id == ctx.workspace_id, WorkspaceMember.user_id != ctx.user_id))
            if change == "membership": (await db.get(WorkspaceMember, (ctx.workspace_id, ctx.user_id))).status = "removed"
        async with get_db_session() as db:
            with pytest.raises(AssistantError):
                await validate_message_sources(db, answer, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
            if change != "membership":
                with pytest.raises(AssistantError):
                    await validate(db, await db.get(Session, ctx.session_id), old)
    finally:
        await lease.release(session_status="idle")


async def test_real_tool_uses_server_command_identity_and_report_mode_cannot_attach(monkeypatch):
    ctx, lease, _, original, _ = await read_turn()
    asset = await asset_for(ctx.user_id, ctx.workspace_id)
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lambda *_: None)
    try:
        async with get_db_session() as db:
            human = await db.scalar(select(AgentInboxItem.message_id).where(
                AgentInboxItem.session_id == ctx.session_id, AgentInboxItem.run_id == lease.run_id))
            task = await db.get(AssistantTask, original["task_id"])
        args = dict(task_id=task.id, text="Use this file", attachment_ids=[asset.id],
            expected_revision=task.control_revision, source_message_ids=[human])
        await consume_context(ctx)
        result, tool_ctx, part = await call_tool(ctx, "assets.attach", args)
        assert not result.metadata.get("error"), result.output
        receipt = json.loads(result.output)
        async with get_db_session() as db:
            command = await db.get(AssistantCommand, receipt["command_id"])
            queued = await db.get(AgentInboxItem, receipt["inbox_id"])
            assert command.idempotency_key == tool_command_key(ctx.session_id, part.id)
            assert command.action == "asset_attach" and queued.origin == "assistant_delegation"
            assert queued.origin_ref["source_refs"][0]["message_id"] == human
        with pytest.raises(AssistantError) as forged:
            await attach_assets(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
                idempotency_key="forged", source=ToolSource("forged", lease.run_id, lease.generation, (human,)),
                **{key: value for key, value in args.items() if key != "source_message_ids"})
        assert forged.value.code == "ASSISTANT_CALL_UNVERIFIED"
    finally:
        await lease.release(session_status="idle")
    report_ctx, report_lease, *_ = await prepare_report()
    try:
        for operation, arguments in (("assets.list", {}), ("assets.attach", args)):
            tool = next(t for t in assistant_tools if t.id == operation)
            denied = await tool.execute(arguments, report_ctx)
            assert denied.metadata["failure_code"] == "ASSISTANT_TOOL_FORBIDDEN"
    finally:
        await report_lease.release(session_status="idle")


async def test_http_clients_share_the_same_inventory_and_command_contract(monkeypatch):
    asset, original, args = await ready_task()
    async with client_for(args["user_id"], args["workspace_id"], monkeypatch) as client:
        page = await client.get("/api/assistant/assets", params={"query": "Private report"})
        assert page.status_code == 200 and page.json()["items"][0]["id"] == asset.id
        body = {key: value for key, value in args.items() if key not in ("user_id", "workspace_id", "main_id", "task_id")}
        url = "/api/assistant/tasks/" + original["task_id"] + "/assets"
        first, replay = await client.post(url, json=body), await client.post(url, json=body)
        assert first.status_code == replay.status_code == 200 and first.json() == replay.json()
        assert first.json()["execution_session_id"] == original["execution_session_id"]
        assert (await client.post(url, json={**body, "attachment_ids": []})).status_code == 422
        assert (await client.post(url, json={**body, "actor_user_id": "forged"})).status_code == 422


async def test_unfiled_asset_filing_and_rename_refresh_without_rewriting_consumed_observation():
    ctx, lease, _, _, _ = await read_turn()
    asset = await asset_for(ctx.user_id, ctx.workspace_id, name="Original filename.txt")
    try:
        await call_tool(ctx, "assets.list", {})
        await consume_context(ctx)
        old = deepcopy(ctx._assistant_context["business_reads"][0])
        async with get_db_session() as db:
            row = await db.get(FileAsset, asset.id)
            row.name, row.session_id, row.project_id = "Renamed filename.txt", ctx.session_id, ctx.project_id
        await consume_context(ctx)
        fresh = ctx._assistant_context["business_reads"][0]
        assert fresh["digest"] != old["digest"]
        async with get_db_session() as db:
            main = await db.get(Session, ctx.session_id)
            await validate(db, main, old)
            with pytest.raises(AssistantError) as changed:
                await validate(db, main, old, fresh=True)
            assert changed.value.code == "ASSISTANT_BUSINESS_SNAPSHOT_CHANGED"
    finally:
        await lease.release(session_status="idle")


async def test_accepted_asset_revoked_before_claim_never_becomes_a_text_only_execution(monkeypatch):
    asset, original, args = await ready_task()
    receipt = await attach_assets(**args)
    async with get_db_session() as db:
        (await db.get(FileAsset, asset.id)).is_deleted = True
    async def forbidden(*args, **kwargs):
        raise AssertionError("Revoked asset must not reach the delivery client")
    monkeypatch.setattr("sandbox.assets.deliver_asset_ids", forbidden)
    lease = await reserve_run(original["execution_session_id"], args["user_id"])
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True, deliver_attachments=True)
        assert batch.empty
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, receipt["inbox_id"])).state == "canceled"
            assert (await db.get(AgentInboxItem, receipt["inbox_id"])).error["code"] == "ASSISTANT_ASSET_UNAVAILABLE"
            assert (await db.get(TaskSubmission, receipt["submission_id"])).disposition == "canceled"
            task = await db.get(AssistantTask, receipt["task_id"])
            assert task.observed_state == "idle" and task.control_revision == args["expected_revision"] + 2
    finally:
        await lease.release(session_status="idle")
