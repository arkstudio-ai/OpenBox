"""PA-03/04: independent HTTP replies use actual SQL decisions and recovery.

Authentication is the fixture boundary; the actual routers, membership checks,
Commands, checkpoints, grant writes and original continuation run unchanged.
No provider is invoked. PostgreSQL cases witness the competing row-lock wait.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json

import httpx
import pytest
from sqlalchemy import event, func, select, text

from api import permissions as permission_api, questions as question_api
from assistant import permission_requests as approvals, requests as questions
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.assistant import AssistantCommand
from db.models.part import Part
from db.models.permission import PermissionRule
from db.models.question import QuestionCheckpoint
from question import runtime
from question.continuation import QuestionContinuationWorker, apply_answers
from tests.unit.test_assistant_api import client_for
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_permissions import call, consume, register  # noqa: F401
from tests.unit.test_assistant_requests import pending, signing_key  # noqa: F401


@asynccontextmanager
async def clients(scope, monkeypatch, *, drop_response=False):
    async with client_for(scope["user_id"], scope["workspace_id"], monkeypatch) as left, \
            client_for(scope["user_id"], scope["workspace_id"], monkeypatch) as right:
        for client in (left, right):
            app = client._transport.app
            app.include_router(question_api.router, prefix="/api/agent")
            app.include_router(permission_api.router, prefix="/api/agent")
        if drop_response:
            app = left._transport.app
            @app.middleware("http")
            async def lose_once(request, call_next):
                nonlocal drop_response
                response = await call_next(request)
                if drop_response and request.method == "POST" and response.status_code == 200:
                    drop_response = False
                    raise ConnectionError("fixture lost HTTP response after SQL commit")
                return response
            left._transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        yield left, right


async def reply_race(module, owner, left, right, monkeypatch, record_property):
    """Pause only after the real first lock; require the second to wait in PG."""
    held, release = asyncio.Event(), asyncio.Event()
    pids, witness = [], None
    postgres = get_engine().dialect.name == "postgresql"
    actual = module.lock_actor

    async def locked(db, user_id):
        assert user_id == owner
        pid = await db.scalar(text("SELECT pg_backend_pid()"))
        if pid not in pids:
            pids.append(pid)
        await actual(db, user_id)
        if pid == pids[0] and not held.is_set():
            held.set()
            await asyncio.wait_for(release.wait(), 5)

    with monkeypatch.context() as patch:
        if postgres:
            patch.setattr(module, "lock_actor", locked)
        one, two = asyncio.create_task(left()), None
        try:
            if postgres:
                await asyncio.wait_for(held.wait(), 3)
            two = asyncio.create_task(right())
            if postgres:
                async with asyncio.timeout(3):
                    while True:
                        if len(pids) > 1:
                            async with get_db_session() as db:
                                row = (await db.execute(text("SELECT pid,wait_event_type,pg_blocking_pids(pid) AS blockers "
                                    "FROM pg_stat_activity WHERE pid=:pid"), {"pid": pids[1]})).mappings().one()
                            if row["wait_event_type"] == "Lock" and pids[0] in row["blockers"]:
                                witness = dict(row)
                                break
                        assert not two.done(), "A reply bypassed the held production actor lock"
                        await asyncio.sleep(.01)
            release.set()
            responses = await asyncio.gather(one, two)
        finally:
            release.set()
            await asyncio.gather(*(task for task in (one, two) if task is not None), return_exceptions=True)
    if witness:
        record_property("reply_lock_wait", json.dumps(witness))
    return responses


async def command(request_id):
    async with get_db_session() as db:
        rows = list((await db.scalars(select(AssistantCommand).where(
            AssistantCommand.action == "request_reply", AssistantCommand.target_id == request_id))).all())
        assert len(rows) == 1
        return rows[0].id, dict(rows[0].receipt)


async def reopen():
    url = get_engine().url
    await close_engine()
    init_engine(url)


@pytest.mark.parametrize("duplicate", [True, False], ids=["same-reply", "competing-decisions"])
async def test_question_two_http_clients_serialize_and_replay_one_decision(monkeypatch, record_property, duplicate):
    scope, _, request, binding = await pending()
    path = f"/api/agent/question/{request.id}"
    body = {**binding, "answers": [["Blue"]]}
    other = body if duplicate else {**binding, "reply_id": "second-client", "answers": [["Green"]]}
    async with clients(scope, monkeypatch) as (left, right):
        responses = await reply_race(questions, scope["user_id"], lambda: left.post(path, json=body),
            lambda: right.post(path, json=other), monkeypatch, record_property)
        assert sorted(response.status_code for response in responses) == ([200, 200] if duplicate else [200, 409])
        command_id, receipt = await command(request.id)
        assert all(response.json()["command_id"] == command_id for response in responses if response.status_code == 200)
        selected = body if receipt["reply_id"] == binding["reply_id"] else other
        changed = {**selected, "answers": [["Green" if selected["answers"] == [["Blue"]] else "Blue"]]}
        assert (await right.post(path, json=changed)).status_code == 409
        await apply_answers(request.session_id, scope["user_id"])
        await apply_answers(request.session_id, scope["user_id"])
        replay = (await right.post(path, json=selected)).json()
        assert replay["state"] == "applied" and replay["command_id"] == command_id
        assert (await left.get(f"/api/assistant/commands/{command_id}")).json()["receipt"] == replay
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, request.id)
        part = await db.get(Part, request.tool["callID"])
        assert row.applied and row.answers == selected["answers"]
        assert part.data["metadata"]["reply_ref"]["command_id"] == command_id
    record_property("reply_receipt", json.dumps({"scenario": "PA-03", "kind": "question", "duplicate": duplicate,
        "receipt": replay, "http_statuses": [r.status_code for r in responses], "decisions": 1}))


@pytest.mark.parametrize("duplicate", [True, False], ids=["same-reply", "competing-decisions"])
async def test_permission_two_http_clients_serialize_and_never_duplicate_grants(call, monkeypatch, record_property, duplicate):
    request, binding = await register(call)
    path = f"/api/agent/permission/{request.id}"
    body = {**binding, "action": "always"}
    other = body if duplicate else {**binding, "reply_id": "second-client", "action": "reject"}
    async with clients(call.scope, monkeypatch) as (left, right):
        responses = await reply_race(approvals, call.scope["user_id"], lambda: left.post(path, json=body),
            lambda: right.post(path, json=other), monkeypatch, record_property)
        assert sorted(response.status_code for response in responses) == ([200, 200] if duplicate else [200, 409])
        command_id, receipt = await command(request.id)
        assert all(response.json()["command_id"] == command_id for response in responses if response.status_code == 200)
        selected = body if receipt["reply_id"] == binding["reply_id"] else other
        assert (await right.post(path, json={**selected, "action": "once"})).status_code == 409
        applied = await asyncio.gather(consume(call, request), consume(call, request))
        assert applied[0] == applied[1] and applied[0]["action"] == selected["action"]
        replay = (await right.post(path, json=selected)).json()
        assert replay["state"] == "applied" and replay["command_id"] == command_id
        assert (await left.get(f"/api/assistant/commands/{command_id}")).json()["receipt"] == replay
    async with get_db_session() as db:
        count = await db.scalar(select(func.count()).select_from(PermissionRule).where(PermissionRule.user_id == call.scope["user_id"]))
        assert count == (1 if selected["action"] == "always" else 0)
        assert len(replay["rule_ids"]) == count
    record_property("reply_receipt", json.dumps({"scenario": "PA-03", "kind": "permission", "duplicate": duplicate,
        "receipt": replay, "http_statuses": [r.status_code for r in responses], "decisions": 1, "grants": count}))


async def test_question_http_distinguishes_known_expired_from_unknown(monkeypatch):
    scope, _, request, binding = await pending(expires_at=runtime.now() - timedelta(seconds=1))
    async with clients(scope, monkeypatch) as (left, right):
        body = {**binding, "answers": [["Blue"]]}
        expired = await left.post(f"/api/agent/question/{request.id}", json=body)
        missing = await right.post("/api/agent/question/absent-fixture-question", json=body)
        assert expired.status_code == 410 and expired.json()["detail"]["code"] == "QUESTION_GONE"
        assert missing.status_code == 404
    async with get_db_session() as db:
        assert (await db.get(QuestionCheckpoint, request.id)).answers is None


async def test_permission_http_distinguishes_known_expired_from_unknown(call, monkeypatch):
    request, binding = await register(call)
    async with clients(call.scope, monkeypatch) as (left, right):
        with monkeypatch.context() as patch:
            patch.setattr(runtime, "now", lambda: datetime.fromisoformat(request.expires_at) + timedelta(seconds=1))
            expired = await left.post(f"/api/agent/permission/{request.id}", json={**binding, "action": "always"})
            assert expired.status_code == 410 and expired.json()["detail"]["code"] == "PERMISSION_GONE"
        assert (await right.post("/api/agent/permission/absent-fixture-permission", json={**binding, "action": "always"})).status_code == 404
    async with get_db_session() as db:
        assert await approvals.decision_for(db, request.id) is None


async def test_question_lost_http_reply_and_sql_apply_failure_recover_exact_original(monkeypatch, record_property):
    scope, _, request, binding = await pending()
    path, body = f"/api/agent/question/{request.id}", {**binding, "answers": [["Blue"]]}
    async with clients(scope, monkeypatch, drop_response=True) as (left, right):
        assert (await left.post(path, json=body)).status_code == 500
        command_id, original = await command(request.id)
        assert original["state"] == "accepted"
        await reopen()
        assert (await right.post(path, json=body)).json() == original
        engine, injected = get_engine().sync_engine, []
        def fail_part(_conn, _cursor, statement, *_):
            if statement.lower().startswith("update parts set"):
                injected.append(True)
                raise RuntimeError("fixture storage failure during actual continuation")
        event.listen(engine, "before_cursor_execute", fail_part)
        try:
            with pytest.raises(RuntimeError, match="fixture storage failure"):
                await apply_answers(request.session_id, scope["user_id"])
        finally:
            event.remove(engine, "before_cursor_execute", fail_part)
        assert injected
        async with get_db_session() as db:
            row = await db.get(QuestionCheckpoint, request.id)
            assert row.answers == [["Blue"]] and not row.applied
        assert (await command(request.id))[1]["state"] == "accepted"
        await reopen()
        resumed = []
        async def provider_boundary(session_id, user_id, generation):
            resumed.append((session_id, user_id, generation))
        worker = QuestionContinuationWorker()
        monkeypatch.setattr(worker, "_resume", provider_boundary)
        try:
            await worker._resume_candidate(request.session_id, scope["user_id"], request.generation)
            await asyncio.gather(*worker.runs.values())
        finally:
            await worker.stop()
        assert resumed == [(request.session_id, scope["user_id"], request.generation)]
        done = (await right.post(path, json=body)).json()
        assert done["command_id"] == command_id and done["state"] == "applied"
        assert (await right.get(f"/api/assistant/commands/{command_id}")).json()["receipt"] == done
    record_property("reply_recovery", json.dumps({"scenario": "PA-04", "kind": "question", "receipt": done,
        "http_response_lost_after_commit": True, "sql_apply_rollback": True, "resumed_original": resumed}))


async def test_permission_lost_http_reply_and_failed_grant_recover_exact_original(call, monkeypatch, record_property):
    request, binding = await register(call)
    path, body = f"/api/agent/permission/{request.id}", {**binding, "action": "always"}
    async with clients(call.scope, monkeypatch, drop_response=True) as (left, right):
        assert (await left.post(path, json=body)).status_code == 500
        command_id, original = await command(request.id)
        assert original["state"] == "applying"
        await reopen()
        assert (await right.post(path, json=body)).json() == original
        engine, injected = get_engine().sync_engine, []
        def fail_grant(_conn, _cursor, statement, *_):
            if statement.lower().startswith("insert into permission_rules"):
                injected.append(True)
                raise RuntimeError("fixture storage failure during actual grant")
        event.listen(engine, "before_cursor_execute", fail_grant)
        try:
            with pytest.raises(RuntimeError, match="fixture storage failure"):
                await consume(call, request)
        finally:
            event.remove(engine, "before_cursor_execute", fail_grant)
        assert injected
        await approvals.application_failed(request)
        failed = (await right.post(path, json=body)).json()
        assert failed["command_id"] == command_id and failed["state"] == "failed" and failed["retryable"]
        async with get_db_session() as db:
            assert not await db.scalar(select(PermissionRule.id).where(PermissionRule.user_id == call.scope["user_id"]))
        await reopen()
        await approvals.recover_decisions()
        assert (await consume(call, request))["command_id"] == command_id
        done = (await right.post(path, json=body)).json()
        assert done["state"] == "applied" and len(done["rule_ids"]) == 1
        assert (await right.get(f"/api/assistant/commands/{command_id}")).json()["receipt"] == done
        assert (await consume(call, request))["command_id"] == command_id
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(PermissionRule).where(PermissionRule.user_id == call.scope["user_id"])) == 1
    record_property("reply_recovery", json.dumps({"scenario": "PA-04", "kind": "permission", "receipt": done,
        "http_response_lost_after_commit": True, "sql_apply_rollback": True, "failed_retry_same_command": True, "grants": 1}))
