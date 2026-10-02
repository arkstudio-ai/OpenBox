"""Real SQL authority, incremental extraction, budget fences and crash recovery."""
from datetime import timedelta
import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from db.base import get_db_session
from db.models.memory_wiki import MemoryWikiCandidate, MemoryWikiJob, MemoryWikiPage
from db.models.wiki_platform import WikiConcept, WikiConceptBinding, WikiConceptExtraction, WikiOrganizationRun
from memory import service as memories
from memory.policy import resolve_access_scope
from memory.wiki import organization, service
from memory.wiki.organization_worker import WikiOrganizationWorker, claim, live
from memory.wiki.worker import MemoryWikiWorker
from tests.unit.test_memory_wiki import FakeModel, approve, seed, wiki_database  # noqa: F401


class ConceptModel:
    def __init__(self, before_return=None):
        self.calls = 0
        self.before_return = before_return

    async def extract(self, request):
        self.calls += 1
        if self.before_return:
            await self.before_return()
        return {"concepts": [{"title": "回复语言", "aliases": ["沟通语言"], "category": "项目约定",
            "description": "项目沟通使用的语言。", "evidence": [{"source_id": source.id, "quote": source.text}
                for source in request.sources], "relations": []}]}, {"input_tokens": 10, "output_tokens": 10}


async def start(data, *, budget=20, compile_pages=True):
    uid, wid, pid, _, config = data
    preview = await organization.preview(user_id=uid, workspace_id=wid, project_id=pid, config=config)
    return await organization.start(user_id=uid, workspace_id=wid, project_id=pid, input_hash=preview["input_hash"],
        request_id=uuid4().hex, max_model_calls=budget, compile_pages=compile_pages, config=config)


async def drain(data, run_id, extractor=None, compiler=None):
    extractor, compiler = extractor or ConceptModel(), compiler or FakeModel()
    worker = WikiOrganizationWorker(data[4], model=extractor)
    page_worker = MemoryWikiWorker(data[4], model=compiler)
    for _ in range(60):
        progressed = await worker.run_once()
        progressed |= await page_worker.run_once()
        row = await organization.runs(user_id=data[0], workspace_id=data[1], run_id=run_id)
        if row["status"] in {"completed", "partial", "paused", "cancelled", "failed"}:
            return row, extractor, compiler
        if not progressed:
            await asyncio.sleep(0.1)
    raise AssertionError(row)


@pytest.mark.asyncio
async def test_two_sources_become_one_reviewable_topic_and_unchanged_run_reuses_everything(monkeypatch):
    data = await seed(monkeypatch)
    await memories.create_note(user_id=data[0], workspace_id=data[1], project_id=data[2], summary="团队的沟通语言是中文。")
    run = await start(data)
    result, extractor, compiler = await drain(data, run["id"])
    assert result["status"] == "completed", result
    assert result["model_calls"] == 3 and extractor.calls == 2 and compiler.calls == 1
    assert result["concept_count"] == 1
    listing = await organization.concepts(user_id=data[0], workspace_id=data[1], project_id=data[2])
    assert len(listing["concepts"]) == 1 and len(listing["concepts"][0]["memory_ids"]) == 2
    concept_id = listing["concepts"][0]["id"]
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        assert await service.authorized_wiki_documents(db, scope, data[4]) == []
        candidate = await db.get(MemoryWikiCandidate, result["pages"][0]["candidate_id"])
    page = await approve(data, candidate)
    second = await start(data)
    reused, _, _ = await drain(data, second["id"], extractor, compiler)
    assert reused["status"] == "completed" and reused["reused_count"] == 2
    assert reused["model_calls"] == 0 and reused["pages"][0]["status"] == "unchanged"
    assert extractor.calls == 2 and compiler.calls == 1
    listing = await organization.concepts(user_id=data[0], workspace_id=data[1], project_id=data[2])
    assert listing["concepts"][0]["id"] == concept_id and listing["concepts"][0]["page_id"] == page["id"]


@pytest.mark.asyncio
async def test_source_edit_hides_old_concept_and_only_reextracts_changed_memory(monkeypatch):
    data = await seed(monkeypatch)
    await memories.create_note(user_id=data[0], workspace_id=data[1], project_id=data[2], summary="技术名词保留英文。")
    run = await start(data)
    result, extractor, compiler = await drain(data, run["id"])
    async with get_db_session() as db:
        candidate = await db.get(MemoryWikiCandidate, result["pages"][0]["candidate_id"])
    page = await approve(data, candidate)
    await memories.edit_note(user_id=data[0], workspace_id=data[1], memory_id=data[3]["id"],
        summary="这个项目改用英文答复。", expected_revision=data[3]["revision"])
    stale = await organization.concepts(user_id=data[0], workspace_id=data[1], project_id=data[2])
    assert stale["concepts"][0]["title"] is None and stale["concepts"][0]["evidence"] == []
    preview = await organization.preview(user_id=data[0], workspace_id=data[1], project_id=data[2])
    assert preview["changed_count"] == 1 and preview["reused_count"] == 1
    again = await start(data)
    refreshed, _, _ = await drain(data, again["id"], extractor, compiler)
    assert refreshed["status"] == "completed", refreshed
    assert extractor.calls == 3 and compiler.calls == 2
    async with get_db_session() as db:
        assert (await db.get(MemoryWikiPage, page["id"])).status == "STALE"
        new_candidate = await db.get(MemoryWikiCandidate, refreshed["pages"][0]["candidate_id"])
        assert new_candidate.expected_target_revision == 1 and new_candidate.status == "PENDING"


@pytest.mark.asyncio
async def test_extraction_and_page_generation_share_a_resumable_actual_call_budget(monkeypatch):
    data = await seed(monkeypatch)
    run = await start(data, budget=1)
    paused, extractor, compiler = await drain(data, run["id"])
    assert paused["status"] == "paused" and extractor.calls == 1 and compiler.calls == 0
    assert paused["model_calls"] == 1
    async with get_db_session() as db:
        job = await db.get(MemoryWikiJob, paused["pages"][0]["job_id"])
        assert job.status == "PAUSED"
    await organization.act_run(user_id=data[0], workspace_id=data[1], run_id=run["id"],
        expected_revision=paused["revision"], action="resume", max_model_calls=2)
    result, _, _ = await drain(data, run["id"], extractor, compiler)
    assert result["status"] == "completed" and result["model_calls"] == 2
    assert extractor.calls == 1 and compiler.calls == 1


@pytest.mark.asyncio
async def test_late_model_result_cannot_reintroduce_corrected_material(monkeypatch):
    data = await seed(monkeypatch)
    async def correct():
        await memories.edit_note(user_id=data[0], workspace_id=data[1], memory_id=data[3]["id"],
            summary="已更正的资料。", expected_revision=data[3]["revision"])
    run = await start(data)
    result, extractor, compiler = await drain(data, run["id"], ConceptModel(correct))
    assert result["status"] == "cancelled" and result["reason_code"] == "wiki_organization_inputs_changed"
    assert extractor.calls == 1 and compiler.calls == 0
    async with get_db_session() as db:
        assert not await db.scalar(select(WikiConceptExtraction.id).where(WikiConceptExtraction.user_id == data[0]))
        assert not await db.scalar(select(WikiConcept.id).where(WikiConcept.user_id == data[0]))


@pytest.mark.asyncio
async def test_foreign_actor_cannot_see_or_resume_organization(monkeypatch):
    data = await seed(monkeypatch)
    run = await start(data)
    other = await seed(monkeypatch)
    assert await organization.runs(user_id=other[0], workspace_id=other[1], run_id=run["id"]) is None
    assert (await organization.concepts(user_id=other[0], workspace_id=other[1]))["concepts"] == []
    assert await organization.act_run(user_id=other[0], workspace_id=other[1], run_id=run["id"],
        expected_revision=run["revision"], action="cancel") is None


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_old_worker_is_fenced(monkeypatch):
    data = await seed(monkeypatch)
    run = await start(data, compile_pages=False)
    old = await claim("old-worker", data[4])
    async with get_db_session() as db:
        await db.execute(update(WikiOrganizationRun).where(WikiOrganizationRun.id == run["id"]).values(
            lease_until=service.now() - timedelta(seconds=1)))
    new = await claim("new-worker", data[4])
    assert new.id == old.id and new.generation == old.generation + 1
    async with get_db_session() as db:
        with pytest.raises(service.WikiStateError, match="lease_lost"):
            await live(db, old)
    with pytest.raises(service.WikiStateError, match="lease_lost"):
        await organization.reserve_call(old.id, generation=old.generation, owner=old.owner)
