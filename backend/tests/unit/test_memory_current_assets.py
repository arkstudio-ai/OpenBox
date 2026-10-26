"""Current-input attachment reads require authenticated, live message evidence."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.part import Part
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from memory.packet import ReadBudget, TurnContext, prepare_context
from memory.read_contract import check_asset_read, current_input_assets, permits
from models.message import FilePart
from session.agent_event_log import append_surface_remove_locked, prepare_agent_event_write
from session.session import create_assistant_message, create_session, save_part
from tests.unit.assistant_helpers import finish, projected_request
from tests.unit.test_assistant_assets import asset_for
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_memory_reads import seed
from tests.unit.test_assistant_observation_replay import IMAGE_TEXT, vision  # noqa: F401
from tests.unit.test_assistant_reads import call_tool
from tool.tool import ToolContext


@pytest.fixture(autouse=True)
def local_only(monkeypatch):
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)

    async def restricted(*_args, **_kwargs):
        return {"read_policy": "current_only", "memory_forbidden": True,
            "memory": {"needed": False}, "task": {"needed": False}, "needs": {}, "usage": {}}

    monkeypatch.setattr("memory.read_routing.route", restricted)


async def user_input(ctx, assets, *, origin="human"):
    await inbox.accept_inbox_item(session_id=ctx.session_id, user_id=ctx.user_id, delivery="followup",
        prompt="Only inspect the files in this input", attachments=assets, origin=origin,
        origin_ref={"actor_user_id": ctx.user_id} if origin == "human" else {})
    lease = await reserve_run(ctx.session_id, ctx.user_id)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    message = await create_assistant_message(ctx.session_id, batch.messages[-1].id, user_id=ctx.user_id,
        model_id="test/model", agent=ctx.agent_id, run_fence=(ctx.session_id, lease.run_id, lease.generation))
    return replace(ctx, message_id=message.id, run_id=lease.run_id, run_generation=lease.generation), lease, message


async def state(monkeypatch, *, entry="assistant", origin="human"):
    identity, other, projects, config = await seed(monkeypatch)
    config.memory.semantic_recall = True
    if entry == "assistant":
        async with get_db_session() as db:
            session = await db.get(Session, identity["main_id"])
    else:
        session = await create_session(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
            project_id=projects[0])
    ctx = ToolContext(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
        session_id=session.id, project_id=session.project_id, agent_id="assistant" if entry == "assistant" else "build")
    old = await asset_for(ctx.user_id, ctx.workspace_id, mime="image/png")
    current = await asset_for(ctx.user_id, ctx.workspace_id, mime="image/png")
    library = await asset_for(ctx.user_id, ctx.workspace_id, mime="image/png")
    foreign = await asset_for(other, ctx.workspace_id, mime="image/png")
    ctx, lease, previous = await user_input(ctx, [old.id])
    await finish(ctx, lease, previous, "Previous input completed")
    ctx, lease, answer = await user_input(ctx, [current.id], origin=origin)
    turn = TurnContext(user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=ctx.session_id,
        project_id=None if entry == "assistant" else session.project_id,
        include_all_projects=entry == "assistant", turn_id=answer.parent_id, entry=entry,
        query="Only inspect the files in this input", current_asset_ids=(library.id, foreign.id))
    return SimpleNamespace(ctx=ctx, lease=lease, answer=answer, turn=turn, old=old, current=current,
        library=library, foreign=foreign, other=other, previous=previous, config=config)


async def bind(value):
    packet = await prepare_context(value.turn, value.config.memory)
    value.ctx.memory_turn = replace(packet["_turn"], read_policy=packet["route"]["read_policy"])
    value.ctx.memory_read_budget = ReadBudget()
    return packet


@pytest.mark.parametrize("entry", ["chat", "assistant"])
async def test_prepare_binds_only_actual_current_human_files_and_not_model_selected_ids(monkeypatch, entry):
    value = await state(monkeypatch, entry=entry)
    try:
        packet = await bind(value)
        assert packet["_turn"].current_asset_ids == (value.current.id,)
        assert not packet["items"] and not packet["stable_background"]["items"]
        for policy in ("current_only", "no_history", "uncertain"):
            value.ctx.memory_turn = replace(value.ctx.memory_turn, read_policy=policy)
            assert await check_asset_read(value.ctx, "assets.describe", {"asset_id": value.current.id}) is None
            assert await check_asset_read(value.ctx, "assets.read", {"asset_id": value.current.id}) is None
            assert not permits(value.ctx.memory_turn, "assets")
            assert await check_asset_read(value.ctx, "assets.list", {"asset_ids": [value.current.id]})
            for invalid in (value.old.id, value.library.id, value.foreign.id):
                assert await check_asset_read(value.ctx, "assets.describe", {"asset_id": invalid})
                assert await check_asset_read(value.ctx, "assets.describe", {"asset_ids": [value.current.id, invalid]})
    finally:
        await value.lease.release(session_status="idle")


async def test_current_only_assistant_uses_fresh_description_but_cannot_list_or_inspect_history(monkeypatch, vision):
    value = await state(monkeypatch)
    try:
        await bind(value)
        result, ctx, _ = await call_tool(value.ctx, "assets.describe", {"asset_id": value.current.id})
        assert IMAGE_TEXT in result.output
        _, wire = await projected_request(ctx)
        assert IMAGE_TEXT in str(wire)
        assert len(vision.calls) == len(vision.reads) == 1
        for operation, args in (("assets.list", {}), ("assets.describe", {"asset_id": value.old.id}),
                ("assets.describe", {"asset_ids": [value.current.id, value.library.id]})):
            refused, _, _ = await call_tool(value.ctx, operation, args)
            assert '"restricted"' in refused.output
        assert len(vision.calls) == len(vision.reads) == 1
    finally:
        await value.lease.release(session_status="idle")


async def test_current_input_revoked_during_vision_does_not_return_its_description(monkeypatch, vision):
    value = await state(monkeypatch)
    try:
        await bind(value)

        async def revoke():
            async with get_db_session() as db:
                text = await db.scalar(select(Part).where(Part.message_id == value.turn.turn_id, Part.type == "text"))
                text.data = {**text.data, "ignored": True}

        vision.before_finish = revoke
        result, _, _ = await call_tool(value.ctx, "assets.describe", {"asset_id": value.current.id})
        assert '"restricted"' in result.output and IMAGE_TEXT not in result.output
        assert len(vision.calls) == len(vision.reads) == 1
    finally:
        await value.lease.release(session_status="idle")


@pytest.mark.parametrize("identity", ["voice_item", "missing", "assistant_message", "previous_human", "another_session"])
async def test_noncurrent_or_unpersisted_turn_never_receives_an_attachment_capability(monkeypatch, identity):
    value = await state(monkeypatch)
    try:
        if identity == "voice_item": value.turn = replace(value.turn, turn_id="provider-asr-item")
        elif identity == "missing": value.turn = replace(value.turn, turn_id=None)
        elif identity == "assistant_message": value.turn = replace(value.turn, turn_id=value.answer.id)
        elif identity == "previous_human": value.turn = replace(value.turn, turn_id=value.previous.parent_id)
        else:
            session = await create_session(user_id=value.ctx.user_id, workspace_id=value.ctx.workspace_id)
            value.turn = replace(value.turn, session_id=session.id, project_id=session.project_id, include_all_projects=False)
        packet = await bind(value)
        assert packet["_turn"].current_asset_ids == ()
    finally:
        await value.lease.release(session_status="idle")


async def test_synthetic_user_role_with_real_files_is_not_a_human_attachment_grant(monkeypatch):
    value = await state(monkeypatch, origin="system_recovery")
    try:
        assert (await bind(value))["_turn"].current_asset_ids == ()
    finally:
        await value.lease.release(session_status="idle")


async def test_sql_provenance_cannot_promote_a_canonical_synthetic_message(monkeypatch):
    value = await state(monkeypatch, origin="system_recovery")
    try:
        async with get_db_session() as db:
            text = await db.scalar(select(Part).where(Part.message_id == value.turn.turn_id, Part.type == "text"))
            text.data = {**text.data, "origin": "human", "origin_ref": {"actor_user_id": value.ctx.user_id},
                         "synthetic": False, "ignored": False}
        assert (await bind(value))["_turn"].current_asset_ids == ()
    finally:
        await value.lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["asset_deleted", "asset_owner", "asset_pending", "membership", "session_deleted",
    "message_removed", "text_origin", "text_actor", "text_synthetic", "text_ignored", "file_removed", "sql_file_replaced"])
async def test_bound_capability_rechecks_revoked_asset_and_original_evidence(monkeypatch, vision, change):
    value = await state(monkeypatch)
    try:
        await bind(value)
        result, ctx, part = await call_tool(value.ctx, "assets.describe", {"asset_id": value.current.id})
        assert IMAGE_TEXT in result.output
        async with get_db_session() as db:
            asset = await db.get(FileAsset, value.current.id)
            text = await db.scalar(select(Part).where(Part.message_id == value.turn.turn_id, Part.type == "text"))
            file = await db.scalar(select(Part).where(Part.message_id == value.turn.turn_id, Part.type == "file"))
            if change == "asset_deleted": asset.is_deleted = True
            elif change == "asset_owner": asset.user_id = value.other
            elif change == "asset_pending": asset.status = "pending"
            elif change == "membership": (await db.get(WorkspaceMember, (ctx.workspace_id, ctx.user_id))).status = "removed"
            elif change == "session_deleted": (await db.get(Session, ctx.session_id)).is_deleted = True
            elif change == "message_removed":
                session = await prepare_agent_event_write(db, session_id=ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
                await append_surface_remove_locked(db, session, message_ids=[value.turn.turn_id], run_fence=ctx.run_fence)
            elif change == "text_origin": text.data = {**text.data, "origin": "unknown"}
            elif change == "text_actor": text.data = {**text.data, "origin_ref": {"actor_user_id": value.other}}
            elif change == "text_synthetic": text.data = {**text.data, "synthetic": True}
            elif change == "text_ignored": text.data = {**text.data, "ignored": True}
            elif change == "file_removed": await db.delete(file)
            elif change == "sql_file_replaced": file.data = {**file.data, "asset_id": value.library.id}
        assert await check_asset_read(ctx, "assets.describe", {"asset_id": value.current.id})
        from assistant.projection import current_observation
        assert not await current_observation(part.model_dump(), ctx)
        assert len(vision.calls) == len(vision.reads) == 1
    finally:
        await value.lease.release(session_status="idle")


async def test_sql_only_file_part_cannot_add_a_resource_to_current_input(monkeypatch):
    value = await state(monkeypatch)
    try:
        forged = FilePart(asset_id=value.library.id, session_id=value.ctx.session_id, message_id=value.turn.turn_id)
        async with get_db_session() as db:
            original = await db.scalar(select(Part).where(Part.message_id == value.turn.turn_id, Part.type == "file"))
            db.add(Part(id=forged.id, user_id=value.ctx.user_id, session_id=value.ctx.session_id,
                message_id=value.turn.turn_id, type="file", data=forged.model_dump(), created_at=original.created_at))
        assert (await bind(value))["_turn"].current_asset_ids == (value.current.id,)
    finally:
        await value.lease.release(session_status="idle")


async def test_new_input_revokes_the_previous_turns_cached_attachment_capability(monkeypatch):
    value = await state(monkeypatch)
    try:
        await bind(value)
        previous_ctx = value.ctx
        await finish(value.ctx, value.lease, value.answer, "Current files inspected")
        value.ctx, value.lease, value.answer = await user_input(value.ctx, [value.library.id])
        assert await check_asset_read(previous_ctx, "assets.describe", {"asset_id": value.current.id})
        async with get_db_session() as db:
            assert await current_input_assets(db, previous_ctx.memory_turn) == ()
    finally:
        await value.lease.release(session_status="idle")
