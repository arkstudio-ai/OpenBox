"""Consolidation keeps every fact, rejects unrelated grouping and fences races."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_wiki import MemoryWikiPage
from db.models.wiki_platform import WikiConcept, WikiConceptBinding, WikiOrganizationRun
from memory import service as memories
from memory.policy import resolve_access_scope
from memory.wiki import organization, reader, service
from memory.wiki.consolidation import ConsolidationOutputError, validate_groups
from memory.wiki.organization_worker import WikiOrganizationWorker
from memory.wiki.worker import MemoryWikiWorker
from tests.unit.test_automatic_knowledge import Verifier, automatic_page
from tests.unit.test_memory_wiki import FakeModel, seed, wiki_database  # noqa: F401
from tests.unit.test_wiki_organization import start


class Topics:
    async def extract(self, request):
        title = "项目沟通约定" if "中文" in request.sources[0].text else "会议纪要的语言"
        return {"concepts": [{"title": title, "aliases": [], "category": "沟通", "description": title,
            "evidence": [{"source_id": s.id, "quote": s.text} for s in request.sources], "relations": []}]}, {}


class Consolidator:
    def __init__(self, before=None):
        self.before = before
        self.calls = 0

    async def generate_data(self, **kwargs):
        self.calls += 1
        topics = kwargs["data"]["topics"]
        target = next(t for t in topics if t["title"] == "项目沟通约定")
        if self.before:
            await self.before()
        return {"groups": [{"target_id": target["id"],
            "member_ids": [t["id"] for t in topics if t["id"] != target["id"]]}]}, {}


async def run_to_completion(data, *, consolidator=None, verifier=None, extractor=None, compiler_model=None, compiler_verifier=None):
    run = await start(data)
    organizer = WikiOrganizationWorker(data[4], model=extractor or Topics(),
        consolidator=consolidator or Consolidator(), verifier=verifier or Verifier())
    compiler = MemoryWikiWorker(data[4], model=compiler_model or FakeModel(), verifier=compiler_verifier or Verifier(False))
    for _ in range(40):
        await organizer.run_once()
        await compiler.run_once()
        async with get_db_session() as db:
            row = await db.get(WikiOrganizationRun, run["id"])
            if row.status in {"COMPLETED", "PARTIAL", "CANCELLED", "FAILED", "PAUSED"}:
                return row
    raise AssertionError("run did not finish")


async def two_facts(monkeypatch):
    data = await seed(monkeypatch)
    note = await memories.create_note(user_id=data[0], workspace_id=data[1], project_id=data[2],
        summary="会议纪要保留英文术语。")
    data[4].automatic_knowledge = True
    # These cases start from separate one-fact topic pages and then merge them.
    data[4].wiki_min_topic_memories = 1
    return data, note


@pytest.mark.asyncio
async def test_merge_combines_facts_and_sources_without_deleting_atomic_memories(monkeypatch):
    data, note = await two_facts(monkeypatch)
    run = await run_to_completion(data)
    assert run.status == "COMPLETED", run.reason_code
    assert run.result["concept_count"] == 1 and len(run.result["consolidated_ids"]) == 1
    listing = await reader.library(user_id=data[0], workspace_id=data[1])
    assert len(listing["pages"]) == 1
    page = await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=listing["pages"][0]["id"])
    assert "这个项目使用中文答复。" in page["body"] and note["summary"] in page["body"]
    assert len(page["memory_dependencies"]) == 2 and len(page["source_details"]) == 2
    async with get_db_session() as db:
        rows = (await db.scalars(select(UserMemory).where(UserMemory.user_id == data[0]))).all()
        assert len(rows) == 2 and all(r.status == "ACTIVE" for r in rows)
        target = await db.get(WikiConcept, run.result["concept_ids"][0])
        assert "会议纪要的语言" in target.aliases


@pytest.mark.asyncio
async def test_old_bookmark_redirects_and_old_body_is_preserved(monkeypatch):
    data, _ = await two_facts(monkeypatch)
    # First publish separately, then let the verified merger run.
    first = await run_to_completion(data, verifier=Verifier(False))
    assert first.result["concept_count"] == 2
    before = await reader.library(user_id=data[0], workspace_id=data[1])
    old = next(p for p in before["pages"] if p["title"] == "会议纪要的语言")
    async with get_db_session() as db:
        old_body = (await db.get(MemoryWikiPage, old["id"])).body
    second = await run_to_completion(data)
    assert second.status == "COMPLETED", second.reason_code
    redirected = await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=old["id"])
    assert redirected["id"] != old["id"] and redirected["body_available"]
    assert len((await reader.library(user_id=data[0], workspace_id=data[1]))["pages"]) == 1
    async with get_db_session() as db:
        row = await db.get(MemoryWikiPage, old["id"])
        assert row.status == "REDIRECT" and row.body == old_body
    with pytest.raises(service.WikiStateError, match="wiki_target_merged"):
        await service.schedule_compile(user_id=data[0], workspace_id=data[1], project_id=data[2],
            slug=old["slug"], title=old["title"], config=data[4])


@pytest.mark.asyncio
async def test_verifier_rejection_keeps_topics_separate(monkeypatch):
    data, _ = await two_facts(monkeypatch)
    run = await run_to_completion(data, verifier=Verifier(False))
    assert run.status == "COMPLETED" and run.result["concept_count"] == 2
    assert not run.result["consolidated_ids"]


@pytest.mark.asyncio
async def test_later_extraction_does_not_drop_facts_from_an_established_merged_article(monkeypatch):
    data, note = await two_facts(monkeypatch)
    first = await run_to_completion(data)
    async with get_db_session() as db:
        target = await db.get(WikiConcept, first.result["concept_ids"][0])
        page_id = target.page_id
    await memories.edit_note(user_id=data[0], workspace_id=data[1], memory_id=note["id"],
        expected_revision=note["revision"], summary="会议纪要保留法语术语。")

    class RenamedTopic(Topics):
        async def extract(self, request):
            value, usage = await super().extract(request)
            if "法语" in request.sources[0].text:
                value["concepts"][0]["title"] = "译文用词"
            return value, usage

    second = await run_to_completion(data, extractor=RenamedTopic(), verifier=Verifier(False))
    assert second.status == "COMPLETED", second.reason_code
    page = await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=page_id)
    assert "中文" in page["body"] and "法语" in page["body"] and "英文" not in page["body"]
    assert len(page["memory_dependencies"]) == 2
    # An old merge audit is not authority to resurrect a forgotten fact.
    async with get_db_session() as db:
        (await db.get(UserMemory, note["id"])).status = "DEPRECATED"
    third = await run_to_completion(data, verifier=Verifier(False))
    assert third.status == "COMPLETED", third.reason_code
    page = await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=page_id)
    assert "法语" not in page["body"] and len(page["memory_dependencies"]) == 1


@pytest.mark.asyncio
async def test_accurate_but_incomplete_merged_summary_falls_back_to_all_admitted_facts(monkeypatch):
    data, note = await two_facts(monkeypatch)
    class Incomplete(FakeModel):
        async def generate(self, request):
            value, usage = await super().generate(request)
            value["paragraphs"] = value["paragraphs"][:1]
            return value, usage
    class CoverageCheck:
        async def verify(self, items, **kwargs):
            assert items[-1]["required_facts"] and len(items[-1]["required_facts"]) == 2
            return [True] * (len(items) - 1) + [False], {}
    run = await run_to_completion(data, compiler_model=Incomplete(), compiler_verifier=CoverageCheck())
    assert run.status == "COMPLETED", run.reason_code
    page = await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=run.result["pages"][0]["page_id"])
    assert "这个项目使用中文答复。" in page["body"] and note["summary"] in page["body"]


@pytest.mark.asyncio
async def test_legacy_page_without_a_concept_is_included_in_consolidation(monkeypatch):
    data = await seed(monkeypatch)
    data[4].wiki_min_topic_memories = 1
    _, candidate = await automatic_page(data)
    old_id = candidate.target_page_id
    run = await run_to_completion(data)
    assert run.status == "COMPLETED", run.reason_code
    assert len(run.result["consolidated_ids"]) == 1
    page = await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=old_id)
    assert page["body_available"] and "中文" in page["body"]
    assert len((await reader.library(user_id=data[0], workspace_id=data[1]))["pages"]) == 1


@pytest.mark.asyncio
async def test_refresh_racing_with_merge_uses_full_current_topic_and_fences_old_scan(monkeypatch):
    from db.models.memory_wiki import MemoryWikiJob
    from db.models.wiki_platform import WikiMaintenancePolicy
    from memory.wiki import maintenance
    from memory.wiki.consumer_refresh import refresh_existing
    data, note = await two_facts(monkeypatch)
    await run_to_completion(data, verifier=Verifier(False))
    before = (await reader.library(user_id=data[0], workspace_id=data[1]))["pages"]
    target_before = next(p for p in before if p["title"] == "项目沟通约定")
    run = await start(data)
    worker = WikiOrganizationWorker(data[4], model=Topics(), consolidator=Consolidator(), verifier=Verifier())
    for _ in range(12):
        await worker.run_once()
        async with get_db_session() as db:
            current = await db.get(WikiOrganizationRun, run["id"])
            if current.result["phase"] == "scheduling":
                break
    assert current.result["consolidated_ids"]
    with pytest.raises(service.WikiStateError, match="wiki_target_changed"):
        await service.schedule_compile(user_id=data[0], workspace_id=data[1], project_id=data[2],
            slug=target_before["slug"], title=target_before["title"], memory_ids=[data[3]["id"]],
            expected_page_revision=target_before["revision"], config=data[4])
    await maintenance.discover_automatic(data[4])
    async with get_db_session() as db:
        policy = await db.scalar(select(WikiMaintenancePolicy).where(WikiMaintenancePolicy.user_id == data[0]))
    await refresh_existing(policy.id, data[4])
    async with get_db_session() as db:
        jobs = (await db.scalars(select(MemoryWikiJob).where(MemoryWikiJob.user_id == data[0],
            MemoryWikiJob.status == "PENDING"))).all()
        assert len(jobs) == 1
        assert {m["id"] for m in jobs[0].spec["memories"]} == {data[3]["id"], note["id"]}


@pytest.mark.asyncio
async def test_correction_during_merge_fences_plan(monkeypatch):
    data, note = await two_facts(monkeypatch)
    async def change():
        await memories.edit_note(user_id=data[0], workspace_id=data[1], memory_id=note["id"],
            expected_revision=note["revision"], summary="会议纪要全部改用法语。")
    run = await run_to_completion(data, consolidator=Consolidator(change))
    assert run.status == "CANCELLED" and run.reason_code == "wiki_organization_inputs_changed"
    async with get_db_session() as db:
        assert not await db.scalar(select(WikiConcept.id).where(WikiConcept.user_id == data[0], WikiConcept.status == "MERGED"))


@pytest.mark.asyncio
async def test_corrected_memory_keeps_topic_identity_but_unavailable_memory_does_not(monkeypatch):
    data, note = await two_facts(monkeypatch)
    await run_to_completion(data)
    await memories.edit_note(user_id=data[0], workspace_id=data[1], memory_id=note["id"],
        expected_revision=note["revision"], summary="会议纪要改用法语。")
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        catalog = await organization.model_catalog(db, scope, data[4])
        assert len(catalog) == 1 and catalog[0]["title"] == "项目沟通约定"
        for row in (await db.scalars(select(UserMemory).where(UserMemory.user_id == data[0]))).all():
            row.status = "DEPRECATED"
        await db.flush()
        assert await organization.model_catalog(db, scope, data[4]) == []


@pytest.mark.parametrize("group", [
    {"target_id": "foreign", "member_ids": ["b"]},
    {"target_id": "a", "member_ids": ["a"]},
    {"target_id": "a", "member_ids": ["b", "b"]},
    {"target_id": "a", "member_ids": ["edited"]},
])
def test_merge_rejects_forged_duplicate_or_user_edited_targets(group):
    records = [{"id": i, "user_edited": i == "edited"} for i in ("a", "b", "edited")]
    with pytest.raises(ConsolidationOutputError):
        validate_groups({"groups": [group]}, records)


class MalformedOnce(Consolidator):
    async def generate_data(self, **kwargs):
        value, usage = await super().generate_data(**kwargs)
        if self.calls == 1:
            value["groups"][0]["reason"] = "同一主题"  # an extra field breaks the contract
        return value, usage


@pytest.mark.asyncio
async def test_a_malformed_grouping_is_retried_rather_than_cancelling_the_run(monkeypatch):
    data, _ = await two_facts(monkeypatch)
    run = await start(data)
    consolidator = MalformedOnce()
    organizer = WikiOrganizationWorker(data[4], model=Topics(), consolidator=consolidator, verifier=Verifier())
    compiler = MemoryWikiWorker(data[4], model=FakeModel(), verifier=Verifier(False))
    reasons = set()
    for _ in range(40):
        await organizer.run_once()
        await compiler.run_once()
        async with get_db_session() as db:
            row = await db.get(WikiOrganizationRun, run["id"])
            reasons.add(row.reason_code)
            if row.status == "RETRY":
                row.available_at = service.now()  # skip the backoff
            if row.status in {"COMPLETED", "PARTIAL", "CANCELLED", "FAILED", "PAUSED"}:
                break
    assert "wiki_consolidation_invalid_response" in reasons
    assert row.status == "COMPLETED" and consolidator.calls == 2
    assert len(row.result["consolidated_ids"]) == 1
