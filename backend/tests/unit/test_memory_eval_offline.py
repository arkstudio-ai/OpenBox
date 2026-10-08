"""Offline memory quality gate over a fixed consumer persona.

Deterministic and model-free: keyword recall through full authorization, core
memory ranking, the explicit routing rules, and what the main model is shown.
Cases live in tests/fixtures/memory_eval/recall.json; add one whenever a recall
bug is fixed. The live extraction/verification harness is scripts/memory_eval.py.
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import update

from core.config import MemoryConfig, OpenBoxConfig
from db.base import get_db_session
from db.models.memory import UserMemory
from memory import orchestrator, retrieval, routing, service as memories
from memory.policy import resolve_access_scope
from tests.unit.test_memory_authority_v2 import authority_scope, identity  # noqa: F401

CASES = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "memory_eval" / "recall.json").read_text())
RECALL_AT = 5
MIN_RECALL = 0.9


async def persona(scope) -> dict[str, str]:
    """Create the persona; return memory text -> case key."""
    by_text = {}
    for memory in CASES["memories"]:
        note = await memories.create_note(**identity(scope), summary=memory["text"])
        async with get_db_session() as db:
            await db.execute(update(UserMemory).where(UserMemory.id == note["id"]).values(
                type=memory["type"], owner=memory["owner"]))
        by_text[memory["text"]] = memory["key"]
    for index in range(CASES["distractors"]):
        note = await memories.create_note(**identity(scope), summary=f"备忘{index}：第{index}次团购的快递单号尾号 {index:04d}")
        async with get_db_session() as db:
            await db.execute(update(UserMemory).where(UserMemory.id == note["id"]).values(
                type="REFERENCE", owner="SYSTEM_VERIFIED"))
    return by_text


@pytest.mark.parametrize("authority_scope", ["sqlite", "postgres"], indirect=True)
async def test_persona_recall_core_routing_and_payload(authority_scope, monkeypatch):
    scope = authority_scope
    config = MemoryConfig(retrieval_v2=False, route_jev=False, allowed_user_ids=[scope["user_id"]])
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(memory=config))
    by_text = await persona(scope)

    misses, rendered = [], []
    for case in CASES["recall"]:
        bundle = await retrieval.search_memory(query=case["query"], **identity(scope), config=config)
        top = [by_text.get(item["text"]) for item in bundle["items"][:RECALL_AT]]
        if case["expect"] not in top:
            misses.append((case["query"], case["expect"], top))
        rendered.append(orchestrator.render_memory_context({**bundle, "stable_background": {"items": []}}))
    recall = 1 - len(misses) / len(CASES["recall"])
    print(f"\nmemory-eval offline: keyword recall@{RECALL_AT} = {recall:.2f} over {len(CASES['recall'])} questions")
    assert recall >= MIN_RECALL, misses

    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope))
    core = await orchestrator._stable_background(access, config)
    core_keys = {by_text.get(item["text"]) for item in core["items"]}
    assert set(CASES["core_expected"]) <= core_keys, core_keys

    for case in CASES["routing"]:
        route = await routing.route_context_needs(case["utterance"], access, config)
        assert (route["memory"]["needed"], route["task"]["needed"]) == (case["memory"], case["task"]), case
        if "rule" in case:
            assert route.get("rule") == case["rule"], case

    for text in rendered:
        for secret in (scope["user_id"], scope["workspace_id"], "content_hash", "storage_scope", "message_id"):
            assert secret not in text
