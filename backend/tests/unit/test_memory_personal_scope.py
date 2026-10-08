"""A fact about the person, learned in one project, is the same fact in all of them.

Real Inbox turns, extraction receipts, jobs and SQL authority; only the
extraction and verification models are fakes.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from core import config as runtime_config
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemorySource, MemorySourceLink
from db.models.project import Project
from db.models.session import Session
from memory import service
from memory.extraction import MemoryExtractionWorker
from memory.policy import resolve_access_scope
from memory.retrieval import authorized_documents
from tests.unit.test_automatic_knowledge import Verifier
from tests.unit.test_memory_pipeline import _finish_turn, _seed, pipeline_database  # noqa: F401

SAID = "我喜欢简短的中文回复，这个项目需要严格测试。"
STYLE = "用户喜欢简短的中文回复。"
TESTING = "这个项目需要严格测试。"


def facts(frozen):
    body = frozen.sources[0]["body"]
    candidates = [{"type": "PREFERENCE", "summary": STYLE, "fact_key": "personal.reply_style", "confidence": 90,
                   "source_indexes": [0], "quotes": [{"source_index": 0, "quote": "我喜欢简短的中文回复"}]}]
    if "严格测试" in body:
        candidates.append({"type": "PROJECT_CONTEXT", "summary": TESTING, "fact_key": "project.testing",
                           "confidence": 90, "source_indexes": [0],
                           "quotes": [{"source_index": 0, "quote": "这个项目需要严格测试"}]})
    return {"candidates": candidates}


async def two_projects(monkeypatch):
    user, workspace, project_a, session_a = await _seed(monkeypatch)
    config = runtime_config.get_config().memory
    config.automatic_knowledge = True
    project_b, session_b = f"prj-{uuid4().hex[:12]}", f"ses-{uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(Project(id=project_b, user_id=user, workspace_id=workspace, name="B", created_at=now, updated_at=now))
        await db.flush()
        db.add(Session(id=session_b, user_id=user, workspace_id=workspace, project_id=project_b, model="test/model",
                       agent="build", status="idle", token_usage={}, tool_exposure_state={},
                       created_at=now, updated_at=now))
    return (user, workspace, project_a, session_a), (user, workspace, project_b, session_b), config


class NothingToRevise:
    """The reconciliation model: a restatement changes no existing memory."""
    def __init__(self):
        self.existing = []

    async def plan(self, *, existing, **_kwargs):
        self.existing.append(sorted(item["summary"] for item in existing.values()))
        return {"revisions": []}, {}


async def learn(seed, text=SAID, reconciler=None):
    await _finish_turn(seed, text=text)
    assert await MemoryExtractionWorker(extractor=facts, verifier=Verifier(),
                                        reconciler=reconciler or NothingToRevise()).run_once() == "SUCCEEDED"


async def visible(user, workspace, project, config):
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user, workspace_id=workspace, project_id=project)
        documents = await authorized_documents(db, scope, config)
    return ({doc.text for doc in documents if doc.kind == "memory"},
            {doc.text for doc in documents if doc.kind == "source"})


async def test_personal_fact_from_project_a_is_recalled_in_project_b_without_project_a_words(monkeypatch):
    a, b, config = await two_projects(monkeypatch)
    user, workspace = a[0], a[1]
    await learn(a)
    async with get_db_session() as db:
        rows = {row.value["summary"]: row for row in (await db.scalars(select(UserMemory).where(
            UserMemory.user_id == user, UserMemory.status == "ACTIVE"))).all()}
        assert rows[STYLE].project_id is None and rows[TESTING].project_id == a[2]
        source = await db.scalar(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == rows[STYLE].id))
        # The evidence keeps the project and chat it was said in.
        assert (source.project_id, source.session_id, source.body) == (a[2], a[3], SAID)
        # The assistant's memory read accepts that project evidence for the
        # personal fact, without reading project A's words from a personal view.
        from assistant.memory import _current
        everywhere = await resolve_access_scope(db, user_id=user, workspace_id=workspace, include_all_projects=True)
        item, bodies = await _current(db, everywhere, "main-session", rows[STYLE], {})
        assert item["project_id"] is None and [entry["project_id"] for entry in item["sources"]] == [a[2]]
        assert bodies == {None: STYLE, source.id: None}
    # Project B: the personal fact, but neither project A's fact nor its words.
    assert await visible(user, workspace, b[2], config) == ({STYLE}, set())
    assert await visible(user, workspace, a[2], config) == ({STYLE, TESTING}, {SAID})

    # Saying it again in project B is the same fact, not a second copy: it was
    # weighed against the personal memory (never project A's), then deduplicated.
    reconciler = NothingToRevise()
    await learn(b, text="我喜欢简短的中文回复", reconciler=reconciler)
    assert reconciler.existing == [[STYLE]]
    async with get_db_session() as db:
        same = (await db.scalars(select(UserMemory).where(UserMemory.user_id == user,
            UserMemory.fact_key == "personal.reply_style"))).all()
        assert [row.id for row in same] == [rows[STYLE].id] and same[0].status == "ACTIVE"


async def test_deleting_the_chat_withdraws_a_personal_memory_everywhere(monkeypatch):
    a, b, config = await two_projects(monkeypatch)
    user, workspace = a[0], a[1]
    await learn(a)
    assert await service.count_learned_from_session(user_id=user, workspace_id=workspace, session_id=a[3]) == 2
    async with get_db_session() as db:
        (await db.get(Session, a[3])).is_deleted = True
    assert await visible(user, workspace, b[2], config) == (set(), set())
    assert await visible(user, workspace, a[2], config) == (set(), set())
    listed, _ = await service.page_memories(user_id=user, workspace_id=workspace, include_all_projects=True)
    assert listed == []


async def test_deleting_the_project_withdraws_personal_facts_learned_there(monkeypatch):
    a, b, config = await two_projects(monkeypatch)
    user, workspace = a[0], a[1]
    await learn(a)
    async with get_db_session() as db:
        (await db.get(Project, a[2])).is_deleted = True
    # The owner no longer has the project the evidence was said in.
    assert await visible(user, workspace, b[2], config) == (set(), set())


async def test_forgetting_suppresses_old_evidence_in_both_the_personal_and_the_original_scope(monkeypatch):
    a, b, config = await two_projects(monkeypatch)
    user, workspace = a[0], a[1]
    await learn(a)
    async with get_db_session() as db:
        row = await db.scalar(select(UserMemory).where(UserMemory.user_id == user,
                                                       UserMemory.fact_key == "personal.reply_style"))
        said = await db.scalar(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == row.id))
    old = [{"source_kind": "user_statement", "body": said.body, "occurred_at": said.occurred_at,
            "session_id": said.session_id, "message_id": said.message_id, "part_id": "replayed"}]
    later = [{**old[0], "occurred_at": datetime.now(timezone.utc) + timedelta(minutes=5)}]
    assert await service.forget_memory(user_id=user, workspace_id=workspace, memory_id=row.id)

    async with get_db_session() as db:
        in_a = await resolve_access_scope(db, user_id=user, workspace_id=workspace, project_id=a[2])
        in_b = await resolve_access_scope(db, user_id=user, workspace_id=workspace, project_id=b[2])
        for evidence in (in_a, in_b):
            # Forgotten once, personally: old words from any project cannot bring it back...
            assert await service.is_candidate_suppressed(db, evidence.personal(), summary=STYLE,
                fact_key="personal.reply_style", sources=old, source_access=evidence)
            # ...while saying it again afterwards is new evidence.
            assert not await service.is_candidate_suppressed(db, evidence.personal(), summary=STYLE,
                fact_key="personal.reply_style", sources=later, source_access=evidence)

    # A fact forgotten while it was still stored in project A keeps suppressing
    # that project's old words when they now yield a personal fact.
    note = await service.create_note(user_id=user, workspace_id=workspace, project_id=a[2],
                                     summary="用户周末不处理工作。", fact_key="personal.weekend")
    await service.forget_memory(user_id=user, workspace_id=workspace, memory_id=note["id"])
    async with get_db_session() as db:
        in_a = await resolve_access_scope(db, user_id=user, workspace_id=workspace, project_id=a[2])
        in_b = await resolve_access_scope(db, user_id=user, workspace_id=workspace, project_id=b[2])
        earlier = [{**old[0], "occurred_at": datetime.now(timezone.utc) - timedelta(days=1)}]
        assert await service.is_candidate_suppressed(db, in_a.personal(), summary="用户周末不处理工作。",
            fact_key="personal.weekend", sources=earlier, source_access=in_a)
        assert not await service.is_candidate_suppressed(db, in_b.personal(), summary="用户周末不处理工作。",
            fact_key="personal.weekend", sources=earlier, source_access=in_b)
