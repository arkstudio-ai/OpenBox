"""Semantic-mode tool observations retain only their current SQL evidence."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from assistant import knowledge, memory
from assistant.asset_vision import describe
from assistant.policy import AssistantError
from assistant.projection import current_observation
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.memory_v2 import MemorySource
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from db.models.workspace import WorkspaceMember
from memory.packet import ReadBudget, TurnContext
from session.session import create_session
from tests.unit.assistant_helpers import projected_request
from tests.unit.test_assistant_assets import asset_for
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_knowledge import page_for
from tests.unit.test_assistant_memory_reads import external_io, note, seed, sources, start  # noqa: F401
from tests.unit.test_assistant_reads import call_tool

IMAGE_TEXT = "FRESH_IMAGE_CONTENT_CANARY"


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)


@pytest.fixture
def vision(monkeypatch):
    state = SimpleNamespace(reads=[], calls=[], before_finish=None)

    async def chunks(key, **kwargs):
        state.reads.append(key)
        yield b"actual-image-pixels"

    async def llm(*args, **kwargs):
        state.calls.append(args)
        yield {"type": "text_delta", "text": IMAGE_TEXT}
        if state.before_finish:
            await state.before_finish()
        yield {"type": "finish", "reason": "stop"}

    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(get_object_chunks=chunks))
    monkeypatch.setattr("agent.vision.supports_vision", lambda _: True)
    monkeypatch.setattr("agent.model_resolve.resolve", lambda model, *_args, **_kwargs: (model or "test/vision", None))
    monkeypatch.setattr("agent.llm.reasoning_profile", lambda _: SimpleNamespace(variants=[]))
    monkeypatch.setattr("agent.llm.stream_llm", llm)
    return state


async def semantic_turn(monkeypatch):
    identity, other, projects, config = await seed(monkeypatch)
    config.memory.semantic_recall = True
    ctx, lease, message = await start(identity)
    ctx.memory_turn = TurnContext(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
        session_id=ctx.session_id, query="Use the requested evidence", entry="assistant", read_policy="allowed")
    ctx.memory_read_budget = ReadBudget()
    return ctx, lease, identity, other, projects


@pytest.mark.parametrize("compare", [False, True])
async def test_fresh_image_result_survives_each_provider_projection_without_another_vision_call(monkeypatch, vision, compare):
    ctx, lease, _, _, projects = await semantic_turn(monkeypatch)
    try:
        first = await asset_for(ctx.user_id, ctx.workspace_id, mime="image/png", project_id=projects[0],
            session_id=ctx.session_id, description="Current user supplied description")
        second = await asset_for(ctx.user_id, ctx.workspace_id, mime="image/png", project_id=projects[1]) if compare else None
        args = {"asset_ids": [first.id, second.id]} if compare else {"asset_id": first.id}
        result, ctx, _ = await call_tool(ctx, "assets.describe", args)
        payload = json.loads(result.output)
        assert payload["description"] == IMAGE_TEXT
        assert len(payload["source_refs"]) == (2 if compare else 1)
        assert all(len(ref["object_version"]) == len(ref["description_version"]) == 64 for ref in payload["source_refs"])
        assert first.oss_key not in result.output
        # The assistant's storage project must not exclude owned library assets.
        for _ in range(2):
            _, wire = await projected_request(ctx)
            assert IMAGE_TEXT in json.dumps(wire)
        assert len(vision.calls) == 1 and len(vision.reads) == (2 if compare else 1)
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["deleted", "pending", "owner", "workspace", "object", "size", "mime",
    "description", "description_time", "description_state", "name", "asset_project", "source_session",
    "project_deleted", "project_owner", "turn_project", "turn_all_projects"])
async def test_changed_asset_evidence_cannot_reenter_provider_input(monkeypatch, vision, change):
    ctx, lease, _, other, projects = await semantic_turn(monkeypatch)
    try:
        asset = await asset_for(ctx.user_id, ctx.workspace_id, mime="image/png", project_id=projects[0],
            session_id=ctx.session_id, description="Original description")
        _, ctx, _ = await call_tool(ctx, "assets.describe", {"asset_id": asset.id})
        async with get_db_session() as db:
            row = await db.get(FileAsset, asset.id)
            if change == "deleted": row.is_deleted = True
            elif change == "pending": row.status = "pending"
            elif change == "owner": row.user_id = other
            elif change == "workspace": row.workspace_id = (await db.get(User, other)).default_workspace_id
            elif change == "object": row.oss_key += "-replaced"
            elif change == "size": row.size += 1
            elif change == "mime": row.mime = "image/jpeg"
            elif change == "description": row.description = "New user correction without a timestamp bump"
            elif change == "description_time": row.description_updated_at = datetime.now(timezone.utc)
            elif change == "description_state": row.description_state = "failed"
            elif change == "name": row.name = "New label"
            elif change == "asset_project": row.project_id = projects[1]
            elif change == "source_session": row.session_id = None
            elif change == "project_deleted": (await db.get(Project, projects[0])).is_deleted = True
            elif change == "project_owner": (await db.get(Project, projects[0])).user_id = other
            elif change == "turn_project": ctx.memory_turn = replace(ctx.memory_turn, project_id=projects[0])
            elif change == "turn_all_projects": ctx.memory_turn = replace(ctx.memory_turn, include_all_projects=True)
        _, wire = await projected_request(ctx)
        serialized = json.dumps(wire)
        assert IMAGE_TEXT not in serialized and "fresh_read_required" in serialized
        assert len(vision.calls) == len(vision.reads) == 1
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["membership", "main_deleted", "reader_session"])
async def test_asset_observation_refuses_lost_reader_authority(monkeypatch, vision, change):
    ctx, lease, _, _, _ = await semantic_turn(monkeypatch)
    try:
        asset = await asset_for(ctx.user_id, ctx.workspace_id, mime="image/png")
        _, ctx, part = await call_tool(ctx, "assets.describe", {"asset_id": asset.id})
        async with get_db_session() as db:
            if change == "membership": (await db.get(WorkspaceMember, (ctx.workspace_id, ctx.user_id))).status = "removed"
            elif change == "main_deleted": (await db.get(Session, ctx.session_id)).is_deleted = True
            else: ctx.memory_turn = replace(ctx.memory_turn, session_id="another-session")
        assert not await current_observation(part.model_dump(), ctx)
        assert len(vision.calls) == len(vision.reads) == 1
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["object", "description", "deleted", "scope"])
async def test_changes_during_inspection_are_rejected_before_the_first_projection(monkeypatch, vision, change):
    ctx, lease, _, _, projects = await semantic_turn(monkeypatch)
    try:
        asset = await asset_for(ctx.user_id, ctx.workspace_id, mime="image/png", project_id=projects[0])

        async def mutate():
            async with get_db_session() as db:
                row = await db.get(FileAsset, asset.id)
                if change == "object": row.oss_key += "-replaced"
                elif change == "description": row.description = "User correction during inspection"
                elif change == "deleted": row.is_deleted = True
                else: ctx.memory_turn = replace(ctx.memory_turn, project_id=projects[0])

        vision.before_finish = mutate
        with pytest.raises(AssistantError):
            await describe(ctx, asset_id=asset.id, question="What is visible?")
        assert len(vision.calls) == len(vision.reads) == 1
    finally:
        await lease.release(session_status="idle")


async def test_asset_scope_cannot_widen_and_legacy_outputs_cannot_invent_a_snapshot(monkeypatch, vision):
    ctx, lease, _, _, projects = await semantic_turn(monkeypatch)
    try:
        own_project = await asset_for(ctx.user_id, ctx.workspace_id, mime="image/png", project_id=projects[0])
        another_project = await asset_for(ctx.user_id, ctx.workspace_id, mime="image/png", project_id=projects[1])
        ctx.memory_turn = replace(ctx.memory_turn, project_id=projects[0])
        with pytest.raises(AssistantError):
            await describe(ctx, asset_id=another_project.id, question="What is visible?")
        assert not vision.calls and not vision.reads
        _, ctx, part = await call_tool(ctx, "assets.describe", {"asset_id": own_project.id})
        _, wire = await projected_request(ctx)
        assert IMAGE_TEXT in json.dumps(wire)
        payload = json.loads(part.output)
        payload.pop("source_refs")
        legacy = {**part.model_dump(), "output": json.dumps(payload)}
        assert not await current_observation(legacy, ctx)
        assert len(vision.calls) == len(vision.reads) == 1
    finally:
        await lease.release(session_status="idle")


async def test_deleting_original_chat_does_not_revoke_an_owned_library_asset(monkeypatch, vision):
    """session.policy.asset_audience retains the owner's files after chat removal."""
    ctx, lease, _, _, projects = await semantic_turn(monkeypatch)
    try:
        original = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id, project_id=projects[0])
        asset = await asset_for(ctx.user_id, ctx.workspace_id, mime="image/png", project_id=projects[0],
            session_id=original.id)
        _, ctx, _ = await call_tool(ctx, "assets.describe", {"asset_id": asset.id})
        async with get_db_session() as db:
            (await db.get(Session, original.id)).is_deleted = True
        _, wire = await projected_request(ctx)
        assert IMAGE_TEXT in json.dumps(wire)
        assert len(vision.calls) == len(vision.reads) == 1
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("operation", ["memory.search", "memory.read", "knowledge.directory", "knowledge.read"])
async def test_semantic_observation_revalidates_exact_revoked_sources_without_searching_again(monkeypatch, external_io, operation):
    ctx, lease, identity, _, _ = await semantic_turn(monkeypatch)
    try:
        marker = "MEMORY_CANARY exact evidence"
        value = await note(identity, marker)
        external_io.hits = [value]
        if operation.startswith("memory."):
            ref = (await memory.search(**identity, query="MEMORY_CANARY"))["items"][0]["source_ref"]
            source_id = (await sources(value["id"]))[0].id
            args = {"query": "MEMORY_CANARY"} if operation.endswith("search") else {"source_ref": ref}
        else:
            marker = "WIKI_EXACT_EVIDENCE_CANARY"
            page, _ = await page_for(identity, None, marker, note=value)
            source_id = page.source_manifest[0]["id"]
            ref = (await knowledge.directory(**identity))["items"][0]["source_ref"]
            args = {} if operation.endswith("directory") else {"source_ref": ref}
        result, ctx, _ = await call_tool(ctx, operation, args)
        assert marker in result.output
        provider_calls = len(external_io.calls)
        _, wire = await projected_request(ctx)
        assert marker in json.dumps(wire)
        async with get_db_session() as db:
            (await db.get(MemorySource, source_id)).status = "REVOKED"
        _, wire = await projected_request(ctx)
        serialized = json.dumps(wire)
        assert marker not in serialized and "fresh_read_required" in serialized
        assert len(external_io.calls) == provider_calls
    finally:
        await lease.release(session_status="idle")
