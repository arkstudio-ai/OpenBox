"""Consumer-grade recall: bounded cost, central facts first, one fact per item,
and topic pages only where they add something beyond the memory list."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from core.config import MemoryConfig, OpenBoxConfig
from db import base
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_wiki import MemoryWikiPage
from db.models.wiki_platform import WikiConcept, WikiMaintenancePolicy, WikiOrganizationRun
from memory import orchestrator, retrieval, service as memories
from memory.index.base import DocumentSnapshot
from memory.policy import resolve_access_scope
from memory.presentation import document_item
from memory.wiki import maintenance, reader, service as wiki
from memory.wiki.consumer_refresh import refresh_existing
from memory.wiki.organization_worker import WikiOrganizationWorker
from memory.wiki.worker import MemoryWikiWorker
from tests.unit.test_automatic_knowledge import Verifier
from tests.unit.test_memory_authority_v2 import authority_scope, identity  # noqa: F401
from tests.unit.test_memory_wiki import FakeModel, seed, wiki_database  # noqa: F401
from tests.unit.test_wiki_organization import ConceptModel


class QueryCounter:
    def __enter__(self):
        self.count = 0
        event.listen(base._engine.sync_engine, "before_cursor_execute", self._hit)
        return self

    def __exit__(self, *exc):
        event.remove(base._engine.sync_engine, "before_cursor_execute", self._hit)

    def _hit(self, *_args, **_kwargs):
        self.count += 1


async def reference_notes(scope, count, *, start=0):
    """Stored trivia: verified from chat, not about the person themselves."""
    ids = [(await memories.create_note(**identity(scope), summary=f"参考资料{index}：{index}号会议室可容纳{index}人"))["id"]
           for index in range(start, start + count)]
    async with get_db_session() as db:
        await db.execute(update(UserMemory).where(UserMemory.id.in_(ids)).values(type="REFERENCE", owner="SYSTEM_VERIFIED"))
    return ids


@pytest.mark.parametrize("authority_scope", ["sqlite", "postgres"], indirect=True)
async def test_recall_cost_does_not_grow_with_what_a_person_has_stored(authority_scope, monkeypatch):
    scope = authority_scope
    config = MemoryConfig(retrieval_v2=False, allowed_user_ids=[scope["user_id"]])
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(memory=config))
    counts = []
    for batch, start in ((20, 0), (100, 1000)):
        await reference_notes(scope, batch, start=start)
        async with get_db_session() as db:
            access = await resolve_access_scope(db, **identity(scope))
        with QueryCounter() as background:
            core = await orchestrator._stable_background(access, config)
        with QueryCounter() as search:
            found = await retrieval.search_memory(query="会议室可容纳多少人", **identity(scope), config=config)
        assert core["items"] and found["items"]
        counts.append((background.count, search.count))
    # 20 stored memories, then 120: the same handful of queries per turn.
    assert counts[0] == counts[1], counts
    assert counts[1][0] <= 12 and counts[1][1] <= 40, counts


@pytest.mark.parametrize("authority_scope", ["sqlite", "postgres"], indirect=True)
async def test_core_memories_keep_personal_facts_ahead_of_recent_trivia(authority_scope, monkeypatch):
    scope = authority_scope
    config = MemoryConfig(allowed_user_ids=[scope["user_id"]])
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(memory=config))
    allergy = await memories.create_note(**identity(scope), summary="我对花生过敏。")
    async with get_db_session() as db:
        row = await db.get(UserMemory, allergy["id"])
        row.updated_at = datetime.now(timezone.utc) - timedelta(days=200)
    await reference_notes(scope, 30)
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope))
    core = await orchestrator._stable_background(access, config)
    texts = [item["text"] for item in core["items"]]
    assert texts[0] == "我对花生过敏。"
    assert len(texts) == orchestrator.CORE_ITEM_LIMIT and core["budget"]["characters"] <= config.stable_context_max_chars


def test_turn_context_rides_on_the_newest_user_message_only():
    from agent.loop import _prepend_to_last_user
    messages = [{"role": "user", "content": "早"}, {"role": "assistant", "content": "早上好"},
                {"role": "user", "content": "帮我订会议室"}, {"role": "tool", "content": "{}"}]
    result = _prepend_to_last_user(messages, "<memory_context>\nX\n</memory_context>")
    assert result[0] == messages[0] and result[1] == messages[1] and result[3] == messages[3]
    assert result[2]["content"] == "<memory_context>\nX\n</memory_context>\n\n帮我订会议室"
    assert messages[2]["content"] == "帮我订会议室"


def test_model_view_keeps_tool_references_but_never_storage_identities():
    source = {"id": "src-1", "revision": 3, "content_hash": "c" * 64, "kind": "user_statement",
              "session_id": "sess-secret", "message_id": "msg-secret", "occurred_at": "2026-03-01T08:00:00+00:00"}
    memory = document_item(DocumentSnapshot("memory", "mem-1", 2, "我对花生过敏。", "user-secret", "ws-secret",
        "proj-secret", 1, "h" * 64, (source,), category="USER_PROFILE"))
    chunk_ref = {"id": "chunk-1", "revision": 1, "content_hash": "d" * 64, "kind": "document_chunk",
                 "document_id": "doc-secret", "filename": "入场说明.pdf", "page_id": "wiki-x", "start": 0, "end": 9,
                 "original_pages": [2]}
    chunk = document_item(DocumentSnapshot("source", "chunk-1", 1, "周三休馆。", "user-secret", "ws-secret", None, 1,
        "d" * 64, (chunk_ref,), confirmation_status="UPLOADED_DOCUMENT", category="DOCUMENT"))
    rendered = orchestrator.render_memory_context({"stable_background": {"items": [memory]},
        "items": [memory, chunk], "degraded_reasons": [], "time_context": {"hard_filter_applied": False}})
    material = json.loads(rendered.split("\n", 2)[2].split("\n</memory_context>")[0])
    assert material["core_memories"] == [{"kind": "memory", "id": "mem-1", "revision": 2, "text": "我对花生过敏。",
        "category": "user_profile", "sources": [{"id": "src-1", "revision": 3, "origin": "chat", "date": "2026-03-01"}]}]
    assert material["relevant_memories"] == [{"kind": "source", "id": "chunk-1", "revision": 1, "text": "周三休馆。",
        "sources": [{"id": "chunk-1", "revision": 1, "origin": "uploaded_file", "file": "入场说明.pdf", "pages": [2]}]}]
    for secret in ("user-secret", "ws-secret", "proj-secret", "sess-secret", "msg-secret", "doc-secret", "h" * 64, "c" * 64):
        assert secret not in rendered
    assert orchestrator.render_memory_context({"items": [], "stable_background": {"items": []}}) == ""


def test_one_fact_becomes_one_item_whichever_form_ranked_first():
    memory = {"kind": "memory", "id": "m", "text": "A", "sources": [{"id": "s1"}, {"id": "s2"}], "rank": 3}
    raw = {"kind": "source", "id": "s1", "text": "原话", "sources": [{"id": "s1"}], "rank": 1}
    page = {"kind": "wiki", "id": "w", "text": "页", "sources": [{"id": "s1"}, {"id": "s2"}], "rank": 2}
    wider = {"kind": "wiki", "id": "w2", "text": "更多", "sources": [{"id": "s1"}, {"id": "s9"}], "rank": 4}
    kept = retrieval._without_repeated_lineage([raw, page, memory, wider])
    assert [(item["id"], item["rank"]) for item in kept] == [("m", 1), ("w2", 2)]


async def organize(data):
    async with get_db_session() as db:
        await db.execute(update(WikiMaintenancePolicy).where(WikiMaintenancePolicy.user_id == data[0]).values(
            next_check_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    await maintenance.schedule_due(data[4])
    organizer = WikiOrganizationWorker(data[4], model=ConceptModel())
    compiler = MemoryWikiWorker(data[4], model=FakeModel(), verifier=Verifier())
    for _ in range(30):
        await organizer.run_once()
        await compiler.run_once()
    async with get_db_session() as db:
        policy = await db.scalar(select(WikiMaintenancePolicy).where(WikiMaintenancePolicy.user_id == data[0]))
        return policy, await db.get(WikiOrganizationRun, policy.last_run_id)


async def second_fact(data):
    return await memories.create_note(user_id=data[0], workspace_id=data[1], project_id=data[2],
                                      summary="代码注释也使用中文。")


@pytest.mark.asyncio
async def test_a_single_fact_gets_no_topic_page_until_related_facts_arrive(monkeypatch):
    data = await seed(monkeypatch)
    data[4].automatic_knowledge = True
    _, run = await organize(data)
    assert run.status == "COMPLETED", run.reason_code
    assert [page["status"] for page in run.result["pages"]] == ["skipped_low_support"]
    assert (await reader.library(user_id=data[0], workspace_id=data[1]))["pages"] == []
    groups = await reader.memory_groups(user_id=data[0], workspace_id=data[1], project_id=data[2])
    assert groups["groups"] and all(group["page_id"] is None for group in groups["groups"])
    await second_fact(data)
    _, run = await organize(data)
    assert run.status == "COMPLETED", run.reason_code
    pages = (await reader.library(user_id=data[0], workspace_id=data[1]))["pages"]
    assert len(pages) == 1 and pages[0]["source_count"] == 2


@pytest.mark.asyncio
async def test_one_fact_topic_retires_without_losing_text_and_returns_in_place(monkeypatch):
    data = await seed(monkeypatch)
    data[4].automatic_knowledge = True
    data[4].wiki_min_topic_memories = 1  # A page published under the earlier rule.
    policy, run = await organize(data)
    page_id = run.result["pages"][0]["page_id"]
    async with get_db_session() as db:
        body = (await db.get(MemoryWikiPage, page_id)).body
        concept = await db.scalar(select(WikiConcept).where(WikiConcept.page_id == page_id))
        concept.user_edited = True
    data[4].wiki_min_topic_memories = 2
    await refresh_existing(policy.id, data[4])
    async with get_db_session() as db:
        assert (await db.get(MemoryWikiPage, page_id)).status == "PUBLISHED"  # A renamed topic stays.
        (await db.get(WikiConcept, concept.id)).user_edited = False
    await refresh_existing(policy.id, data[4])
    async with get_db_session() as db:
        row = await db.get(MemoryWikiPage, page_id)
        assert row.status == "RETIRED" and row.invalidation_reason == "retired_low_support" and row.body == body
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        assert await wiki.authorized_wiki_documents(db, scope, data[4]) == []
    assert (await reader.library(user_id=data[0], workspace_id=data[1]))["pages"] == []
    detail = await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=page_id)
    assert detail["status"] == "retired" and not detail["body_available"] and detail["body"] is None
    await second_fact(data)
    await organize(data)
    async with get_db_session() as db:
        row = await db.get(MemoryWikiPage, page_id)
        assert row.status == "PUBLISHED" and "代码注释也使用中文" in row.body


@pytest.mark.asyncio
async def test_page_whose_facts_were_all_forgotten_leaves_the_library(monkeypatch):
    data = await seed(monkeypatch)
    data[4].automatic_knowledge = True
    data[4].wiki_min_topic_memories = 1
    policy, run = await organize(data)
    page_id = run.result["pages"][0]["page_id"]
    await memories.forget_memory(user_id=data[0], workspace_id=data[1], memory_id=data[3]["id"],
                                 expected_revision=data[3]["revision"], request_id=uuid4().hex)
    async with get_db_session() as db:
        assert (await db.get(MemoryWikiPage, page_id)).status == "STALE"
    await refresh_existing(policy.id, data[4])
    async with get_db_session() as db:
        row = await db.get(MemoryWikiPage, page_id)
        assert row.status == "RETIRED" and row.invalidation_reason == "retired_no_sources"
    assert (await reader.library(user_id=data[0], workspace_id=data[1]))["pages"] == []
