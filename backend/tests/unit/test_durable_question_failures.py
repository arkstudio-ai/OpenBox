"""Failure-window regressions for durable human input.

Uses the migration-backed SQLite/PostgreSQL fixture. Fault injection is local
to the test process; no live Redis, model, sandbox or user database is stopped.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

import db.base as database
from api.questions import router
from auth.middleware import get_current_user
from db.models.message import Message
from db.models.part import Part
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from question import question as q
from question import runtime
from question.continuation import QuestionContinuationWorker, apply_answers, expire_questions
from tests.unit.test_durable_questions import checkpoint, read, state  # noqa: F401


def application(user_id="u1"):
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user_id}
    return app


@pytest.mark.parametrize("kind", ["missing", "malformed", "expired", "refresh", "revoked"])
async def test_invalid_auth_cannot_read_or_change_questions(state, monkeypatch, kind):
    from jose import jwt
    import auth.jwt as auth_jwt
    import auth.middleware as middleware
    monkeypatch.setattr(auth_jwt, "_secret", "local-regression-secret-not-a-deployed-key")
    monkeypatch.setattr(middleware, "_auth_enabled", True)

    class Blacklist:
        async def exists(self, _key):
            return kind == "revoked"

    monkeypatch.setattr(middleware, "_cache", Blacklist())
    request_id = await checkpoint()
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    token = jwt.encode({"sub": "u1", "jti": "test-only", "type": "refresh" if kind == "refresh" else "access",
                        "exp": runtime.now() + timedelta(hours=-1 if kind == "expired" else 1)},
                       auth_jwt._secret, algorithm="HS256")
    headers = {} if kind == "missing" else {"Authorization": "Bearer " + ("invalid" if kind == "malformed" else token)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        path = f"/api/agent/question/{request_id}"
        responses = [await client.get("/api/agent/question"), await client.get(path),
                     await client.post(path, json={"answers": [["Yes"]]}),
                     await client.post(path + "/reject"),
                     await client.put(path + "/draft", json={"draft": [{}], "revision": 0})]
    assert [response.status_code for response in responses] == [401] * 5
    assert (await read(QuestionCheckpoint, request_id)).status == "pending"


@pytest.mark.parametrize("body", [
    {"draft": [], "revision": 0},
    {"draft": [{}], "revision": -1},
    {"draft": [{}], "revision": "not-a-revision"},
    {"draft": [{"selected": ["not-offered"]}], "revision": 0},
    {"draft": [{"custom": "not-allowed"}], "revision": 0},
    {"draft": [{"custom": "x" * 5001}], "revision": 0},
    {"draft": [{}, {}], "revision": 0},
])
async def test_invalid_draft_never_replaces_saved_input(state, body):
    request_id = await checkpoint(questions=[q.Question(question="Choose", custom=False, options=[q.QuestionOption(label="Yes")])])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="http://test") as client:
        response = await client.put(f"/api/agent/question/{request_id}/draft", json=body)
    assert response.status_code == 422
    row = await read(QuestionCheckpoint, request_id)
    assert row.status == "pending" and row.draft_revision == 0
    assert row.draft == [{"selected": [], "custom": "", "use_custom": False}]


@pytest.mark.parametrize("answers", [
    None, [], [None], ["Yes"], [[1]], [[{}]], [[]], [[" "]],
    [["Yes", "No"]], [["Yes"], ["Yes"]], [["Yes"]] * 5, [["x" * 5001]],
])
async def test_invalid_http_answers_leave_checkpoint_untouched(state, answers):
    request_id = await checkpoint()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application()), base_url="http://test") as client:
        response = await client.post(f"/api/agent/question/{request_id}", json={"answers": answers})
    assert response.status_code == 422
    row = await read(QuestionCheckpoint, request_id)
    assert row.status == "pending" and row.answers is None and not row.applied
    assert not (await read(SessionExecution, "s1")).resume_pending


@pytest.mark.parametrize("count", [1, 4])
async def test_question_count_and_custom_answer_upper_bound_are_accepted(state, count):
    request_id = await checkpoint(questions=[q.Question(question=f"Question {i}") for i in range(count)])
    await q.reply(request_id, [["a" * 5000] for _ in range(count)], "u1")
    assert (await read(QuestionCheckpoint, request_id)).answers == [["a" * 5000] for _ in range(count)]


async def test_multiselect_normalizes_duplicates_and_rejects_unknown_labels(state):
    request_id = await checkpoint(questions=[q.Question(question="Pick", multiple=True, custom=False,
        options=[q.QuestionOption(label="A"), q.QuestionOption(label="B")])])
    with pytest.raises(ValueError):
        await q.reply(request_id, [["unknown"]], "u1")
    await q.reply(request_id, [[" B ", "A", "B", ""]], "u1")
    assert (await read(QuestionCheckpoint, request_id)).answers == [["B", "A"]]


@pytest.mark.parametrize("user_id,request_id", [("u1", "missing"), ("u2", None)])
async def test_every_question_endpoint_hides_unknown_or_other_owners_requests(state, user_id, request_id):
    actual_id = await checkpoint()
    path = f"/api/agent/question/{request_id or actual_id}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(user_id)), base_url="http://test") as client:
        responses = [await client.get(path), await client.post(path, json={"answers": [["Yes"]]}),
                     await client.post(path + "/reject"),
                     await client.put(path + "/draft", json={"draft": [{}], "revision": 0})]
        assert [r.status_code for r in responses] == [404] * 4
    assert (await read(QuestionCheckpoint, actual_id)).status == "pending"


@pytest.mark.parametrize("terminal", ["cancelled", "superseded", "expired", "rejected"])
async def test_terminal_questions_cannot_be_answered_or_draft_mutated(state, terminal):
    request_id = await checkpoint()
    if terminal == "cancelled":
        await runtime.cancel_session("s1", "u1")
    elif terminal == "superseded":
        from session.session import create_user_message
        await create_user_message("s1", "Replacement", user_id="u1")
    elif terminal == "expired":
        async with database.get_db_session() as db:
            (await db.get(QuestionCheckpoint, request_id)).expires_at = runtime.now() - timedelta(seconds=1)
        await expire_questions()
    else:
        await q.reject(request_id, "u1")
    with pytest.raises((q.QuestionGone, q.QuestionConflict)):
        await q.reply(request_id, [["Yes"]], "u1")
    with pytest.raises(q.QuestionGone):
        await q.save_draft(request_id, [q.DraftAnswer(selected=["Yes"])], 0, "u1")
    assert (await read(QuestionCheckpoint, request_id)).status == terminal
    assert await q.list_pending("u1") == []


async def test_concurrent_duplicate_answers_emit_one_resolution(state):
    request_id = await checkpoint()
    replies = await asyncio.gather(*(q.reply(request_id, [["Yes"]], "u1") for _ in range(12)))
    assert all(reply["ok"] for reply in replies)
    assert len([e for e in state if e[0] == "question.replied"]) == 1
    assert (await read(QuestionCheckpoint, request_id)).answers == [["Yes"]]


@pytest.mark.parametrize("second_action", ["different_answer", "skip", "cancel"])
async def test_conflicting_resolution_race_never_overwrites_or_revives(state, second_action):
    request_id = await checkpoint()
    other = {"different_answer": lambda: q.reply(request_id, [["No"]], "u1"),
             "skip": lambda: q.reject(request_id, "u1"),
             "cancel": lambda: runtime.cancel_session("s1", "u1")}[second_action]
    results = await asyncio.gather(q.reply(request_id, [["Yes"]], "u1"), other(), return_exceptions=True)
    row = await read(QuestionCheckpoint, request_id)
    if second_action == "cancel":
        assert row.status == "cancelled"
        assert await apply_answers("s1", "u1") is None
        assert not (await read(SessionExecution, "s1")).resume_pending
    else:
        assert sum(isinstance(result, q.QuestionConflict) for result in results) == 1
        assert row.status in ("answered", "rejected")
        assert len([e for e in state if e[0] in ("question.replied", "question.rejected")]) == 1


async def test_answer_transaction_failure_rolls_back_then_retry_succeeds(state, monkeypatch):
    request_id = await checkpoint()
    real_transaction = runtime.transaction

    @asynccontextmanager
    async def fail_before_commit(*args, **kwargs):
        async with real_transaction(*args, **kwargs) as transaction:
            yield transaction
            raise ConnectionError("injected failure before commit")

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "transaction", fail_before_commit)
        with pytest.raises(ConnectionError):
            await q.reply(request_id, [["Yes"]], "u1")
    row = await read(QuestionCheckpoint, request_id)
    assert row.status == "pending" and row.answers is None
    assert not (await read(SessionExecution, "s1")).resume_pending
    assert not any(e[0] == "question.replied" for e in state)
    await q.reply(request_id, [["Yes"]], "u1")
    assert (await read(QuestionCheckpoint, request_id)).status == "answered"


async def test_response_lost_after_commit_retries_without_duplicate_continuation(state, monkeypatch):
    request_id = await checkpoint()
    app = application()
    drop_response = True

    @app.middleware("http")
    async def lose_first_reply(request, call_next):
        nonlocal drop_response
        response = await call_next(request)
        if request.method == "POST" and drop_response:
            drop_response = False
            raise ConnectionError("injected response loss after commit")
        return response

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        path = f"/api/agent/question/{request_id}"
        assert (await client.post(path, json={"answers": [["Yes"]]})).status_code == 500
        assert (await read(QuestionCheckpoint, request_id)).status == "answered"
        assert (await read(SessionExecution, "s1")).resume_pending
        assert (await client.post(path, json={"answers": [["Yes"]]})).status_code == 200
    started = []

    async def run_once(session_id, user_id, expected_generation):
        ticket = await runtime.start_run(session_id, user_id, expected_generation=expected_generation)
        if ticket:
            started.append(ticket.run_id)
            await runtime.finish_run(ticket)

    monkeypatch.setattr("agent.loop.run_loop", run_once)
    worker = QuestionContinuationWorker()
    try:
        await worker.tick()
        await asyncio.gather(*worker.runs.values())
        await worker.tick()
        assert len(started) == 1
        assert len([e for e in state if e[0] == "question.replied"]) == 1
    finally:
        await worker.stop()


async def test_lost_ui_events_do_not_lose_checkpoint_or_resume_intent(state, monkeypatch):
    monkeypatch.setattr("bus.bus.publish", lambda *args, **kwargs: None)
    request_id = await checkpoint()
    assert [row.id for row in await q.list_pending("u1")] == [request_id]
    await q.reply(request_id, [["Yes"]], "u1")
    assert (await read(SessionExecution, "s1")).resume_pending
    assert await apply_answers("s1", "u1") == 0
    assert (await read(QuestionCheckpoint, request_id)).applied
    ticket = await runtime.start_run("s1", "u1", expected_generation=0)
    assert ticket is not None
    await runtime.finish_run(ticket)


async def test_permanent_continuation_error_preserves_answer_and_rolls_back_approval(state):
    request_id = await checkpoint(tool="plan_enter", continuation={"kind": "plan_enter"})
    await q.reply(request_id, [["Yes"]], "u1")
    async with database.get_db_session() as db:
        await db.execute(delete(Part).where(Part.id == "p1"))
    worker = QuestionContinuationWorker()
    try:
        await worker.tick()
        await worker.tick()
        row = await read(QuestionCheckpoint, request_id)
        execution = await read(SessionExecution, "s1")
        assert row.answers == [["Yes"]] and not row.applied
        assert not execution.resume_pending and execution.resume_error
        session = await read(Session, "s1")
        assert session.status == "error" and session.agent == "build"
        async with database.get_db_session() as db:
            assert await db.scalar(select(Message.id).where(Message.client_message_id == f"ask:{request_id}")) is None
        assert len([e for e in state if e[0] == "session.error"]) == 1
    finally:
        await worker.stop()


async def test_transient_continuation_error_recovers_after_backoff(state, monkeypatch):
    request_id = await checkpoint()
    await q.reply(request_id, [["Yes"]], "u1")
    started = []

    async def run_once(session_id, user_id, expected_generation):
        ticket = await runtime.start_run(session_id, user_id, expected_generation=expected_generation)
        if ticket:
            started.append(ticket.run_id)
            await runtime.finish_run(ticket)

    async def unavailable(*args):
        raise ConnectionError("injected database outage")

    monkeypatch.setattr("agent.loop.run_loop", run_once)
    worker = QuestionContinuationWorker()
    try:
        with monkeypatch.context() as patch:
            patch.setattr("question.continuation.apply_answers", unavailable)
            await worker.tick()
        assert not started
        assert (await read(SessionExecution, "s1")).next_attempt_at is not None
        await worker.tick()
        assert not worker.runs  # Respect backoff rather than spinning.
        async with database.get_db_session() as db:
            (await db.get(SessionExecution, "s1")).next_attempt_at = runtime.now() - timedelta(seconds=1)
        await worker.tick()
        await asyncio.gather(*worker.runs.values())
        await worker.tick()
        assert len(started) == 1
        assert (await read(QuestionCheckpoint, request_id)).applied
    finally:
        await worker.stop()


async def test_heartbeat_failure_aborts_execution(state, monkeypatch):
    ticket = await runtime.start_run("s1", "u1")
    abort = asyncio.Event()

    @asynccontextmanager
    async def unavailable(*args):
        raise ConnectionError("injected heartbeat database outage")
        yield  # pragma: no cover

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "LEASE_SECONDS", .003)
        patch.setattr(runtime, "transaction", unavailable)
        await asyncio.wait_for(runtime.heartbeat(ticket, abort), timeout=1)
    assert abort.is_set()


async def test_long_wait_does_not_expire_or_start_work_without_an_explicit_ttl(state, monkeypatch):
    request_id = await checkpoint()
    future = runtime.now() + timedelta(days=30)
    monkeypatch.setattr(runtime, "now", lambda: future)
    worker = QuestionContinuationWorker()
    try:
        await worker.tick()
        assert not worker.runs
        assert [row.id for row in await q.list_pending("u1")] == [request_id]
        execution = await read(SessionExecution, "s1")
        assert execution.run_id is None and execution.lease_until is None
        assert not execution.resume_pending
        assert (await read(Session, "s1")).status == "waiting_input"
    finally:
        await worker.stop()
