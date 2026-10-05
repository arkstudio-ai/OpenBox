"""PA-25: legacy read routes enforce the same private Session audience.

ASGI dispatch, JWT/workspace authorization, Session/Part and todo SQL are real.
Only the remote sandbox/snapshot read boundary is replaced; no cloud service,
provider, local runtime or existing user file is accessed.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import event

from api import sessions as routes
from assistant.service import ensure_main_session
from auth import jwt, middleware
from cache.memory_cache import MemoryCache
from db.base import get_db_session, get_engine
from db.models.message import Message
from db.models.part import Part
from db.models.workspace import WorkspaceMember
from session import session as sessions
from storage import storage
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


READ_ROUTES = ("todo", "diff", "diff/step", "plan")


@pytest.fixture
async def read_world(monkeypatch):
    # Todo uses the pre-ORM kv_store. PG is migrated by the disposable-db
    # runner; file SQLite needs the same legacy-table bootstrap as the app.
    from db.base import _ensure_single_user_legacy_tables
    async with get_engine().begin() as connection:
        await connection.run_sync(_ensure_single_user_legacy_tables)
    owner, peer, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    main = await sessions.update_session(main.id, user_id=owner, title="private-main-title-canary")
    execution = await sessions.create_session(user_id=owner, workspace_id=workspace,
        parent_id=main.id, title="private-execution-title-canary")
    private = await sessions.create_session(user_id=owner, workspace_id=workspace,
        visibility="private", title="private-ordinary-title-canary")
    shared = await sessions.create_session(user_id=owner, workspace_id=workspace,
        title="ordinary-shared-title")
    targets = (main, execution, private, shared)
    now = datetime.now(timezone.utc)
    for target in targets:
        await storage.write(["todo", target.id], {"items": [{"id": "todo-" + target.id,
            "subject": "todo-canary-" + target.id, "status": "pending"}]})
        async with get_db_session() as db:
            message_id = "read-message-" + uuid4().hex
            db.add(Message(id=message_id, session_id=target.id, user_id=owner,
                role="assistant", created_at=now))
            await db.flush()
            for index, kind in enumerate(("step-start", "step-finish")):
                db.add(Part(id="read-part-" + uuid4().hex, message_id=message_id,
                    session_id=target.id, user_id=owner, type=kind,
                    data={"snapshot": kind + "-" + target.id},
                    created_at=now + timedelta(seconds=index)))

    monkeypatch.setattr(jwt, "_secret", "private-session-read-route-test-key")
    monkeypatch.setattr(middleware, "_auth_enabled", True)
    monkeypatch.setattr(middleware, "_cache", MemoryCache())
    state = SimpleNamespace(owner=owner, peer=peer, workspace=workspace, main=main,
        execution=execution, private=private, shared=shared, reads=[])

    from snapshot import snapshot
    from sandbox import sandbox_manager

    async def diff(first, last, *, session_id, user_id):
        state.reads.append(("diff", session_id, user_id, first, last))
        return [SimpleNamespace(path="diff-canary-" + session_id, additions=2,
                                deletions=1, status="modified")]

    async def diff_full(first, last, *, session_id, user_id):
        state.reads.append(("diff_full", session_id, user_id, first, last))
        return [{"path": "diff-canary-" + session_id, "hunks": [{"content": "private-diff-body-canary"}]}]

    async def get_client(session_id, *, user_id):
        state.reads.append(("sandbox", session_id, user_id))

        async def read_file_raw(path):
            state.reads.append(("file", session_id, path))
            return "plan-canary-" + session_id

        return SimpleNamespace(read_file_raw=read_file_raw)

    monkeypatch.setattr(snapshot, "diff", diff)
    monkeypatch.setattr(snapshot, "diff_full", diff_full)
    monkeypatch.setattr(sandbox_manager, "get_client", get_client)

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select "):
            if " from parts " in normalized:
                state.reads.append(("parts_sql",))
            if " from kv_store " in normalized:
                state.reads.append(("todo_sql",))

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record_sql)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/agent")

    def headers(actor):
        return {"Authorization": "Bearer " + jwt.create_access_token(actor, "user"),
                "X-Workspace-Id": workspace}

    state.headers = headers
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://read.test") as api:
            state.api = api
            yield state
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)


async def read(w, session_id, route, actor, *, full=False):
    params = {"from_snapshot": "private-before", "to_snapshot": "private-after"} if route == "diff/step" else {}
    if route == "diff" and full:
        params["full"] = "true"
    return await w.api.get(f"/api/agent/session/{session_id}/{route}", params=params,
                           headers=w.headers(actor) if actor else {})


@pytest.mark.parametrize("route", READ_ROUTES)
async def test_private_session_read_routes_hide_peer_and_unknown_before_downstream_read(read_world, route):
    w = read_world
    unknown = await read(w, "session-unknown-" + uuid4().hex, route, w.peer)
    assert unknown.status_code == 404 and unknown.json() == {"detail": "Session not found"}
    for target in (w.main, w.execution, w.private):
        denied = await read(w, target.id, route, w.peer)
        assert (denied.status_code, denied.json()) == (unknown.status_code, unknown.json())
        assert target.id not in denied.text and target.title not in denied.text
        assert "canary" not in denied.text
        anonymous = await read(w, target.id, route, None)
        assert anonymous.status_code == 401
    # These historical routes are owner-only even when the transcript is shared.
    shared = await read(w, w.shared.id, route, w.peer)
    assert shared.status_code == 403 and shared.json()["detail"]["code"] == "SESSION_READ_ONLY"
    assert w.reads == []


@pytest.mark.parametrize("route", READ_ROUTES)
async def test_owner_read_routes_preserve_private_and_ordinary_positive_contract(read_world, route):
    w = read_world
    targets = (w.private, w.shared) if route == "plan" else (w.main, w.execution, w.private, w.shared)
    for target in targets:
        w.reads.clear()
        response = await read(w, target.id, route, w.owner)
        assert response.status_code == 200, response.text
        if route == "todo":
            assert response.json()["items"][0]["subject"] == "todo-canary-" + target.id
            assert w.reads == [("todo_sql",)]
        elif route == "plan":
            assert response.json()["content"] == "plan-canary-" + target.id
            assert w.reads[0] == ("sandbox", target.id, w.owner)
            assert w.reads[1] == ("file", target.id, response.json()["path"])
        elif route == "diff":
            assert response.json() == [{"path": "diff-canary-" + target.id,
                "additions": 2, "deletions": 1, "status": "modified"}]
            assert w.reads == [("parts_sql",), ("diff", target.id, w.owner,
                "step-start-" + target.id, "step-finish-" + target.id)]
            w.reads.clear()
            detailed = await read(w, target.id, route, w.owner, full=True)
            assert detailed.status_code == 200 and "private-diff-body-canary" in detailed.text
            assert w.reads == [("parts_sql",), ("diff_full", target.id, w.owner,
                "step-start-" + target.id, "step-finish-" + target.id)]
        else:
            assert response.json()[0]["path"] == "diff-canary-" + target.id
            assert "private-diff-body-canary" in response.text
            assert w.reads == [("diff_full", target.id, w.owner, "private-before", "private-after")]


@pytest.mark.parametrize("route", READ_ROUTES)
async def test_revoked_owner_read_routes_recheck_membership_before_downstream_read(read_world, route):
    w = read_world
    headers = w.headers(w.owner)
    assert (await read(w, w.private.id, route, w.owner)).status_code == 200
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
    w.reads.clear()
    params = {"from_snapshot": "private-before", "to_snapshot": "private-after"} if route == "diff/step" else {}
    responses = [await w.api.get(f"/api/agent/session/{target}/{route}", headers=headers, params=params)
                 for target in (w.private.id, "session-unknown-" + uuid4().hex)]
    assert responses[0].status_code == responses[1].status_code == 403
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["detail"]["code"] == "WORKSPACE_FORBIDDEN"
    assert "canary" not in responses[0].text and w.reads == []


async def test_managed_owner_plan_reads_require_the_recorded_review_without_sandbox_read(read_world):
    w = read_world
    for target, code in ((w.main, "ASSISTANT_INPUT_REQUIRED"),
                         (w.execution, "ASSISTANT_PLAN_REVIEW_REQUIRED")):
        response = await read(w, target.id, "plan", w.owner)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == code
    assert w.reads == []
