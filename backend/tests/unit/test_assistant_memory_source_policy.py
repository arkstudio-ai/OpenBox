"""Existing Wiki jobs must not send sources whose Session becomes isolated.

The source, confirmation, compilation job, claim, provider guard and terminal
receipt are real services and SQL. Only the external model/verifier is fake.
"""
import asyncio
import json

import pytest
from sqlalchemy import func, select

from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemorySource, MemorySourceLink
from db.models.memory_wiki import MemoryWikiCandidate, MemoryWikiJob
from db.models.session import Session
from memory import service
from memory.policy import resolve_access_scope
from memory.wiki import service as wiki
from memory.wiki.worker import MemoryWikiWorker, SQLCompilationCache
from session.session import create_session, create_user_message
from tests.unit.test_automatic_knowledge import Verifier
from tests.unit.test_memory_wiki import FakeModel, seed, wiki_database  # noqa: F401

BODY = "Source policy canary: this project uses concise Chinese replies."


async def original_source(monkeypatch):
    user, workspace, project, _, config = await seed(monkeypatch)
    session = await create_session(user_id=user, workspace_id=workspace, project_id=project, agent="build")
    message = await create_user_message(session.id, BODY, user_id=user, origin="human",
                                        origin_ref={"actor_user_id": user})
    proposal = await service.propose_note(user_id=user, workspace_id=workspace, project_id=project,
                                           summary=BODY, session_id=session.id)
    note = await service.confirm_note(user_id=user, workspace_id=workspace, proposal_id=proposal["id"],
                                     expected_revision=proposal["revision"])
    async with get_db_session() as db:
        source = await db.scalar(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == note["id"],
            MemorySourceLink.revision == note["revision"], MemorySourceLink.relation == "SUPPORTS",
            MemorySource.source_kind == "user_statement"))
        assert source.session_id == session.id and source.message_id == message.id
        return user, workspace, project, session.id, note, source.id, config


async def isolate(session_id, change="policy"):
    parent = None
    if change == "assistant_child":
        async with get_db_session() as db:
            owner = await db.get(Session, session_id)
            user_id, workspace_id, project_id = owner.user_id, owner.workspace_id, owner.project_id
        parent = await create_session(user_id=user_id, workspace_id=workspace_id, project_id=project_id, agent="build")
    async with get_db_session() as db:
        row = await db.get(Session, session_id)
        if change in {"kind", "assistant_child"}:
            row.kind = "assistant"
            row.parent_id = parent.id if parent else None
        else:
            row.memory_policy = "assistant_isolated" if change == "policy" else "unknown-policy"


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("change", ["policy", "kind", "unknown", "assistant_child"])
async def test_source_authority_rejects_isolated_sessions_with_or_without_prefetched_facts(monkeypatch, batched, change):
    """Isolated execution sessions, unknown policies and assistant children never
    hold evidence. Since V2 P3 a top-level assistant session ("kind") is the
    person's own main session, whose evidence stands like any of their chats."""
    user, workspace, project, session, _, source_id, _ = await original_source(monkeypatch)

    async def available():
        async with get_db_session() as db:
            scope = await resolve_access_scope(db, user_id=user, workspace_id=workspace, project_id=project)
            source = await db.get(MemorySource, source_id)
            if batched:
                async with service.source_authority(db, scope):
                    await service.prefetch_source_facts(db, scope, [source])
                    return await service.source_is_available(db, scope, source)
            return await service.source_is_available(db, scope, source)

    assert await available()
    await isolate(session, change)
    assert await available() is (change == "kind")


@pytest.mark.parametrize("stage", ["standard", "pending", "before_send", "between_calls"])
async def test_real_wiki_pipeline_rechecks_source_policy_before_each_provider_call(monkeypatch, record_property, stage):
    user, workspace, project, session, note, source_id, config = await original_source(monkeypatch)
    config.automatic_knowledge = stage == "between_calls"
    queued = await wiki.schedule_compile(user_id=user, workspace_id=workspace, project_id=project,
        slug="source-policy", title="Source policy", memory_ids=[note["id"]], config=config)
    captured, release = asyncio.Event(), asyncio.Event()
    original_get = SQLCompilationCache.get

    async def pause_after_capture(cache, key):
        value = await original_get(cache, key)
        captured.set()
        await release.wait()
        return value

    class Model(FakeModel):
        async def generate(self, request):
            assert source_id in {source.id for source in request.sources}
            assert any(source.text == BODY for source in request.sources)
            result = await super().generate(request)
            if stage == "between_calls":
                await isolate(session)
            return result

    model, verifier = Model(), Verifier()
    worker = MemoryWikiWorker(config, model=model, verifier=verifier)
    if stage == "pending":
        await isolate(session)
    if stage == "before_send":
        # Suspend after the real request/cache read, before CheckedModel's
        # real recheck; the policy commit uses a separate SQL session.
        monkeypatch.setattr(SQLCompilationCache, "get", pause_after_capture)
        running = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(captured.wait(), 5)
            await isolate(session)
        finally:
            release.set()
        assert await running
    else:
        assert await worker.run_once()

    async with get_db_session() as db:
        job = await db.get(MemoryWikiJob, queued["id"])
        candidates = await db.scalar(select(func.count()).select_from(MemoryWikiCandidate).where(
            MemoryWikiCandidate.user_id == user))
        jobs = await db.scalar(select(func.count()).select_from(MemoryWikiJob).where(MemoryWikiJob.user_id == user))
        current_note = await db.get(UserMemory, note["id"])
        assert current_note.revision == note["revision"] and current_note.value["summary"] == BODY
        assert jobs == 1 and job.lease_generation == 1 and job.attempts == 1
        if stage == "standard":
            assert job.status == "COMPLETED" and job.candidate_id and candidates == 1
            assert model.calls == 1
        else:
            assert job.status == "CANCELLED" and job.candidate_id is None and candidates == 0
            assert job.last_error == "wiki_source_changed"
            assert model.calls == (1 if stage == "between_calls" else 0)
            assert verifier.calls == 0
        record_property("source_policy_evidence", json.dumps({"session_id": session, "source_id": source_id,
            "memory_id": note["id"], "memory_revision": current_note.revision, "job_id": job.id,
            "generation": job.lease_generation, "attempts": job.attempts, "status": job.status,
            "job_count": jobs, "candidate_count": candidates, "model_calls": model.calls,
            "verifier_calls": verifier.calls, "stage": stage}))
