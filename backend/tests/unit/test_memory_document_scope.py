"""Document-only capability: actual SQL, current versions, and no history fallback."""
from dataclasses import replace
from datetime import datetime

import pytest
from sqlalchemy import update

from db.base import get_db_session
from db.models.memory_document import MemoryDocument
from memory.packet import ReadBudget, ReadRequest, TurnContext, prepare_context, read_more, refresh_packet
from memory.read_contract import permits
from memory.providers.configured_read import decode
from memory.readers import read_request, revalidate_result
from memory.policy import resolve_access_scope
from tests.unit.test_memory_configured_read import wire_answer
from tests.unit.test_memory_documents import ingest
from tests.unit.test_memory_wiki import seed, wiki_database  # noqa: F401


async def setup(monkeypatch):
    data = await seed(monkeypatch)
    config = data[4]
    config.semantic_recall = config.retrieval_v2 = config.route_jev = True
    config.recall_deadline_seconds = 2
    turn = TurnContext(data[0], data[1], "只查2026-10-23上传的文档，告诉我文件名。", project_id=data[2],
        read_policy="documents_only", time_intent={"expression": "2026-10-23", "choice": "source_time", "confidence": 1})
    return data, config, turn


async def test_upload_discovery_includes_text_documents_without_media_rows_and_rechecks_acl(monkeypatch):
    from memory.readers import catalogs
    data, config, turn = await setup(monkeypatch)
    turn = replace(turn, query="之前上传的那份资料完整叫什么？", read_policy="allowed", time_intent=None)
    own, _ = await ingest(data, "# 拍摄说明\n镜头最长五秒。", filename="门店拍摄完整说明（确认版）.txt")
    foreign, _ = await ingest(data, "# 其他用户\n不该读到。", filename="other-private.txt")
    deleted, _ = await ingest(data, "# 已移除\n不该读到。", filename="removed.txt")
    async with get_db_session() as db:
        await db.execute(update(MemoryDocument).where(MemoryDocument.id == foreign.id).values(user_id="other-user"))
        await db.execute(update(MemoryDocument).where(MemoryDocument.id == deleted.id).values(status="DELETED"))
    for needs in ({"assets_useful": 1}, {"knowledge_useful": 1}):
        result = await catalogs(turn, needs, config)
        assert [(item["kind"], item["filename"]) for item in result] == [("document", own.filename)]
        assert "not a resolved reference" in result[0]["instruction"]
        assert "text" not in result[0]
    inventory = await read_request(ReadRequest(kind="documents"), turn, config)
    assert [item["id"] for item in inventory["items"]] == [own.id]
    async with get_db_session() as db:
        await db.execute(update(MemoryDocument).where(MemoryDocument.id == own.id).values(status="DELETED"))
    assert await catalogs(turn, {"assets_useful": 1}, config) == []


async def test_upload_window_fences_inventory_exact_sources_and_rewritten_search(monkeypatch):
    data, config, turn = await setup(monkeypatch)
    documents = []
    for name, at in (("before.md", "2026-10-22T15:59:59"), ("inside.md", "2026-10-22T16:00:00"),
                     ("after.md", "2026-10-23T16:00:00")):
        doc, _ = await ingest(data, f"# {name}\nOnly {name} supports this statement.", filename=name)
        async with get_db_session() as db:
            await db.execute(update(MemoryDocument).where(MemoryDocument.id == doc.id).values(created_at=datetime.fromisoformat(at)))
        documents.append(doc)
    result = await read_request(ReadRequest(kind="documents"), turn, config)
    assert [item["filename"] for item in result["items"]] == ["inside.md"]
    assert result["time_context"]["hard_filter_applied"]
    turn = replace(turn, read_time_context=result["time_context"])
    rewritten = await read_request(ReadRequest(kind="search", query="All chats and files from last month"), turn, config)
    assert rewritten["items"] == result["items"]
    refs = tuple({"id": doc.source_ids[0], "revision": doc.revision} for doc in documents)
    read = await read_more([ReadRequest(kind="sources", refs=refs)], turn, ReadBudget(), config)
    assert [item["available"] for item in read["results"][0]["items"]] == [False, True, False]
    assert "Only inside.md" in read["results"][0]["items"][1]["text"]
    arguments = {"reads": [{"kind": "sources", "refs": refs}]}
    assert await revalidate_result(arguments, read, turn, config) == read
    # Revocation after a model boundary cannot replay the previously read body.
    async with get_db_session() as db:
        await db.execute(update(MemoryDocument).where(MemoryDocument.id == documents[1].id).values(status="DELETED"))
    invalidated = await revalidate_result(arguments, read, turn, config)
    assert invalidated["reason"] == "evidence_changed"


@pytest.mark.parametrize("intent", [None, {"expression": "上个季度", "choice": "source_time", "confidence": 1},
    {"expression": "2026-10-23", "choice": "source_time", "confidence": .3}])
async def test_unknown_upload_window_never_turns_into_all_documents(monkeypatch, intent):
    data, config, turn = await setup(monkeypatch)
    await ingest(data, "# Private file\nMust not read without a resolved scope.")
    result = await read_request(ReadRequest(kind="documents"), replace(turn, time_intent=intent), config)
    assert result["status"] == "unavailable" and result["items"] == []


async def test_explicit_absence_of_time_condition_is_not_an_unresolved_date(monkeypatch):
    data, config, turn = await setup(monkeypatch)
    doc, _ = await ingest(data, "# 拍摄说明\n总时长十八秒。")
    turn = replace(turn, query="只读上传文档，不读聊天。", time_intent={
        "choice": "none", "expression": "", "confidence": 0})
    result = await read_request(ReadRequest(kind="documents"), turn, config)
    assert result["status"] == "complete" and result["items"][0]["id"] == doc.id
    uncertain = replace(turn, time_intent={"choice": "uncertain", "expression": "", "confidence": 0})
    assert (await read_request(ReadRequest(kind="documents"), uncertain, config))["status"] == "unavailable"


async def test_category_selection_keeps_document_evidence_out_of_personal_recall_and_vice_versa(monkeypatch):
    from memory.retrieval import search_memory
    from memory.service import create_note
    data, config, turn = await setup(monkeypatch)
    config.retrieval_v2 = config.rerank = False
    doc, _ = await ingest(data, "# 拍摄说明\n门店拍摄使用自然光，十八秒。")
    note = await create_note(user_id=turn.user_id, workspace_id=turn.workspace_id,
        project_id=turn.project_id, summary="门店拍摄时用户偏好蓝色围裙。")
    async def search(personal, knowledge):
        return await search_memory(query="门店拍摄", user_id=turn.user_id, workspace_id=turn.workspace_id,
            project_id=turn.project_id, config=config, retrieval_needs={"personal_useful": personal,
                "knowledge_useful": knowledge, "history_useful": 0, "assets_useful": 0, "experience_useful": 0})
    document_read = await search(0, 1)
    assert any(item["id"] in doc.source_ids for item in document_read["items"])
    assert all(item["id"] != note["id"] for item in document_read["items"])
    personal_read = await search(1, 0)
    assert any(item["id"] == note["id"] for item in personal_read["items"])
    assert all(item["id"] not in doc.source_ids for item in personal_read["items"])


async def test_document_scope_does_not_enable_legacy_history_or_personal_core(monkeypatch):
    data, config, turn = await setup(monkeypatch)
    await ingest(data, "# 文件\n门店镜头最多五秒。")
    async def forbidden(*args, **kwargs):
        pytest.fail("Document-only scope attempted a general memory/history read")
    monkeypatch.setattr("memory.orchestrator._stable_background", forbidden)
    monkeypatch.setattr("memory.retrieval.search_memory", forbidden)
    async def evaluate(*args):
        wire = {**wire_answer(), "p": ["只查2026-10-23上传的文档", "documents_only", 1],
                "d": ["2026-10-23", "filter_records", 1], "t": ["read", 1]}
        return {"classifier": "configured", "model": "test", "usage": {}, "answers": decode(wire, turn.query)}
    result = await prepare_context(turn, config, evaluator=evaluate)
    assert result["status"] == "complete"
    assert result["route"]["read_policy"] == "documents_only"
    assert result["items"] == result["stable_background"]["items"] == []
    assert "task_state" not in result
    assert result["_turn"].read_time_context == result["time_context"]
    for kind in ("history", "wiki", "experience", "assets", "task_state", "search", "sources"):
        assert not permits(turn, kind)
    assert permits(turn, "sources", bounded=True)
    denied = await read_more([ReadRequest(kind="history")], turn, ReadBudget(), config)
    assert denied["status"] == "restricted"
    # A changed clock must not rebuild the user's broad personal background.
    result["context_version"] = -1
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=turn.user_id, workspace_id=turn.workspace_id, project_id=turn.project_id)
    assert (await refresh_packet(result, scope, config))["stable_background"]["items"] == []


@pytest.mark.parametrize("confidence", [.2, .89])
async def test_low_confidence_document_permission_is_not_a_read_grant(monkeypatch, confidence):
    data, config, turn = await setup(monkeypatch)
    async def evaluate(*args):
        wire = {**wire_answer(), "p": ["只查2026-10-23上传的文档", "documents_only", confidence]}
        return {"classifier": "configured", "model": "test", "usage": {}, "answers": decode(wire, turn.query)}
    result = await prepare_context(turn, config, evaluator=evaluate)
    assert result["status"] == "unavailable"
    assert result["route"]["read_policy"] == "uncertain"
