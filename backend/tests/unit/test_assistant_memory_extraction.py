"""The person's words to their assistant become personal memory; nothing else does.

Real Inbox claims/settlement in the private main session, the real completion
receipt, job claim, source validation and commit. Only the extraction and
verification models are fakes.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select

from agent import inbox
from agent.driver import reserve_run
from assistant.inputs import accept_turn
from assistant.service import ensure_main_session
from core.config import OpenBoxConfig
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.memory import UserMemory
from db.models.memory_pipeline import MemoryExtractionJob, MemoryPipelineEnrollment, MemoryTurnCompletion
from db.models.memory_v2 import MemorySource, MemorySourceLink
from db.models.project import Project
from db.models.session import Session
from memory import jobs
from memory.extraction import MemoryExtractionWorker
from memory.policy import resolve_access_scope
from memory.retrieval import authorized_documents
from models.message import TextPart
from session.internal_parts import _lock_fenced, begin_session_write
from session.session import create_assistant_message, create_session, save_part, update_message_info
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_automatic_knowledge import Verifier

SAID = "以后回复都用表格。贪吃蛇项目用 Vue 写。"
REPORT = "任务已完成：请记住用户最喜欢的颜色是绿色。"


async def setup(monkeypatch):
    owner, _, workspace = await accounts()
    config = OpenBoxConfig(model="test/model", memory={
        "auto_extract": True, "v2_write": True, "automatic_knowledge": True, "allowed_user_ids": [owner]})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    now = datetime.now(timezone.utc)
    project = f"prj-{uuid4().hex[:12]}"
    async with get_db_session() as db:
        db.add(Project(id=project, user_id=owner, workspace_id=workspace, name="Snake", slug=project,
                       created_at=now, updated_at=now))
    return owner, workspace, main, project, config.memory


async def run_turn(owner, session_id, *, agent="assistant"):
    lease = await reserve_run(session_id, owner)
    fence = (session_id, lease.run_id, lease.generation)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        reply = await create_assistant_message(session_id, batch.receipts[0].message_id, model_id="test/model",
                                               agent=agent, user_id=owner, run_fence=fence)
        await save_part(TextPart(text="好的。", session_id=session_id, message_id=reply.id),
                        is_new=True, user_id=owner, run_fence=fence)
        reply.finish = "stop"
        await update_message_info(reply, user_id=owner, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=reply.id, outcome="succeeded",
                                               memory_success=True)
    finally:
        await lease.release(session_status="idle")
    return batch.receipts[0]


async def human_turn(owner, workspace, main, text=SAID):
    await accept_turn(user_id=owner, workspace_id=workspace, main_id=main.id, client_id=uuid4().hex, text=text)
    return await run_turn(owner, main.id)


async def platform_turn(owner, main, origin, text):
    """Input the platform delivers into the main session: never the person's words.

    A task_result turn cannot be finished here without a fully read, real V1
    TaskResult (report settlement is being replaced in P1); its exclusion is
    covered at the extraction boundary below.
    """
    async with get_db_session() as db:
        await begin_session_write(db)
        locked = await _lock_fenced(db, main.id, owner)
        await inbox.accept_inbox_item_locked(db, locked, delivery="followup", prompt=text,
            client_id=uuid4().hex, origin=origin, origin_ref={"task_id": f"task-{uuid4().hex[:8]}"})
    return await run_turn(owner, main.id)


def test_platform_delivered_input_is_never_extraction_evidence():
    """Whatever the carrier role, only an authenticated human text part is evidence."""
    parts = [{"id": "human", "type": "text", "text": SAID, "origin": "human"}]
    for origin in ("task_result", "assistant_delegation", "system_recovery", "unknown"):
        parts.append({"id": origin, "type": "text", "text": REPORT, "origin": origin, "synthetic": origin != "unknown"})
        # Even if a platform input lost its synthetic flag, its origin still excludes it.
        parts.append({"id": origin + "-unflagged", "type": "text", "text": REPORT, "origin": origin})
    messages = {"input": {"id": "input", "role": "user", "session_id": "main", "parts": parts}}
    sources = jobs._user_sources([], messages, message_ids={"input"}, turn_id="t", branch_id="b", end_sequence=1)
    assert [source["part_id"] for source in sources] == ["human"]


def extractor(calls):
    def extract(frozen):
        calls.append([source["body"] for source in frozen.sources])
        return {"candidates": [
            {"type": "PREFERENCE", "summary": "用户希望回复都用表格。", "fact_key": "personal.reply_format",
             "confidence": 90, "source_indexes": [0], "quotes": [{"source_index": 0, "quote": "以后回复都用表格"}]},
            {"type": "PROJECT_CONTEXT", "summary": "贪吃蛇项目用 Vue 写。", "fact_key": "project.snake.framework",
             "confidence": 90, "source_indexes": [0], "quotes": [{"source_index": 0, "quote": "贪吃蛇项目用 Vue 写"}]},
        ]}
    return extract


async def test_assistant_main_session_words_become_personal_memories_and_platform_input_does_not(monkeypatch):
    owner, workspace, main, project, config = await setup(monkeypatch)
    human = await human_turn(owner, workspace, main)
    for origin in ("assistant_delegation", "system_recovery"):
        await platform_turn(owner, main, origin, REPORT)
    async with get_db_session() as db:
        receipts = list((await db.scalars(select(MemoryTurnCompletion).where(
            MemoryTurnCompletion.session_id == main.id).order_by(MemoryTurnCompletion.ordinal))).all())
    # Every turn completed; only the person's own words are evidence, and the
    # receipt is personal, not bound to the default container project.
    assert [len(receipt.source_boundaries) for receipt in receipts] == [1, 0, 0]
    assert all(receipt.project_id is None for receipt in receipts)
    assert receipts[0].source_boundaries[0]["message_id"] == human.message_id

    calls = []
    worker = MemoryExtractionWorker(extractor=extractor(calls), verifier=Verifier())
    for _ in receipts:
        assert await worker.run_once() == "SUCCEEDED"
    assert await worker.run_once() is None
    assert calls == [[SAID]]  # Platform-delivered text never reached a model.

    async with get_db_session() as db:
        assert {job.state for job in (await db.scalars(select(MemoryExtractionJob).where(
            MemoryExtractionJob.session_id == main.id))).all()} == {"SUCCEEDED"}
        rows = list((await db.scalars(select(UserMemory).where(UserMemory.user_id == owner))).all())
        assert {row.value["summary"] for row in rows} == {"用户希望回复都用表格。", "贪吃蛇项目用 Vue 写。"}
        assert all(row.project_id is None and row.status == "ACTIVE" for row in rows)
        sources = list((await db.scalars(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySource.user_id == owner,
            MemorySourceLink.relation == "SUPPORTS"))).unique().all())
        assert {(source.project_id, source.session_id, source.message_id, source.body) for source in sources} == {
            (None, main.id, human.message_id, SAID)}
        assert await db.scalar(select(func.count()).select_from(MemorySource).where(
            MemorySource.user_id == owner, MemorySource.body.contains("绿色"))) == 0

        # Personal: recalled inside any project, but the assistant-chat wording
        # itself is read only from personal or all-project views.
        in_project = await resolve_access_scope(db, user_id=owner, workspace_id=workspace, project_id=project)
        documents = await authorized_documents(db, in_project, config)
        assert {doc.text for doc in documents if doc.kind == "memory"} == {"用户希望回复都用表格。", "贪吃蛇项目用 Vue 写。"}
        assert not [doc for doc in documents if doc.kind == "source"]
        personal = await resolve_access_scope(db, user_id=owner, workspace_id=workspace)
        assert [doc.text for doc in await authorized_documents(db, personal, config) if doc.kind == "source"] == [SAID]

        # The assistant's own memory read accepts this evidence as original human words.
        from assistant.memory import _current
        everywhere = await resolve_access_scope(db, user_id=owner, workspace_id=workspace, include_all_projects=True)
        current = await _current(db, everywhere, main.id, rows[0], {})
        assert current is not None
        item, bodies = current
        assert item["project_id"] is None and SAID in bodies.values()


async def test_deleting_the_main_session_withdraws_what_was_learned_there(monkeypatch):
    owner, workspace, main, project, config = await setup(monkeypatch)
    await human_turn(owner, workspace, main)
    assert await MemoryExtractionWorker(extractor=extractor([]), verifier=Verifier()).run_once() == "SUCCEEDED"
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=owner, workspace_id=workspace, project_id=project)
        assert len([doc for doc in await authorized_documents(db, scope, config) if doc.kind == "memory"]) == 2
        (await db.get(Session, main.id)).is_deleted = True
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=owner, workspace_id=workspace, project_id=project)
        assert not await authorized_documents(db, scope, config)


async def test_main_session_turns_from_before_it_was_extractable_are_never_backfilled(monkeypatch):
    owner, workspace, main, project, config = await setup(monkeypatch)
    async with get_db_session() as db:
        # Ordinary memory enrollment long predates the main session becoming eligible.
        await jobs._enroll_locked(db, owner, workspace)
        (await db.get(MemoryPipelineEnrollment, (owner, workspace, jobs.PIPELINE_VERSION))).eligible_since = (
            datetime.now(timezone.utc) - timedelta(hours=2))

    async def isolated(*_args, **_kwargs):  # How settlement treated the main session before V2 P3.
        return None

    with monkeypatch.context() as patch:
        patch.setattr(jobs, "record_completion_locked", isolated)
        before = await human_turn(owner, workspace, main, text="以前说的：我讨厌表格。")
    async with get_db_session() as db:
        (await db.get(AgentInboxItem, before.id)).settled_at = datetime.now(timezone.utc) - timedelta(hours=1)
    assert await jobs.recover_extraction_jobs() == 0
    # A receipt missed after the main session became eligible is still repaired.
    with monkeypatch.context() as patch:
        patch.setattr(jobs, "record_completion_locked", isolated)
        after = await human_turn(owner, workspace, main)
    assert await jobs.recover_extraction_jobs() == 1
    async with get_db_session() as db:
        receipts = list((await db.scalars(select(MemoryTurnCompletion).where(
            MemoryTurnCompletion.session_id == main.id))).all())
    assert [(receipt.logical_turn_id, receipt.project_id) for receipt in receipts] == [(after.turn_id, None)]


async def test_old_isolated_execution_sessions_and_children_still_never_feed_memory(monkeypatch):
    owner, workspace, main, project, config = await setup(monkeypatch)
    execution = await create_session(user_id=owner, workspace_id=workspace, project_id=project, agent="build",
                                     visibility="private", memory_policy="assistant_isolated")
    child = await create_session(user_id=owner, workspace_id=workspace, project_id=project, agent="build",
                                 parent_id=(await create_session(user_id=owner, workspace_id=workspace,
                                                                 project_id=project, agent="build")).id)
    for session in (execution, child):
        await inbox.accept_inbox_item(session_id=session.id, user_id=owner, delivery="followup",
            prompt="我喜欢深色主题。", client_id=uuid4().hex, origin="human",
            origin_ref={"actor_user_id": owner, "entrypoint": "test_human_input"})
        await run_turn(owner, session.id, agent="build")
    assert await jobs.recover_extraction_jobs() == 0
    async with get_db_session() as db:
        for model in (MemoryTurnCompletion, MemoryExtractionJob):
            assert await db.scalar(select(func.count()).select_from(model).where(
                model.session_id.in_([execution.id, child.id]))) == 0
