"""Host isolation, immutable approval CAS, late jobs and SQL invalidation."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from core.config import OpenBoxConfig
from db.base import Base, close_engine, get_db_session, init_engine
from db.models.memory_v2 import MemoryDebugRun, MemoryOutbox, MemorySource, MemoryTombstone
from db.models.memory_wiki import MemoryWikiCandidate, MemoryWikiJob, MemoryWikiPage
from db.models.project import Project
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from memory import service as memory_service
from memory.policy import resolve_access_scope
from memory.time_context import document_matches_time, resolve_query_time
from memory.wiki import service
from memory.wiki.worker import MemoryWikiWorker, claim_job, commit_candidate, read_request
from wiki_compiler import compile_candidate


@pytest.fixture(autouse=True)
async def wiki_database(tmp_path):
    await close_engine()
    url = os.environ.get("MEMORY_WIKI_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'wiki.db'}"
    engine = init_engine(url)
    import db.models  # noqa: F401
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield
    await close_engine()


class FakeModel:
    def __init__(self):
        self.calls = 0

    async def generate(self, request):
        self.calls += 1
        return {"paragraphs": [{"text": "项目约定：" + source.text,
            "citations": [{"source_id": source.id, "quote": source.text}]} for source in request.sources]}, {
            "input_tokens": 20, "output_tokens": 10, "estimated_cost": None, "duration_ms": 1}


async def seed(monkeypatch, *, wiki=True):
    suffix = uuid4().hex[:12]
    uid, wid, pid = (f"{kind}-{suffix}" for kind in ("wiki-user", "wiki-ws", "wiki-project"))
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=uid, username=uid, created_at=now, updated_at=now))
        await db.flush()
        db.add(Workspace(id=wid, owner_user_id=uid, name="Wiki test", created_at=now, updated_at=now))
        await db.flush()
        db.add(WorkspaceMember(user_id=uid, workspace_id=wid, role="owner", status="active", created_at=now, updated_at=now))
        db.add(Project(id=pid, user_id=uid, workspace_id=wid, name="Wiki test", created_at=now, updated_at=now))
        await db.execute(update(User).where(User.id == uid).values(default_workspace_id=wid))
    config = OpenBoxConfig(model="test/strong", memory={"wiki": wiki, "automatic_knowledge": False, "v2_write": True, "debug_view": True,
        "allowed_user_ids": [uid], "worker_lease_seconds": 90})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    note = await memory_service.create_note(user_id=uid, workspace_id=wid, project_id=pid, summary="这个项目使用中文答复。")
    return uid, wid, pid, note, config.memory


async def compile_one(seed, model=None, *, slug="project-agreement", title="项目约定"):
    uid, wid, pid, note, config = seed
    result = await service.schedule_compile(user_id=uid, workspace_id=wid, project_id=pid, slug=slug,
        title=title, memory_ids=[note["id"]], request_id=uuid4().hex, config=config)
    model = model or FakeModel()
    worker = MemoryWikiWorker(config, model=model)
    assert await worker.run_once()
    async with get_db_session() as db:
        job = await db.get(MemoryWikiJob, result["id"])
        assert job.status == "COMPLETED", job.last_error
        candidate = await db.get(MemoryWikiCandidate, job.candidate_id)
    return candidate, model


async def approve(seed, candidate, **overrides):
    uid, wid, _pid, _note, config = seed
    arguments = {"user_id": uid, "workspace_id": wid, "candidate_id": candidate.id,
        "candidate_revision": candidate.revision, "approved_hash": candidate.candidate_hash,
        "expected_target_revision": candidate.expected_target_revision,
        "expected_target_hash": candidate.expected_target_hash, "request_id": uuid4().hex, "config": config}
    arguments.update(overrides)
    return await service.approve_candidate(**arguments)


@pytest.mark.asyncio
async def test_flag_off_never_calls_a_model(monkeypatch):
    data = await seed(monkeypatch, wiki=False)
    with pytest.raises(service.WikiStateError, match="wiki_disabled"):
        await service.schedule_compile(user_id=data[0], workspace_id=data[1], project_id=data[2],
            slug="agreement", title="项目约定", config=data[4])
    model = FakeModel()
    assert not await MemoryWikiWorker(data[4], model=model).run_once()
    assert model.calls == 0


@pytest.mark.asyncio
async def test_worker_allowlist_does_not_settle_another_actors_exhausted_job(monkeypatch):
    allowed, outside = await seed(monkeypatch), await seed(monkeypatch)
    queued = []
    for data in (allowed, outside):
        queued.append(await service.schedule_compile(user_id=data[0], workspace_id=data[1], project_id=data[2],
            slug="agreement", title="项目约定", config=data[4]))
    async with get_db_session() as db:
        await db.execute(update(MemoryWikiJob).where(MemoryWikiJob.id == queued[1]["id"]).values(attempts=outside[4].max_attempts))
    lease = await claim_job("allowlisted-worker", allowed[4])
    assert lease.id == queued[0]["id"]
    async with get_db_session() as db:
        assert (await db.get(MemoryWikiJob, queued[1]["id"])).status == "PENDING"


@pytest.mark.asyncio
@pytest.mark.parametrize("occurred_at", [None, datetime(2026, 9, 30, 12, tzinfo=timezone.utc)])
async def test_candidate_requires_approval_then_indexes_and_unchanged_sources_skip_model(monkeypatch, occurred_at):
    data = await seed(monkeypatch)
    async with get_db_session() as db:
        await db.execute(update(MemorySource).where(MemorySource.user_id == data[0]).values(occurred_at=occurred_at))
    candidate, model = await compile_one(data)
    assert candidate.status == "PENDING"
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        assert await service.authorized_wiki_documents(db, scope, data[4]) == []

    page = await approve(data, candidate)
    assert page["status"] == "published" and page["body_available"]
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        docs = await service.authorized_wiki_documents(db, scope, data[4])
        assert len(docs) == 1 and docs[0].sources[0]["revision"] == 1
        assert docs[0].sources[0]["occurred_at"] == (occurred_at.isoformat() if occurred_at else None)
        assert document_matches_time(docs[0], resolve_query_time("2026-09-30 的决定", "Asia/Shanghai")) == (occurred_at is not None)
        assert await db.scalar(select(MemoryOutbox.id).where(MemoryOutbox.object_kind == "wiki", MemoryOutbox.operation == "UPSERT"))
        assert await db.scalar(select(MemoryDebugRun.id).where(MemoryDebugRun.user_id == data[0], MemoryDebugRun.request_id == "wiki:" + candidate.job_id))
    unchanged = await service.schedule_compile(user_id=data[0], workspace_id=data[1], project_id=data[2],
        slug="project-agreement", title="项目约定", memory_ids=[data[3]["id"]], config=data[4])
    assert unchanged["status"] == "unchanged" and unchanged["model_called"] is False
    assert model.calls == 1


@pytest.mark.asyncio
async def test_rejected_candidate_can_be_recompiled_as_new_candidate_using_validated_cache(monkeypatch):
    data = await seed(monkeypatch)
    candidate, model = await compile_one(data)
    await service.reject_candidate(user_id=data[0], workspace_id=data[1], candidate_id=candidate.id,
        candidate_revision=candidate.revision, approved_hash=candidate.candidate_hash)
    replacement, _ = await compile_one(data, model=model)
    assert replacement.id != candidate.id and replacement.job_id != candidate.job_id
    assert replacement.status == "PENDING" and model.calls == 1
    assert replacement.usage["input_tokens"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["body", "source_manifest"])
async def test_body_or_dependency_replacement_invalidates_old_approval(monkeypatch, field):
    data = await seed(monkeypatch)
    candidate, _ = await compile_one(data)
    async with get_db_session() as db:
        stored = await db.get(MemoryWikiCandidate, candidate.id)
        if field == "body":
            stored.draft = {**stored.draft, "body": "Replaced candidate"}
        else:
            stored.source_manifest = [{**stored.source_manifest[0], "content_hash": "0" * 64}]
    with pytest.raises(service.WikiStateError, match="wiki_candidate_changed"):
        await approve(data, candidate)


@pytest.mark.asyncio
async def test_source_correction_invalidates_candidate_and_published_derivative_in_same_transaction(monkeypatch):
    data = await seed(monkeypatch)
    candidate, _ = await compile_one(data)
    page = await approve(data, candidate)
    second, _ = await compile_one(data, slug="other-agreement")
    await memory_service.edit_note(user_id=data[0], workspace_id=data[1], memory_id=data[3]["id"],
                                   summary="这个项目改用英文答复。", expected_revision=data[3]["revision"])
    async with get_db_session() as db:
        stored = await db.get(MemoryWikiPage, page["id"])
        assert stored.status == "STALE" and stored.body is None
        assert (await db.get(MemoryWikiCandidate, second.id)).status == "STALE"
        assert await db.scalar(select(MemoryOutbox.id).where(MemoryOutbox.object_kind == "wiki",
            MemoryOutbox.object_id == page["id"], MemoryOutbox.operation == "DELETE"))
    with pytest.raises(service.WikiStateError, match="wiki_candidate_unavailable"):
        await approve(data, second)


@pytest.mark.asyncio
async def test_forgetting_source_memory_immediately_hides_wiki_even_before_worker(monkeypatch):
    data = await seed(monkeypatch)
    candidate, _ = await compile_one(data)
    page = await approve(data, candidate)
    await memory_service.delete_memory(user_id=data[0], workspace_id=data[1], memory_id=data[3]["id"],
                                        expected_revision=data[3]["revision"])
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        assert await service.authorized_wiki_documents(db, scope, data[4]) == []
    listed = await service.list_wiki(user_id=data[0], workspace_id=data[1], project_id=data[2])
    assert listed["pages"][0]["id"] == page["id"]
    assert listed["pages"][0]["body"] is None and listed["candidates"][0]["body"] is None


@pytest.mark.asyncio
async def test_late_compile_after_source_delete_cannot_create_a_candidate(monkeypatch):
    data = await seed(monkeypatch)
    queued = await service.schedule_compile(user_id=data[0], workspace_id=data[1], project_id=data[2],
        slug="agreement", title="项目约定", memory_ids=[data[3]["id"]], config=data[4])
    lease = await claim_job("late-worker", data[4])
    request = await read_request(lease, data[4])
    result = await compile_candidate(request, model=FakeModel())
    await memory_service.delete_memory(user_id=data[0], workspace_id=data[1], memory_id=data[3]["id"],
                                        expected_revision=data[3]["revision"])
    with pytest.raises(service.WikiStateError, match="wiki_source_changed"):
        await commit_candidate(lease, result, data[4])
    async with get_db_session() as db:
        assert await db.scalar(select(MemoryWikiCandidate.id).where(MemoryWikiCandidate.job_id == queued["id"])) is None


@pytest.mark.asyncio
async def test_target_created_after_snapshot_blocks_stale_candidate_approval(monkeypatch):
    data = await seed(monkeypatch)
    first, _ = await compile_one(data)
    second, _ = await compile_one(data, title="新的候选标题")
    await approve(data, first)
    with pytest.raises(service.WikiStateError, match="wiki_target_changed"):
        await approve(data, second)


@pytest.mark.asyncio
async def test_target_body_change_is_detected_even_without_revision_update(monkeypatch):
    data = await seed(monkeypatch)
    original, _ = await compile_one(data)
    page = await approve(data, original)
    candidate, _ = await compile_one(data, title="修订后的标题")
    async with get_db_session() as db:
        row = await db.get(MemoryWikiPage, page["id"])
        row.body = "Body replaced without updating hash"
    with pytest.raises(service.WikiStateError, match="wiki_target_changed"):
        await approve(data, candidate)


@pytest.mark.asyncio
async def test_acl_epoch_change_hides_old_body_and_prevents_approval(monkeypatch):
    data = await seed(monkeypatch)
    candidate, _ = await compile_one(data)
    await approve(data, candidate)
    other, _ = await compile_one(data, slug="second-page")
    async with get_db_session() as db:
        member = await db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == data[0], WorkspaceMember.workspace_id == data[1]))
        member.updated_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    with pytest.raises(service.WikiStateError, match="wiki_acl_changed"):
        await approve(data, other)
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        assert await service.authorized_wiki_documents(db, scope, data[4]) == []


@pytest.mark.asyncio
async def test_old_lease_fails_candidate_commit_after_reclaim(monkeypatch):
    data = await seed(monkeypatch)
    await service.schedule_compile(user_id=data[0], workspace_id=data[1], project_id=data[2], slug="agreement", title="项目约定", config=data[4])
    old = await claim_job("old", data[4])
    request = await read_request(old, data[4])
    result = await compile_candidate(request, model=FakeModel())
    async with get_db_session() as db:
        await db.execute(update(MemoryWikiJob).where(MemoryWikiJob.id == old.id).values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
    new = await claim_job("new", data[4])
    assert new.generation == old.generation + 1
    with pytest.raises(service.WikiStateError, match="wiki_lease_lost"):
        await commit_candidate(old, result, data[4])


@pytest.mark.asyncio
async def test_hash_mismatch_rejects_before_external_model_call(monkeypatch):
    data = await seed(monkeypatch)
    async with get_db_session() as db:
        source = await db.scalar(select(MemorySource).where(MemorySource.user_id == data[0]))
        source.body = "Source changed without versioning"
    with pytest.raises(service.WikiStateError, match="wiki_source_unavailable"):
        await service.schedule_compile(user_id=data[0], workspace_id=data[1], project_id=data[2], slug="agreement", title="项目约定", config=data[4])


@pytest.mark.asyncio
async def test_shared_source_body_cannot_regenerate_a_forgotten_fact_from_other_summary(monkeypatch):
    data = await seed(monkeypatch)
    original = "我的保密代号是海风；回复语言使用中文。"
    ids = []
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        for key, summary in [("secret.codename", "我的保密代号是海风"), ("reply.language", "回复语言使用中文")]:
            row = await memory_service.create_candidate_in_session(db, access=scope, type="PREFERENCE", summary=summary,
                fact_key=key, sources=[{"body": original, "source_kind": "user_statement"}])
            ids.append(row.id)
    notes = [await memory_service.confirm_note(user_id=data[0], workspace_id=data[1], proposal_id=mid,
                expected_revision=1) for mid in ids]
    remaining = (data[0], data[1], data[2], notes[1], data[4])
    candidate, _ = await compile_one(remaining)
    page = await approve(remaining, candidate)
    await memory_service.delete_memory(user_id=data[0], workspace_id=data[1], memory_id=ids[0], expected_revision=2)
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        assert await service.authorized_wiki_documents(db, scope, data[4]) == []
        stored = await db.get(MemoryWikiPage, page["id"])
        assert stored.status == "STALE" and stored.body is None
    active = await memory_service.list_active_memories(user_id=data[0], workspace_id=data[1], project_id=data[2])
    assert any(row["id"] == ids[1] and row["summary"] == "回复语言使用中文" for row in active)
    with pytest.raises(service.WikiStateError, match="wiki_source_unavailable"):
        await service.schedule_compile(user_id=data[0], workspace_id=data[1], project_id=data[2], slug="new-agreement",
            title="项目约定", memory_ids=[ids[1]], config=data[4])
    listed = await service.list_wiki(user_id=data[0], workspace_id=data[1], project_id=data[2])
    assert listed["pages"][0]["body"] is None and listed["candidates"][0]["body"] is None


@pytest.mark.asyncio
async def test_same_workspace_members_cannot_read_compile_or_approve_other_users_wiki(monkeypatch):
    data = await seed(monkeypatch)
    candidate, _ = await compile_one(data)
    await approve(data, candidate)
    other = "wiki-other-" + uuid4().hex[:12]
    instant = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=other, username=other, default_workspace_id=data[1], created_at=instant, updated_at=instant))
        await db.flush()
        db.add(WorkspaceMember(user_id=other, workspace_id=data[1], role="member", status="active", created_at=instant, updated_at=instant))
    data[4].allowed_user_ids.append(other)
    assert await service.list_wiki(user_id=other, workspace_id=data[1]) == {"pages": [], "candidates": []}
    assert await service.get_job(user_id=other, workspace_id=data[1], job_id=candidate.job_id) is None
    assert await approve(data, candidate, user_id=other) is None
    with pytest.raises(service.WikiStateError, match="wiki_source_unavailable"):
        await service.schedule_compile(user_id=other, workspace_id=data[1], project_id=None,
            slug="agreement", title="Private memories", memory_ids=[data[3]["id"]], config=data[4])


@pytest.mark.asyncio
async def test_page_tombstone_hides_body_in_management_and_retrieval(monkeypatch):
    data = await seed(monkeypatch)
    candidate, _ = await compile_one(data)
    page = await approve(data, candidate)
    async with get_db_session() as db:
        db.add(MemoryTombstone(id="wiki-tombstone-" + uuid4().hex[:12], object_kind="wiki", object_id=page["id"],
            revision=page["revision"],
            user_id=data[0], workspace_id=data[1], project_id=data[2], content_hash=page["content_hash"],
            scope="MEMORY", purge_status="PENDING", deleted_at=datetime.now(timezone.utc)))
    listed = await service.list_wiki(user_id=data[0], workspace_id=data[1], project_id=data[2])
    assert listed["pages"][0]["body"] is None and not listed["pages"][0]["body_available"]
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        assert await service.authorized_wiki_documents(db, scope, data[4]) == []

    with pytest.raises(service.WikiStateError, match="wiki_target_deleted"):
        await service.schedule_compile(user_id=data[0], workspace_id=data[1], project_id=data[2],
            slug="project-agreement", title="项目约定", config=data[4])


@pytest.mark.asyncio
async def test_retrieval_reads_any_number_of_pages_in_the_same_statements(monkeypatch):
    from sqlalchemy import event
    from db.base import get_engine
    from memory import retrieval
    data = await seed(monkeypatch)

    async def publish(slug):
        candidate, _ = await compile_one(data, slug=slug, title=slug)
        return await approve(data, candidate)

    async def read():
        statements = []

        def count(*_args):
            statements.append(1)
        engine = get_engine().sync_engine
        async with get_db_session() as db:
            scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
            event.listen(engine, "after_cursor_execute", count)
            try:
                documents = await retrieval.authorized_documents(db, scope, data[4])
            finally:
                event.remove(engine, "after_cursor_execute", count)
        return sorted(doc.id for doc in documents if doc.kind == "wiki"), len(statements)

    first = await publish("page-0")
    one, one_count = await read()
    later = [await publish(f"page-{index}") for index in range(1, 4)]
    four, four_count = await read()
    assert one == [first["id"]] and four == sorted(page["id"] for page in [first, *later])
    # Pages, their memories, sources and tombstones are read a table at a time.
    assert four_count == one_count
    # Read-ahead rows are still checked like single reads: a tombstone hides one page.
    async with get_db_session() as db:
        db.add(MemoryTombstone(id="wiki-tombstone-" + uuid4().hex[:12], object_kind="wiki", object_id=later[0]["id"],
            revision=later[0]["revision"], user_id=data[0], workspace_id=data[1], project_id=data[2],
            content_hash=later[0]["content_hash"], scope="MEMORY", purge_status="PENDING",
            deleted_at=datetime.now(timezone.utc)))
    remaining, _ = await read()
    assert remaining == sorted(page["id"] for page in [first, *later[1:]])


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("MEMORY_WIKI_TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
@pytest.mark.parametrize("mutation", ["correct", "forget", "shared-forget"])
async def test_publish_and_source_mutation_are_serialized_with_no_stale_published_body(monkeypatch, mutation):
    data = await seed(monkeypatch)
    publishing_data = data
    if mutation == "shared-forget":
        shared_ids = []
        async with get_db_session() as db:
            scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
            for key, summary in [("secret.codename", "我的代号是海风"), ("reply.language", "回复语言使用中文")]:
                memory = await memory_service.create_candidate_in_session(db, access=scope, type="PREFERENCE", summary=summary,
                    fact_key=key, sources=[{"body": "我的代号是海风；回复语言使用中文。", "source_kind": "user_statement"}])
                shared_ids.append(memory.id)
        confirmed = [await memory_service.confirm_note(user_id=data[0], workspace_id=data[1], proposal_id=mid,
            expected_revision=1) for mid in shared_ids]
        publishing_data = (data[0], data[1], data[2], confirmed[1], data[4])
        data = (data[0], data[1], data[2], confirmed[0], data[4])
    candidate, _ = await compile_one(publishing_data)
    validated, allow_publish = asyncio.Event(), asyncio.Event()
    original_read = service.read_sources

    async def hold_after_validation(*args, **kwargs):
        result = await original_read(*args, **kwargs)
        if kwargs.get("lock"):
            validated.set()
            await asyncio.wait_for(allow_publish.wait(), timeout=5)
        return result

    monkeypatch.setattr(service, "read_sources", hold_after_validation)
    publishing = asyncio.create_task(approve(data, candidate))
    await asyncio.wait_for(validated.wait(), timeout=5)
    arguments = {"user_id": data[0], "workspace_id": data[1], "memory_id": data[3]["id"], "expected_revision": data[3]["revision"]}
    mutating = asyncio.create_task(memory_service.edit_note(**arguments, summary="Changed source") if mutation == "correct"
                                   else memory_service.delete_memory(**arguments))
    try:
        await asyncio.wait_for(asyncio.shield(mutating), timeout=0.1)
        pytest.fail("Source mutation passed the actor fence during approval")
    except asyncio.TimeoutError:
        pass
    finally:
        allow_publish.set()
    page, _ = await asyncio.wait_for(asyncio.gather(publishing, mutating), timeout=5)
    async with get_db_session() as db:
        stored = await db.get(MemoryWikiPage, page["id"])
        assert stored.status == "STALE" and stored.body is None
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        assert await service.authorized_wiki_documents(db, scope, data[4]) == []


@pytest.mark.asyncio
async def test_wiki_reader_paginates_searches_and_returns_current_provenance(monkeypatch):
    from memory.wiki import reader
    data = await seed(monkeypatch)
    first, model = await compile_one(data, slug="collaboration", title="协作约定")
    first_page = await approve(data, first)
    second, _ = await compile_one(data, slug="communication", title="沟通指南")
    second_page = await approve(data, second)
    identity = {"user_id": data[0], "workspace_id": data[1]}
    batch = await reader.library(**identity, limit=1)
    assert len(batch["pages"]) == 1 and batch["next_offset"] == 1
    next_batch = await reader.library(**identity, limit=1, offset=batch["next_offset"])
    assert {batch["pages"][0]["id"], next_batch["pages"][0]["id"]} == {first_page["id"], second_page["id"]}
    assert next_batch["next_offset"] is None
    assert "body" not in batch["pages"][0]
    assert "中文" in batch["pages"][0]["excerpt"]
    assert len((await reader.library(**identity, query="中文"))["pages"]) == 2
    assert len((await reader.library(**identity, query="协作"))["pages"]) == 1
    detail = await reader.page_detail(**identity, page_id=first_page["id"])
    assert detail["body_available"] and detail["source_details"][0]["body"] == "这个项目使用中文答复。"
    assert detail["source_details"][0]["revision"] == 1
    assert model.calls == 1  # All reader operations are free of model work.
    sources = await reader.compile_sources(**identity, project_id=data[2])
    assert sources["memories"][0]["id"] == data[3]["id"]
    assert sources["memories"][0]["source_characters"] == len("这个项目使用中文答复。")
    assert (await reader.compile_sources(**identity))["memories"] == []


@pytest.mark.asyncio
async def test_reader_rechecks_source_on_every_projection_without_worker_invalidation(monkeypatch):
    from memory.wiki import reader
    data = await seed(monkeypatch)
    candidate, _ = await compile_one(data, title="语言约定")
    page = await approve(data, candidate)
    identity = {"user_id": data[0], "workspace_id": data[1]}
    async with get_db_session() as db:
        await db.execute(update(MemorySource).where(MemorySource.id == candidate.source_manifest[0]["id"]).values(status="DELETED"))
    assert (await reader.library(**identity, query="中文"))["pages"] == []
    stale = await reader.library(**identity, status="stale")
    assert stale["pages"][0]["excerpt"] == "" and stale["pages"][0]["source_ids"] == []
    detail = await reader.page_detail(**identity, page_id=page["id"])
    assert detail["body"] is None and detail["source_details"] == [] and detail["paragraphs"] == []
    assert (await reader.compile_sources(**identity, project_id=data[2]))["memories"] == []


@pytest.mark.asyncio
async def test_reader_rejects_foreign_actor_and_deleted_project(monkeypatch):
    from memory.wiki import reader
    from memory.policy import MemoryAccessDenied
    data = await seed(monkeypatch)
    candidate, _ = await compile_one(data)
    page = await approve(data, candidate)
    other = await seed(monkeypatch)
    assert await reader.page_detail(user_id=other[0], workspace_id=other[1], page_id=page["id"]) is None
    assert (await reader.library(user_id=other[0], workspace_id=other[1]))["pages"] == []
    async with get_db_session() as db:
        await db.execute(update(Project).where(Project.id == data[2]).values(is_deleted=True))
    assert await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=page["id"]) is None
    with pytest.raises(MemoryAccessDenied):
        await reader.compile_sources(user_id=data[0], workspace_id=data[1], project_id=data[2])


@pytest.mark.asyncio
async def test_reader_excludes_tombstoned_pages_and_explicit_empty_selection(monkeypatch):
    from memory.wiki import reader
    data = await seed(monkeypatch)
    candidate, _ = await compile_one(data)
    page = await approve(data, candidate)
    identity = {"user_id": data[0], "workspace_id": data[1]}
    async with get_db_session() as db:
        db.add(MemoryTombstone(id="reader-tombstone-" + uuid4().hex[:12], object_kind="wiki", object_id=page["id"],
            revision=page["revision"], user_id=data[0], workspace_id=data[1], project_id=data[2],
            content_hash=page["content_hash"], scope="MEMORY", purge_status="PENDING", deleted_at=datetime.now(timezone.utc)))
    assert (await reader.library(**identity))["pages"] == []
    assert await reader.page_detail(**identity, page_id=page["id"]) is None
    with pytest.raises(service.WikiStateError, match="wiki_source_budget_exceeded"):
        await service.schedule_compile(**identity, project_id=data[2], slug="empty", title="Empty", memory_ids=[], config=data[4])


@pytest.mark.asyncio
async def test_wiki_reader_http_routes_are_authorized_read_only_and_not_cached(monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from memory.wiki.api import router
    from auth.middleware import get_current_user
    from auth.workspace import get_workspace
    data = await seed(monkeypatch)
    candidate, model = await compile_one(data)
    page = await approve(data, candidate)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": data[0], "workspace_id": data[1]}
    app.dependency_overrides[get_workspace] = lambda: data[1]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://wiki.test") as client:
        listing = await client.get("/api/memory-wiki/library", params={"project_id": data[2], "query": "中文"})
        assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
        assert listing.json()["pages"][0]["id"] == page["id"]
        detail = await client.get("/api/memory-wiki/pages/" + page["id"])
        assert detail.status_code == 200 and detail.json()["source_details"]
        assert (await client.get("/api/memory-wiki/pages/missing")).status_code == 404
        assert (await client.get("/api/memory-wiki/compile-sources", params={"project_id": "foreign"})).status_code == 404
        empty = await client.post("/api/memory-wiki/compile", json={"title": "Empty", "slug": "empty", "memory_ids": [], "confirm_cost": True})
        assert empty.status_code == 422
    assert model.calls == 1
