"""Primary document selection respects relevance, live authority and budgets."""
from dataclasses import replace

import pytest

from memory import retrieval
from memory.index.base import DocumentSnapshot
from memory.redaction import text_hash
from tests.unit.test_memory_authority_v2 import authority_scope, identity  # noqa: F401
from tests.unit.test_memory_retrieval_runtime import runtime_env, FakeEmbedding  # noqa: F401


@pytest.mark.parametrize("fault", [None, "irrelevant", "revoked", "revised", "disabled"])
async def test_primary_file_not_displaced_by_topic_page(runtime_env, monkeypatch, fault):
    scope, config, _, index = runtime_env
    config = config.model_copy(update={"rerank_min_score": .4})
    def document(kind, name, body, category=None):
        return DocumentSnapshot(kind, name, 1, body, scope["user_id"], scope["workspace_id"],
            None, 1, text_hash(body), ({"id": name, "revision": 1,
            "kind": "document_chunk" if category else "user_statement"},), category=category)
    page = document("wiki", "derived", "Camera setup: review the former instructions.")
    primary = document("source", "file", "Camera setup: vertical frame, 24 seconds, each shot at most 6 seconds.", "DOCUMENT")
    reads = []
    async def load(db, access, settings, only=None):
        reads.append(only)
        docs = [page, primary]
        if len(reads) >= 3:
            if fault == "revoked": docs = [page]
            if fault == "revised": docs = [page, replace(primary, revision=2)]
        return [doc for doc in docs if only is None or (doc.kind, doc.id) in only]
    async def rank(query, texts, settings):
        # The topic page ranks higher, but the primary file is still relevant.
        positions = {text: position for position, text in enumerate(texts)}
        return [(positions[page.text], .9), (positions[primary.text], .1 if fault == "irrelevant" else .7)], {}
    monkeypatch.setattr("memory.rerank.rerank", rank)
    result = await retrieval.search_memory(query="Camera setup", **identity(scope), config=config,
        index=index, embedding=FakeEmbedding(), document_loader=load, force_rerank=True, limit=1,
        prefer_document_source=fault != "disabled")
    assert [item["id"] for item in result["items"]] == ["file" if fault is None else "derived"]
    if fault is None:
        assert result["items"][0]["text"] == primary.text
    if fault == "irrelevant":
        assert result["rerank"]["filtered_count"] == 1


@pytest.mark.parametrize("revoked", [False, True])
async def test_expired_optional_io_never_dispatches_and_keeps_authorized_fallback(runtime_env, monkeypatch, revoked):
    scope, config, _, index = runtime_env
    body = "Camera setup: vertical frame, 24 seconds."
    primary = DocumentSnapshot("source", "file", 1, body, scope["user_id"], scope["workspace_id"],
        None, 1, text_hash(body), ({"id": "file", "revision": 1, "kind": "document_chunk"},), category="DOCUMENT")
    reads = []
    async def load(db, access, settings, only=None):
        reads.append(only)
        return [] if revoked and only is not None else [primary]
    async def forbidden(*args, **kwargs):
        pytest.fail("An expired IO budget dispatched a provider request")
    from types import SimpleNamespace
    monkeypatch.setattr("memory.rerank.rerank", forbidden)
    diagnostics = {}
    result = await retrieval.search_memory(query="Camera setup", **identity(scope), config=config,
        index=index, embedding=SimpleNamespace(embed=forbidden), document_loader=load,
        force_rerank=True, io_budget_seconds=0, diagnostics=diagnostics)
    assert [item["id"] for item in result["items"]] == ([] if revoked else ["file"])
    assert "dense_deadline" in result["degraded_reasons"]
    assert result["rerank"]["called"] is False
    assert diagnostics["reused_authorized_pass"] is True
    assert len(reads) == 2  # discovery, then the current authority checkpoint


@pytest.mark.parametrize("primary_relevance", [.8, .1])
async def test_primary_source_gets_a_rerank_slot_before_duplicate_topics_fill_both_caps(
        runtime_env, monkeypatch, primary_relevance):
    scope, config, _, index = runtime_env
    config = config.model_copy(update={"semantic_recall": True, "merged_candidate_limit": 8,
        "rerank_max_documents": 2, "rerank_min_score": .4})
    def document(kind, name, body, category=None):
        return DocumentSnapshot(kind, name, 1, body, scope["user_id"], scope["workspace_id"],
            None, 1, text_hash(body), ({"id": name, "revision": 1,
            "kind": "document_chunk" if category else "user_statement"},), category=category)
    pages = [document("wiki", f"derived-{i}", f"Camera setup camera setup camera setup question {i}.")
             for i in range(12)]
    primary = document("source", "file", "Camera: use a phone in natural light. Each shot is at most six seconds.", "DOCUMENT")
    async def load(db, access, settings, only=None):
        return [doc for doc in [*pages, primary] if only is None or (doc.kind, doc.id) in only]
    calls = []
    async def rank(query, texts, settings):
        calls.append(texts)
        assert len(texts) == 2 and primary.text in texts
        return sorted([(i, primary_relevance if text == primary.text else .9)
                       for i, text in enumerate(texts)], key=lambda row: -row[1]), {}
    monkeypatch.setattr("memory.rerank.rerank", rank)
    result = await retrieval.search_memory(query="Camera setup", **identity(scope), config=config,
        index=index, embedding=FakeEmbedding(), document_loader=load, force_rerank=True, limit=1,
        prefer_document_source=True)
    assert len(calls) == 1
    assert (result["items"][0]["id"] == "file") is (primary_relevance >= .4)
    assert len(result["candidates"]) <= 8
