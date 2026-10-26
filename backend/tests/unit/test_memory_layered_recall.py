"""Regressions for compact personal core, channel fusion and shared authority reads."""
from copy import deepcopy
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from db.base import get_db_session
from db.models.memory import UserMemory
from memory import retrieval, service
from memory.index.base import DocumentSnapshot, IndexHit
from memory.orchestrator import _stable_background
from memory.policy import resolve_access_scope
import pytest
from memory.redaction import text_hash
from tests.unit.test_memory_authority_v2 import authority_scope, identity  # noqa: F401
from tests.unit.test_memory_retrieval_runtime import FakeEmbedding, runtime_env  # noqa: F401


def test_lexical_scores_share_a_corpus_across_storage_kinds():
    ranked = retrieval.rank_lexical_pool("budget", {
        "memory": [("m1", "budget"), ("m2", "unrelated note")],
        "wiki": [("w1", "budget")],
    }, 4)
    assert ranked["memory"][0][1] == ranked["wiki"][0][1]


async def test_frequent_task_feedback_cannot_crowd_out_lasting_personal_facts(runtime_env):
    scope, config, _, _ = runtime_env
    rows = []
    for text, kind, key, ttl in (
        ("只检查这一版草稿", "FEEDBACK", "personal.task.review", None),
        ("明天下午参加活动", "CONSTRAINT", "personal.schedule", datetime.now(timezone.utc) + timedelta(days=1)),
        ("用户喜欢拼模型", "USER_PROFILE", "personal.hobby.models", None),
        ("用户对乳制品过敏", "CONSTRAINT", "personal.diet", None),
        ("当前门店脚本待确认", "PROJECT_CONTEXT", "project.store.status", None),
    ):
        row = await service.create_note(**identity(scope), summary=text, fact_key=key)
        rows.append(row)
        async with get_db_session() as db:
            await db.execute(update(UserMemory).where(UserMemory.id == row["id"]).values(
                type=kind, ttl=ttl, hit_count=10000 if len(rows) < 3 else 0,
                last_hit_at=datetime.now(timezone.utc) if len(rows) < 3 else None))
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope))
    background = await _stable_background(access, config)
    assert {item["id"] for item in background["items"]} == {rows[2]["id"], rows[3]["id"]}
    # All original records remain available for task-specific retrieval.
    assert len(await service.list_active_memories(**identity(scope))) == 5


async def test_dense_partition_first_place_does_not_override_stronger_other_kind(runtime_env):
    scope, config, _, index = runtime_env
    config.rerank = False
    notes = [await service.create_note(**identity(scope), summary=text)
             for text in ("facts alpha", "facts beta")]
    docs = [DocumentSnapshot("memory", note["id"], 1, text, scope["user_id"],
            scope["workspace_id"], None, 1, text_hash(text), ())
            for note, text in zip(notes, ("facts alpha", "facts beta"))]
    docs.append(DocumentSnapshot("wiki", "weak-page", 1, "generic page", scope["user_id"],
                scope["workspace_id"], None, 1, text_hash("generic page"), ()))
    async def loader(db, access, settings, *, only=None):
        return [doc for doc in docs if only is None or (doc.kind, doc.id) in only]
    index.hits = [IndexHit(doc.kind, doc.id, 1, score)
                  for doc, score in zip(docs, (.95, .9, .1))]
    result = await retrieval.search_memory(query="unmatched", **identity(scope), config=config,
        embedding=FakeEmbedding(), index=index, document_loader=loader)
    assert [item["id"] for item in result["items"]] == [doc.id for doc in docs]


async def test_core_and_recall_share_reads_and_recheck_after_rerank(runtime_env, monkeypatch):
    scope, config, _, index = runtime_env
    row = await service.create_note(**identity(scope), summary="用户喜欢听爵士乐")
    original = retrieval.authorized_documents
    calls, checkpoints = [], []
    async def counted(db, access, settings, *, only=None):
        calls.append(only)
        return await original(db, access, settings, only=only)
    async def expire_after_first_authorization(query, texts, settings):
        assert checkpoints[0]["items"][0]["id"] == row["id"]
        async with get_db_session() as db:
            await db.execute(update(UserMemory).where(UserMemory.id == row["id"]).values(
                ttl=datetime.now(timezone.utc) - timedelta(seconds=1)))
        return [(i, .9) for i in range(len(texts))], {}
    monkeypatch.setattr(retrieval, "authorized_documents", counted)
    monkeypatch.setattr("memory.rerank.rerank", expire_after_first_authorization)
    result = await retrieval.search_memory(query="爵士乐", **identity(scope), config=config,
        embedding=FakeEmbedding(), index=index, include_background=True,
        on_background=lambda value: checkpoints.append(deepcopy(value)))
    assert len(calls) == 2  # shared pre-provider and post-provider checks
    assert result["items"] == result["stable_background"]["items"] == []
    assert checkpoints[-1]["items"] == []


async def test_ranked_checkpoint_survives_final_sql_timeout_without_weak_candidates(runtime_env, monkeypatch):
    scope, config, _, index = runtime_env
    good = await service.create_note(**identity(scope), summary="拼模型需要先分类零件")
    await service.create_note(**identity(scope), summary="拼模型购物小票")
    original = retrieval.authorized_documents
    checked, checkpoints = 0, []
    async def loader(db, access, settings, *, only=None):
        nonlocal checked
        checked += 1
        if checked == 2:
            await asyncio.Event().wait()
        return await original(db, access, settings, only=only)
    async def rank(query, texts, settings):
        return [(i, .9 if "分类" in text else .01) for i, text in enumerate(texts)], {}
    monkeypatch.setattr(retrieval, "authorized_documents", loader)
    monkeypatch.setattr("memory.rerank.rerank", rank)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(.3):
            await retrieval.search_memory(query="拼模型", **identity(scope), config=config,
                embedding=FakeEmbedding(), index=index,
                on_checkpoint=lambda value: checkpoints.append(deepcopy(value)))
    assert checked == 2
    assert [item["id"] for item in checkpoints[0]["items"]] == [good["id"]]
    assert checkpoints[0]["candidates"] == []
    # The irrelevant note and its raw source are both excluded.
    assert checkpoints[0]["rerank"]["filtered_count"] == 2
