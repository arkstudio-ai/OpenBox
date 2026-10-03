"""Provenance must survive every path without inferred semantic bindings."""
from copy import deepcopy
import json

import pytest

from db.base import get_db_session
from memory import orchestrator, retrieval, service
from memory.index.base import DocumentSnapshot
from memory.policy import resolve_access_scope
from memory.presentation import document_item, model_item
from tests.unit.test_memory_authority_v2 import authority_scope, identity  # noqa: F401
from tests.unit.test_memory_retrieval_runtime import runtime_env  # noqa: F401


@pytest.mark.parametrize("statement", [
    "周宁负责海盐项目，方舟负责山岚项目。",
    "Cedar's maintainer is Robin; the owner prefers asynchronous reviews.",
    "仅在纸质校样中使用大字号；屏幕版不适用。",
    "Nora said, ‘I use Rust’; the user's own language preference is unknown.",
])
def test_projection_preserves_claim_without_inventing_subjects(statement):
    source = {"id": "source-a", "revision": 3, "kind": "user_statement", "message_id": "message-a"}
    doc = DocumentSnapshot("memory", "memory-a", 4, statement, "owner-a", "workspace-a", "project-a",
                           2, "hash-a", (source,), category="REFERENCE", valid_to="2027-01-01")
    item = document_item(doc)
    assert item["text"] == statement
    assert item["storage_scope"] == {"user_id": "owner-a", "workspace_id": "workspace-a", "project_id": "project-a"}
    assert item["sources"] == [source] and item["valid_to"] == "2027-01-01"
    assert "subject" not in item and "role" not in item
    item["sources"][0]["kind"] = "forged"
    assert doc.sources[0]["kind"] == "user_statement"


async def test_background_search_and_refresh_keep_same_authoritative_metadata(runtime_env):
    scope, config, _, _ = runtime_env
    row = await service.create_note(**identity(scope), project_id=scope["p1"],
                                    summary="林悦只负责云杉项目；银杏项目负责人尚未确定。")
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope), project_id=scope["p1"])
        docs = await retrieval.authorized_documents(db, access, config)
    doc = next(doc for doc in docs if doc.kind == "memory" and doc.id == row["id"])
    expected = document_item(doc)
    background = await orchestrator._stable_background(access, config)
    assert background["items"] == [expected]
    detailed = retrieval._item(doc, {"rank": 1})
    assert {key: detailed[key] for key in expected} == expected
    original = {"items": [detailed], "stable_background": background}
    stale = deepcopy(original)
    for item in stale["items"] + stale["stable_background"]["items"]:
        item["storage_scope"]["project_id"] = "stale-project"
        item["category"] = "stale-category"
        item["sources"] = [{"id": "stale-source"}]
    refreshed = await orchestrator.refresh_memory_context(stale, access, config)
    for item in refreshed["items"] + refreshed["stable_background"]["items"]:
        assert {key: item[key] for key in expected} == expected
    rendered = orchestrator.render_memory_context(refreshed)
    material = json.loads(rendered.split("\n", 2)[2].split("\n</memory_context>")[0])
    # The model sees the same statement and tool references, without storage
    # identities or hashes; an item already in core is not repeated.
    assert material["core_memories"] == [model_item(expected)]
    assert material["relevant_memories"] == []
    for leaked in (scope["user_id"], scope["workspace_id"], "content_hash", "storage_scope"):
        assert leaked not in rendered
    assert original["stable_background"]["items"] == [expected]
