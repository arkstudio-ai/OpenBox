"""A revoked run cannot commit chat, session or provider state, recording on or off.

Ported from the recorder-only check in test_trajectory_session_runtime.py: the
same writes are now refused by question.runtime, for superseded runs and for
runs whose lease was recovered in this process.
"""
import hashlib
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

import db.base as database
from db.models.file_asset import FileAsset
from db.models.internal_part import InternalPart
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from db.models.video_job import VideoJob
from models.message import TextPart, ToolPartData, ToolStatus
from question import runtime
from session.internal_parts import (
    PROVIDER_TRANSCRIPT_KIND,
    TOOL_REVEAL_KIND,
    ProviderCapabilityBinding,
    ToolRevealEvent,
    commit_tool_reveals,
    save_internal_part,
)
from session.session import (
    create_assistant_message,
    create_user_message,
    save_part,
    update_message_info,
    update_part_data,
    update_session,
)
from tests.unit.test_durable_questions import read, state  # noqa: F401
from tests.unit.test_run_fencing_api import acting_as, expire_lease, recording  # noqa: F401

WRITES = ("save_part", "create_assistant_message", "update_message_info", "update_part_data",
          "update_session", "save_internal_part", "commit_tool_reveals")
BINDING = ProviderCapabilityBinding(provider="openai", endpoint="https://api.example.test/v1",
                                    account_id="acct", api_version="2026-08-01", model="gpt-test",
                                    dialect="responses", beta_headers=("tools-v2",))


async def _owned_turn():
    """A live run that already wrote its assistant message and a capability_search result."""
    prompt = await create_user_message("s1", "Find the right tool", user_id="u1")
    ticket = await runtime.start_run("s1", "u1")
    with acting_as(ticket):
        assistant = await create_assistant_message("s1", prompt.id, user_id="u1")
        search = ToolPartData(tool="capability_search", status=ToolStatus.COMPLETED, input={"query": "read"},
                              output="read", call_id="search", session_id="s1", message_id=assistant.id)
        await save_part(search, is_new=True, user_id="u1")
    return ticket, assistant, search


async def _count(model, *conditions) -> int:
    async with database.get_db_session() as db:
        return await db.scalar(select(func.count()).select_from(model).where(*conditions))


def _write(kind: str, assistant, search, marker: str):
    """One write as the bound run, and a probe telling whether it committed."""
    if kind == "save_part":
        part = TextPart(text=marker, session_id="s1", message_id=assistant.id)

        async def probe():
            return await read(Part, part.id) is not None
        return lambda: save_part(part, is_new=True, user_id="u1"), probe
    if kind == "create_assistant_message":
        async def probe():
            return await _count(Message, Message.session_id == "s1", Message.agent == marker) == 1
        return lambda: create_assistant_message("s1", assistant.parent_id, agent=marker, user_id="u1"), probe
    if kind == "update_message_info":
        info = assistant.model_copy(update={"error": {"message": marker}})

        async def probe():
            return (await read(Message, assistant.id)).error == {"message": marker}
        return lambda: update_message_info(info, user_id="u1"), probe
    if kind == "update_part_data":
        data = {**search.model_dump(), "title": marker}

        async def probe():
            return (await read(Part, search.id)).data.get("title") == marker
        return lambda: update_part_data(search.id, data, user_id="u1"), probe
    if kind == "update_session":
        async def probe():
            return (await read(Session, "s1")).title == marker
        return lambda: update_session("s1", user_id="u1", title=marker), probe
    if kind == "save_internal_part":
        async def probe():
            return await _count(InternalPart, InternalPart.response_chain_id == marker) == 1
        return lambda: save_internal_part(
            session_id="s1", user_id="u1", message_id=assistant.id, kind=PROVIDER_TRANSCRIPT_KIND,
            data={"type": "search_result", "marker": marker}, binding=BINDING,
            response_chain_id=marker, stream_seq=1), probe
    reveal = ToolRevealEvent(
        session_id="s1", user_id="u1", message_id=assistant.id, origin_part_id=search.id, agent_id="build",
        canonical_tool_id=f"read-{marker}", schema_digest=hashlib.sha256(marker.encode()).hexdigest(),
        catalog_generation="generation-1", evidence_source="portable", stream_seq=1)

    async def probe():
        async with database.get_db_session() as db:
            rows = (await db.scalars(select(InternalPart).where(InternalPart.kind == TOOL_REVEAL_KIND))).all()
        return any(json.dumps(row.data).find(f"read-{marker}") >= 0 for row in rows)
    return lambda: commit_tool_reveals([reveal]), probe


@pytest.mark.parametrize("revocation", ["superseded", "lease_lost"])
@pytest.mark.parametrize("kind", WRITES)
async def test_revoked_run_cannot_commit_chat_session_or_provider_state(state, recording, kind, revocation):
    ticket, assistant, search = await _owned_turn()
    action, probe = _write(kind, assistant, search, "while-owned")
    with acting_as(ticket):
        await action()
    assert await probe()

    if revocation == "superseded":
        await create_user_message("s1", "New requirements", user_id="u1")
    else:
        await expire_lease()
        await runtime.recover_expired_runs()
    action, probe = _write(kind, assistant, search, "after-revocation")
    with acting_as(ticket), pytest.raises(runtime.RunRevoked) as refused:
        await action()
    assert refused.value.reason == revocation
    assert not await probe()


async def _finished_video(key: str) -> VideoJob:
    asset_id, job_id = uuid4().hex, uuid4().hex
    async with database.get_db_session() as db:
        db.add(FileAsset(id=asset_id, user_id="u1", workspace_id="w1", session_id="s1", name=f"{key}.mp4",
                         oss_key=f"assets/u1/{asset_id}/{key}.mp4", mime="video/mp4", size=16, status="ready",
                         created_at=runtime.now()))
        await db.flush()
        job = VideoJob(id=job_id, user_id="u1", session_id="s1", kind="segment", idempotency_key=key,
                       status="completed", model="fixture", attempt=1, request_data={}, result_data={},
                       output_asset_id=asset_id, created_at=runtime.now(), updated_at=runtime.now())
        db.add(job)
    return job


async def _attached(asset_id: str) -> bool:
    async with database.get_db_session() as db:
        parts = (await db.scalars(select(Part).where(Part.session_id == "s1", Part.type == "file"))).all()
    return any(part.data.get("asset_id") == asset_id for part in parts)


async def test_detached_video_finalization_attaches_after_finish_but_not_after_supersession(
        state, recording, monkeypatch):
    from tool import video_production
    from tool.tool import ToolContext
    prompt = await create_user_message("s1", "Make a clip", user_id="u1")
    ticket = await runtime.start_run("s1", "u1")
    with acting_as(ticket):
        assistant = await create_assistant_message("s1", prompt.id, user_id="u1")
    ctx = ToolContext(session_id="s1", user_id="u1", workspace_id="w1", message_id=assistant.id)
    await runtime.finish_run(ticket, completed=True)

    # The finalization task inherited the run's identity and outlives the run.
    finished = await _finished_video("after-finish")
    with acting_as(ticket):
        assert await video_production._attach_completed(finished, ctx)
    assert await _attached(finished.output_asset_id)

    retry = await create_user_message("s1", "A different clip", user_id="u1")
    late = await _finished_video("after-supersession")
    with acting_as(ticket), pytest.raises(runtime.RunRevoked):
        await video_production._attach_completed(late, ctx)
    assert not await _attached(late.output_asset_id)

    # The refused attach leaves no claim behind: the run that owns the new turn
    # can still put the finished video into its own reply.
    current = await runtime.start_run("s1", "u1")
    with acting_as(current):
        reply = await create_assistant_message("s1", retry.id, user_id="u1")
        owner_ctx = ToolContext(session_id="s1", user_id="u1", workspace_id="w1", message_id=reply.id)
        assert await video_production._attach_completed(await read(VideoJob, late.id), owner_ctx)
    assert await _attached(late.output_asset_id)
    assert (await read(VideoJob, late.id)).attached_message_id == reply.id
    await runtime.finish_run(current, completed=True)
