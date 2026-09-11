"""Real SQL transactions for human-input lifecycle, not an in-memory mock."""
import asyncio
import importlib
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import db.base as database
from db.models.message import Message
from db.models.part import Part
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from question import question as q
from question import runtime
from question.continuation import apply_answers, expire_questions, QuestionContinuationWorker


@pytest.fixture
async def state(tmp_path, monkeypatch):
    monkeypatch.setattr("session.status._abort_signals", {})
    monkeypatch.setattr("session.status._pending_aborts", {})
    url = os.environ.get("OBX_QUESTION_TEST_DATABASE_URL")
    admin = None
    schema = None
    if url:
        parsed = make_url(url)
        if parsed.host not in {"127.0.0.1", "localhost"} or parsed.database != "openbox_questions":
            raise ValueError("Question regressions require the isolated local openbox_questions database")
        admin = create_async_engine(url)
        schema = f"qtest_{uuid4().hex}"
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        @asynccontextmanager
        async def separate_worker_lock(_session_id):
            yield
        # Simulate independent workers: only PostgreSQL row locks, not the
        # process-local asyncio lock, can serialize these transactions.
        monkeypatch.setattr(runtime, "session_exposure_lock", separate_worker_lock)
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'questions.db'}")
    monkeypatch.setattr(database, "_engine", engine)
    monkeypatch.setattr(database, "_session_factory", async_sessionmaker(engine, expire_on_commit=False))
    def migrate(connection):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        # Exercise the shipped migration, not just the ORM's create_all.
        existing = [t for t in database.Base.metadata.sorted_tables
                    if t.name not in {"question_checkpoints", "session_executions"}]
        database.Base.metadata.create_all(connection, tables=existing)
        migration = importlib.import_module("db.migrations.versions.d9e1f3a5b7c2_durable_questions")
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        for model in (QuestionCheckpoint, SessionExecution):
            assert {c["name"] for c in inspect(connection).get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
    async with engine.begin() as connection:
        await connection.run_sync(migrate)
        await connection.execute(text("CREATE TABLE kv_store (key VARCHAR(512) PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP WITH TIME ZONE NOT NULL)"))
    events = []
    monkeypatch.setattr("bus.bus.publish", lambda event, data: events.append((event, data)))
    async with database.get_db_session() as db:
        from db.models.user import User
        from db.models.workspace import Workspace
        from db.models.project import Project
        db.add_all([User(id=uid, username=uid, created_at=runtime.now(), updated_at=runtime.now()) for uid in ("u1", "u2")])
        await db.flush()
        db.add(Workspace(id="w1", name="Test workspace", owner_user_id="u1", created_at=runtime.now(), updated_at=runtime.now()))
        await db.flush()
        db.add(Project(id="p1", name="Test project", user_id="u1", workspace_id="w1", created_at=runtime.now(), updated_at=runtime.now()))
        await db.flush()
        for sid, user in (("s1", "u1"), ("s2", "u2")):
            db.add(Session(id=sid, user_id=user, workspace_id="w1", project_id="p1", title="Regression test",
                           agent="build", status="idle", created_at=runtime.now(), updated_at=runtime.now()))
    try:
        yield events
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def checkpoint(*, part_id="p1", questions=None, continuation=None, tool="question", expires_at=None):
    async with database.get_db_session() as db:
        message_id = f"m-{part_id}"
        db.add(Message(id=message_id, session_id="s1", user_id="u1", role="assistant",
                       finish="waiting_input", created_at=runtime.now()))
        await db.flush()
        db.add(Part(id=part_id, session_id="s1", message_id=message_id, user_id="u1", type="tool",
                    data={"id": part_id, "type": "tool", "tool": tool, "status": "running"}, created_at=runtime.now()))
    with pytest.raises(q.QuestionSuspended) as suspended:
        await asyncio.wait_for(q.ask("s1", questions or [q.Question(question="Choose?", options=[q.QuestionOption(label="Yes")])],
                                    {"messageID": message_id, "callID": part_id}, "u1",
                                    continuation=continuation, expires_at=expires_at), timeout=1)
    return suspended.value.request_id


async def read(model, key):
    async with database.get_db_session() as db:
        return await db.get(model, key)


async def test_ask_persists_and_returns_without_a_waiter(state):
    request_id = await checkpoint()
    pending = await q.list_pending("u1")
    assert [item.id for item in pending] == [request_id]
    assert (await read(Session, "s1")).status == "waiting_input"
    assert (await read(Part, "p1")).data["status"] == "waiting_input"
    assert (await read(SessionExecution, "s1")).run_id is None
    assert not hasattr(q, "_pending")


async def test_waiting_releases_execution_and_quota(state):
    ticket = await runtime.start_run("s1", "u1")
    token = runtime.current_run.set(ticket)
    try:
        await checkpoint()
        assert (await read(Session, "s1")).status == "busy"
        await runtime.finish_run(ticket)
    finally:
        runtime.current_run.reset(token)
    assert (await read(Session, "s1")).status == "waiting_input"
    assert (await read(SessionExecution, "s1")).run_id is None
    from db.repository.session_repo import PgSessionRepo
    assert await PgSessionRepo().count_busy("u1") == 0


async def test_answer_is_durable_idempotent_and_completed_tool_not_replayed(state):
    request_id = await checkpoint()
    await q.reply(request_id, [["Yes"]], "u1")
    assert (await read(SessionExecution, "s1")).resume_pending
    assert (await read(Session, "s1")).status == "queued"
    assert await q.list_pending("u1") == []
    await q.reply(request_id, [["Yes"]], "u1")
    with pytest.raises(q.QuestionConflict):
        await q.reply(request_id, [["No"]], "u1")
    generation = await apply_answers("s1", "u1")
    assert generation == 0
    part = await read(Part, "p1")
    assert part.data["status"] == "completed"
    assert part.data["metadata"]["answers"] == [["Yes"]]
    before = len([e for e in state if e[0] == "part.updated"])
    await apply_answers("s1", "u1")
    assert len([e for e in state if e[0] == "part.updated"]) == before
    ticket = await runtime.start_run("s1", "u1", expected_generation=generation)
    assert ticket
    assert await runtime.start_run("s1", "u1", expected_generation=generation) is None


async def test_parallel_questions_resume_only_after_all_answers(state):
    first = await checkpoint()
    second = await checkpoint(part_id="p2")
    await q.reply(first, [["Yes"]], "u1")
    assert await apply_answers("s1", "u1") is None
    assert (await read(Session, "s1")).status == "waiting_input"
    await q.reply(second, [["Yes"]], "u1")
    assert await apply_answers("s1", "u1") == 0


async def test_answer_while_run_finishes_does_not_launch_concurrently(state):
    ticket = await runtime.start_run("s1", "u1")
    request_id = await checkpoint()
    await q.reply(request_id, [["Yes"]], "u1")
    assert await apply_answers("s1", "u1") is None
    await runtime.finish_run(ticket)
    assert (await read(Session, "s1")).status == "queued"
    assert await apply_answers("s1", "u1") == 0


async def test_new_message_supersedes_old_ask_even_without_new_ask(state):
    request_id = await checkpoint()
    from session.session import create_user_message
    await create_user_message("s1", "New requirements", user_id="u1")
    assert (await read(QuestionCheckpoint, request_id)).status == "superseded"
    assert (await read(Part, "p1")).data["metadata"]["question_status"] == "superseded"
    with pytest.raises(q.QuestionGone, match="superseded"):
        await q.reply(request_id, [["Yes"]], "u1")
    assert await q.list_pending("u1") == []


async def test_failed_message_transaction_keeps_question(state):
    request_id = await checkpoint()
    with pytest.raises(RuntimeError):
        async with runtime.transaction("s1", "u1") as (db, _, execution):
            await runtime.invalidate_locked(db, execution)
            raise RuntimeError("message insert failed")
    assert (await read(QuestionCheckpoint, request_id)).status == "pending"
    await q.reply(request_id, [["Yes"]], "u1")


async def test_cancelled_and_old_generation_cannot_resume(state):
    request_id = await checkpoint()
    await q.reply(request_id, [["Yes"]], "u1")
    await runtime.cancel_session("s1", "u1")
    assert not (await read(SessionExecution, "s1")).resume_pending
    assert await runtime.start_run("s1", "u1", expected_generation=0) is None
    with pytest.raises(q.QuestionGone):
        await q.reply(request_id, [["Yes"]], "u1")


async def test_old_run_cannot_create_questions_or_clear_new_run(state):
    first = await runtime.start_run("s1", "u1")
    from session.session import create_user_message
    await create_user_message("s1", "New input", user_id="u1")
    second = await runtime.start_run("s1", "u1")
    token = runtime.current_run.set(first)
    try:
        with pytest.raises(q.QuestionGone):
            await q.ask("s1", [q.Question(question="old")], user_id="u1")
        await runtime.finish_run(first)
    finally:
        runtime.current_run.reset(token)
    assert (await read(SessionExecution, "s1")).run_id == second.run_id


async def test_tenant_isolation_precedes_mutation(state):
    request_id = await checkpoint()
    assert await q.list_pending("u2") == []
    with pytest.raises(KeyError):
        await q.reply(request_id, [["Yes"]], "u2")
    with pytest.raises(KeyError):
        await q.reject(request_id, "u2")
    assert (await read(QuestionCheckpoint, request_id)).status == "pending"


async def test_multi_question_validation_and_draft_revision(state):
    questions = [q.Question(question="Duration", options=[q.QuestionOption(label="30s")]),
                 q.Question(question="Extras", multiple=True, custom=False,
                            options=[q.QuestionOption(label="Captions"), q.QuestionOption(label="Music")])]
    request_id = await checkpoint(questions=questions)
    for invalid in ([["30s"]], [["30s"], []], [["30s", "60s"], ["Music"]], [["30s"], ["Unknown"]]):
        with pytest.raises(ValueError):
            await q.reply(request_id, invalid, "u1")
    draft = [q.DraftAnswer(custom="45 seconds", use_custom=True), q.DraftAnswer(selected=["Music"])]
    saved = await q.save_draft(request_id, draft, 0, "u1")
    assert saved.draft_revision == 1
    assert (await q.list_pending("u1"))[0].draft == draft
    with pytest.raises(q.QuestionConflict):
        await q.save_draft(request_id, draft, 0, "u1")
    await q.reply(request_id, [["45 seconds"], ["Captions", "Music"]], "u1")
    with pytest.raises(q.QuestionGone):
        await q.save_draft(request_id, draft, 1, "u1")


async def test_expiry_never_approves(state):
    request_id = await checkpoint(expires_at=runtime.now() - timedelta(seconds=1))
    with pytest.raises(q.QuestionGone, match="expired"):
        await q.reply(request_id, [["Yes"]], "u1")
    await expire_questions()
    assert (await read(QuestionCheckpoint, request_id)).status == "expired"
    assert not (await read(SessionExecution, "s1")).resume_pending
    assert (await read(Session, "s1")).status == "idle"


async def test_new_worker_recovers_committed_answer(state, monkeypatch):
    request_id = await checkpoint()
    await q.reply(request_id, [["Yes"]], "u1")
    started = []
    async def fake_run(session_id, user_id, expected_generation):
        started.append((session_id, user_id, expected_generation))
        ticket = await runtime.start_run(session_id, user_id, expected_generation=expected_generation)
        await runtime.finish_run(ticket)
    monkeypatch.setattr("agent.loop.run_loop", fake_run)
    worker = QuestionContinuationWorker()
    await worker.tick()
    await asyncio.gather(*worker.runs.values())
    await worker.tick()
    assert started == [("s1", "u1", 0)]
    await worker.stop()


async def test_expired_pre_start_claim_redelivers_but_progress_is_not_replayed(state):
    request_id = await checkpoint()
    await q.reply(request_id, [["Yes"]], "u1")
    await apply_answers("s1", "u1")
    first = await runtime.start_run("s1", "u1", expected_generation=0)
    async with runtime.transaction("s1", "u1") as (_, _, execution):
        execution.lease_until = runtime.now() - timedelta(seconds=1)
    await runtime.recover_expired_runs()
    assert (await read(SessionExecution, "s1")).resume_pending
    second = await runtime.start_run("s1", "u1", expected_generation=0)
    assert first.run_id != second.run_id
    await runtime.still_current(second, progress=True)
    async with runtime.transaction("s1", "u1") as (_, _, execution):
        execution.lease_until = runtime.now() - timedelta(seconds=1)
    await runtime.recover_expired_runs()
    assert not (await read(SessionExecution, "s1")).resume_pending
    assert (await read(Session, "s1")).status == "error"


async def test_plan_confirmation_is_applied_once(state):
    request_id = await checkpoint(tool="plan_enter", continuation={"kind": "plan_enter"})
    await q.reply(request_id, [["Yes"]], "u1")
    await apply_answers("s1", "u1")
    await apply_answers("s1", "u1")
    assert (await read(Session, "s1")).agent == "plan"
    async with database.get_db_session() as db:
        rows = (await db.scalars(select(Message).where(Message.client_message_id == f"ask:{request_id}"))).all()
    assert len(rows) == 1


async def test_memory_confirmation_uses_saved_proposal_without_recreating_it(state):
    from db.models.memory import UserMemory
    from memory.service import PENDING_NOTE_TYPE, USER_NOTE_TYPE
    async with database.get_db_session() as db:
        db.add(UserMemory(id="memory-1", user_id="u1", workspace_id="w1", scope="LONG_TERM",
                          type=PENDING_NOTE_TYPE, value={"summary": "old"}, owner="SYSTEM_INFERRED",
                          status="CANDIDATE", created_at=runtime.now(), updated_at=runtime.now()))
    request_id = await checkpoint(tool="creator_context", continuation={"kind": "memory_proposal", "memory_id": "memory-1", "workspace_id": "w1"})
    await q.reply(request_id, [["My edited wording"]], "u1")
    await apply_answers("s1", "u1")
    await apply_answers("s1", "u1")
    memory = await read(UserMemory, "memory-1")
    assert memory.type == USER_NOTE_TYPE
    assert memory.value["summary"] == "My edited wording"


@pytest.mark.parametrize("question_count", [1, 2])
async def test_real_tool_processor_suspends_and_resumes_from_saved_result(state, monkeypatch, question_count):
    from types import SimpleNamespace
    from agent import processor
    from agent.agent import AgentDef
    from agent.hooks import ToolHooks
    from permission.permission import Rule
    from session.session import create_assistant_message, create_user_message
    from tool.question_tool import question_tool
    from tool.tool import ToolContext, ToolResult, define_tool
    from pydantic import BaseModel
    class EffectArgs(BaseModel):
        label: str
    effects = []
    async def effect(args, ctx):
        effects.append(args.label)
        return ToolResult(output="Already performed")
    effect_tool = define_tool("effect", description="test only", parameters=EffectArgs,
        execute=effect, sandbox_required=False, parallel_safe=False)
    user = await create_user_message("s1", "Ask me before proceeding", user_id="u1")
    ticket = await runtime.start_run("s1", "u1")
    token = runtime.current_run.set(ticket)
    async def stream(**kwargs):
        yield {"type": "tool_call", "tool": "effect", "call_id": "before", "invalid": False, "args": {"label": "before"}}
        for i in range(question_count):
            yield {"type": "tool_call", "tool": "question", "call_id": f"ask-{i}", "invalid": False,
                   "args": {"questions": [{"question": f"Duration {i}?", "options": [{"label": "30s"}]}]}}
        yield {"type": "tool_call", "tool": "effect", "call_id": "after", "invalid": False, "args": {"label": "after"}}
        yield {"type": "finish", "reason": "tool_calls", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", stream)
    assistant = await create_assistant_message("s1", user.id, user_id="u1")
    try:
        result = await processor.process_step(session_id="s1", user_id="u1", session=SimpleNamespace(),
            agent_def=AgentDef(name="build", description="test"), system=[], llm_messages=[],
            tools={"question": question_tool, "effect": effect_tool}, model_id="test/model",
            ctx=ToolContext(session_id="s1", user_id="u1", workspace_id="w1"),
            hooks=ToolHooks("s1", "u1", config_rules=[Rule(permission="*", pattern="*", action="allow")]),
            assistant_info=assistant, sandbox=None, abort=asyncio.Event(), doom_loop_history=[])
        assert result.finish_reason == "waiting_input"
        await runtime.finish_run(ticket)
    finally:
        runtime.current_run.reset(token)
    pending = await q.list_pending("u1")
    assert len(pending) == question_count
    assert effects == ["before"]
    part_id = pending[0].tool["callID"]
    assert (await read(Part, part_id)).data["status"] == "waiting_input"
    for question in pending:
        await q.reply(question.id, [["30s"]], "u1")
    assert await apply_answers("s1", "u1") == ticket.generation
    assert (await read(Part, part_id)).data["metadata"]["answers"] == [["30s"]]
    assert effects == ["before"]  # Applying answers never replays earlier tools.


async def test_waiting_question_is_not_restarted_by_a_bare_run_request(state):
    await checkpoint()
    assert await runtime.start_run("s1", "u1") is None


async def test_replacement_and_answer_race_cannot_revive_old_generation(state):
    from session.session import create_user_message
    request_id = await checkpoint()
    await asyncio.gather(q.reply(request_id, [["Yes"]], "u1"),
                         create_user_message("s1", "Changed requirements", user_id="u1"), return_exceptions=True)
    row = await read(QuestionCheckpoint, request_id)
    assert row.status == "superseded"
    assert await apply_answers("s1", "u1") is None
    assert not (await read(SessionExecution, "s1")).resume_pending


async def test_two_workers_claim_one_resume_and_quota_keeps_answer_queued(state, monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "max_concurrent_agents", 1)
    async with database.get_db_session() as db:
        db.add(Session(id="busy-session", user_id="u1", workspace_id="w1", project_id="p1",
            agent="build", status="busy", created_at=runtime.now(), updated_at=runtime.now()))
    request_id = await checkpoint()
    await q.reply(request_id, [["Yes"]], "u1")
    await apply_answers("s1", "u1")
    assert await runtime.start_run("s1", "u1", expected_generation=0) is None
    assert (await read(SessionExecution, "s1")).resume_pending
    async with database.get_db_session() as db:
        (await db.get(Session, "busy-session")).status = "idle"
    claims = await asyncio.gather(*(runtime.start_run("s1", "u1", expected_generation=0) for _ in range(2)))
    assert sum(ticket is not None for ticket in claims) == 1


async def test_shutdown_redelivers_only_claims_that_made_no_progress(state):
    request_id = await checkpoint()
    await q.reply(request_id, [["Yes"]], "u1")
    await apply_answers("s1", "u1")
    first = await runtime.start_run("s1", "u1", expected_generation=0)
    await runtime.finish_run(first, interrupted=True)
    assert (await read(SessionExecution, "s1")).resume_pending
    second = await runtime.start_run("s1", "u1", expected_generation=0)
    await runtime.still_current(second, progress=True)
    await runtime.finish_run(second, interrupted=True)
    assert not (await read(SessionExecution, "s1")).resume_pending
    assert (await read(Session, "s1")).status == "error"


async def test_transient_worker_failure_preserves_delivery_intent(state, monkeypatch):
    request_id = await checkpoint()
    await q.reply(request_id, [["Yes"]], "u1")
    async def temporary_failure(*args):
        raise RuntimeError("temporarily unavailable")
    monkeypatch.setattr("question.continuation.apply_answers", temporary_failure)
    worker = QuestionContinuationWorker()
    await worker.tick()
    execution = await read(SessionExecution, "s1")
    assert execution.resume_pending
    assert execution.next_attempt_at is not None
    assert (await read(QuestionCheckpoint, request_id)).answers == [["Yes"]]
    await worker.stop()


async def test_expired_lease_cannot_authorize_more_work(state):
    ticket = await runtime.start_run("s1", "u1")
    async with runtime.transaction("s1", "u1") as (_, _, execution):
        execution.lease_until = runtime.now() - timedelta(seconds=1)
    assert not await runtime.still_current(ticket, progress=True)


async def test_question_migration_can_round_trip_without_touching_history(state):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    migration = importlib.import_module("db.migrations.versions.d9e1f3a5b7c2_durable_questions")
    def round_trip(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            assert "question_checkpoints" not in inspect(connection).get_table_names()
            migration.upgrade()
    async with database._engine.begin() as connection:
        await connection.run_sync(round_trip)
    assert (await read(Session, "s1")).user_id == "u1"


async def test_upgrade_closes_legacy_cards_but_preserves_durable_waits(state):
    from question.legacy import reconcile_legacy_questions
    async with database.get_db_session() as db:
        db.add(Message(id="legacy-msg", session_id="s1", user_id="u1", role="assistant", created_at=runtime.now()))
        await db.flush()
        db.add(Part(id="legacy-part", session_id="s1", message_id="legacy-msg", user_id="u1", type="tool",
            data={"id": "legacy-part", "tool": "question", "status": "running",
                  "input": {"questions": [{"question": "Old approval?"}]}}, created_at=runtime.now()))
        (await db.get(Session, "s1")).status = "busy"
    assert await reconcile_legacy_questions() == 1
    assert (await read(Part, "legacy-part")).data["metadata"]["question_status"] == "expired"
    assert (await read(Session, "s1")).status == "error"
    pending = await checkpoint()
    assert await reconcile_legacy_questions() == 0
    assert (await read(QuestionCheckpoint, pending)).status == "pending"


async def test_deleting_session_cancels_pending_and_delivery(state, monkeypatch):
    from session.session import delete_session
    async def release(*args, **kwargs):
        pass
    monkeypatch.setattr("sandbox.sandbox_manager.release", release)
    request_id = await checkpoint()
    await q.reply(request_id, [["Yes"]], "u1")
    assert await delete_session("s1", "u1")
    assert not (await read(SessionExecution, "s1")).resume_pending
    assert (await read(QuestionCheckpoint, request_id)).status == "cancelled"
    assert await q.list_pending("u1") == []
    with pytest.raises(KeyError):
        await q.reply(request_id, [["Yes"]], "u1")


async def test_http_answer_and_draft_contracts_use_authenticated_durable_state(state):
    import httpx
    from fastapi import FastAPI
    from api.questions import router
    from auth.middleware import get_current_user
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    user_id = "u1"
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user_id}
    request_id = await checkpoint()
    path = f"/api/agent/question/{request_id}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local-test") as client:
        assert (await client.get("/api/agent/question")).json()[0]["id"] == request_id
        draft = {"draft": [{"selected": [], "custom": "30s", "use_custom": True}], "revision": 0}
        assert (await client.put(f"{path}/draft", json=draft)).json()["draft_revision"] == 1
        assert (await client.put(f"{path}/draft", json=draft)).status_code == 409
        assert (await client.post(path, json={"answers": [["Yes", "No"]]})).status_code == 422
        user_id = "u2"
        assert (await client.post(path, json={"answers": [["Yes"]]})).status_code == 404
        user_id = "u1"
        assert (await client.post(path, json={"answers": [["Yes"]]})).status_code == 200
        assert (await client.post(path, json={"answers": [["Yes"]]})).status_code == 200
        assert (await client.post(path, json={"answers": [["No"]]})).status_code == 409
        assert (await client.put(f"{path}/draft", json={**draft, "revision": 1})).status_code == 410
        assert (await client.get("/api/agent/question")).json() == []


async def test_modified_memory_proposal_requires_fresh_approval(state):
    from db.models.memory import UserMemory
    from memory.service import PENDING_NOTE_TYPE
    async with database.get_db_session() as db:
        db.add(UserMemory(id="changed-memory", user_id="u1", workspace_id="w1", scope="LONG_TERM",
            type=PENDING_NOTE_TYPE, value={"summary": "changed after asking"}, owner="SYSTEM_INFERRED",
            status="CANDIDATE", created_at=runtime.now(), updated_at=runtime.now()))
    request_id = await checkpoint(tool="creator_context", questions=[q.Question(question="Remember?", detail={"summary": "original"})],
        continuation={"kind": "memory_proposal", "memory_id": "changed-memory"})
    await q.reply(request_id, [["记住"]], "u1")
    with pytest.raises(ValueError, match="changed"):
        await apply_answers("s1", "u1")
    assert (await read(UserMemory, "changed-memory")).type == PENDING_NOTE_TYPE
    assert not (await read(QuestionCheckpoint, request_id)).applied


async def test_complete_loop_releases_wait_and_new_worker_continues_with_answer(state, monkeypatch):
    from agent import loop, processor
    from agent.agent import AgentDef
    from agent.tool_resolution import ResolvedStepTools
    from core.config import get_config
    from session.session import create_user_message, get_messages
    from tool.question_tool import question_tool
    config = get_config().model_copy(deep=True)
    config.tool_exposure.mode = "legacy_eager"
    config.model = "openai/gpt-4o"
    config.models = []
    config.permission = {"*": "allow"}
    config.jwt_secret = ""
    monkeypatch.setattr("core.config.get_config", lambda: config)
    async def no_sandbox(*args, **kwargs):
        return None
    async def system(*args, **kwargs):
        return ["Test conversation"]
    async def tools(*args, **kwargs):
        return ResolvedStepTools(tools={"question": question_tool}, catalogue_availability="available")
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", no_sandbox)
    monkeypatch.setattr("sandbox.entitlement.subscription_sandbox_enabled", lambda: False)
    monkeypatch.setattr(loop, "_build_system_prompt", system)
    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    monkeypatch.setattr(loop, "get_agent", lambda _name: AgentDef(name="build", description="test"))
    calls = []
    async def stream(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            name = next(iter(kwargs["tools"]))
            yield {"type": "tool_call", "tool": name, "call_id": "approval", "invalid": False,
                "args": {"questions": [{"question": "Duration?", "options": [{"label": "30s"}]}]}}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            yield {"type": "text_delta", "text": "Continuing with your 30s answer."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", stream)
    await create_user_message("s1", "Prepare a video; ask me for duration first", user_id="u1")
    await asyncio.wait_for(loop.run_loop("s1", user_id="u1"), timeout=5)
    assert (await read(Session, "s1")).status == "waiting_input", state
    assert (await read(SessionExecution, "s1")).run_id is None
    assert len(calls) == 1
    request_id = (await q.list_pending("u1"))[0].id
    await q.reply(request_id, [["30s"]], "u1")
    worker = QuestionContinuationWorker()
    try:
        await worker.tick()
        await asyncio.wait_for(asyncio.gather(*worker.runs.values()), timeout=5)
    finally:
        await worker.stop()
    assert len(calls) == 2, state
    assert "30s" in str(calls[1]["messages"])
    assert (await read(Session, "s1")).status == "idle", state
    messages = await get_messages("s1", user_id="u1")
    assert messages[-1].finish == "stop"
    assert await q.list_pending("u1") == []


@pytest.mark.parametrize("tool,kind", [("question", "question"), ("plan_enter", "plan_enter"), ("creator_context", "memory_proposal")])
async def test_skip_is_durable_but_never_grants_approval(state, tool, kind):
    request_id = await checkpoint(tool=tool, continuation={"kind": kind, "memory_id": "not-approved"})
    await q.reject(request_id, "u1")
    await q.reject(request_id, "u1")
    assert (await read(QuestionCheckpoint, request_id)).status == "rejected"
    assert await q.list_pending("u1") == []
    assert await apply_answers("s1", "u1") == 0
    assert (await read(Part, "p1")).data["metadata"]["rejected"] is True
    assert (await read(Session, "s1")).agent == "build"


async def test_questions_are_bounded_and_content_cannot_change_under_one_id(state):
    with pytest.raises(ValueError, match="1 and 4"):
        await q.ask("s1", [q.Question(question="?")] * 5, user_id="u1")
    with pytest.raises(ValueError, match="persisted tool"):
        await q.ask("s1", [q.Question(question="?")], user_id="u1")
    request_id = await checkpoint()
    with pytest.raises(q.QuestionConflict):
        await q.ask("s1", [q.Question(question="Different question")],
                    {"messageID": "m-p1", "callID": "p1"}, "u1")
    assert (await read(QuestionCheckpoint, request_id)).questions[0]["question"] == "Choose?"


async def test_desktop_takeover_part_may_file_a_question(state):
    # Regression: desktop_takeover files a durable question from its OWN part.
    # It must be on the question-family allowlist, or the captcha handoff card
    # is never delivered and the tool errors "must be called directly". The
    # tool's own unit test mocks ask(), so only a real transaction catches this.
    request_id = await checkpoint(part_id="pt", tool="desktop_takeover")
    pending = await q.list_pending("u1")
    assert request_id in [item.id for item in pending]
    assert (await read(Part, "pt")).data["status"] == "waiting_input"


async def test_a_non_question_tool_part_is_still_rejected(state):
    # The same guard still blocks a question filed from an unrelated tool's
    # part (e.g. a batch-wrapped call, whose part is the batch tool): the
    # allowlist did not simply open up.
    async with database.get_db_session() as db:
        db.add(Message(id="m-pb", session_id="s1", user_id="u1", role="assistant",
                       finish="waiting_input", created_at=runtime.now()))
        await db.flush()
        db.add(Part(id="pb", session_id="s1", message_id="m-pb", user_id="u1", type="tool",
                    data={"id": "pb", "type": "tool", "tool": "bash", "status": "running"},
                    created_at=runtime.now()))
    with pytest.raises(ValueError, match="must be called directly"):
        await q.ask("s1", [q.Question(question="Choose?", options=[q.QuestionOption(label="Yes")])],
                    {"messageID": "m-pb", "callID": "pb"}, "u1")
