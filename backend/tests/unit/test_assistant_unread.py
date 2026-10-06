"""Read-only sidebar counts retain full-snapshot scope and window semantics.

V2 (docs/PERSONAL_ASSISTANT_DESIGN_V2.md 4.2, D1): the badge checks each unread
answer itself and current access, never the answer's original sources.
"""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from api import assistant as api
from assistant import snapshot
from assistant.service import ensure_main_session
from db.base import get_db_session, get_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantReadCursor
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from db.models.user import User
from db.models.workspace import WorkspaceMember
from session.agent_event_log import append_agent_event_locked
from session.internal_parts import _lock_fenced, begin_session_write
from tests.unit.test_assistant_api import client_for, complete_answer, signing_key  # noqa: F401
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


def badge(value):
    return {key: value[key] for key in ("unread_count", "unread_count_is_lower_bound")}


def forbid(*_args, **_kwargs):
    pytest.fail("Unread GET must not create display receipts, inspect task detail or wake work")


async def forbid_async(*_args, **_kwargs):
    forbid()


def no_details(monkeypatch):
    monkeypatch.setattr(snapshot, "list_tasks", forbid_async)
    monkeypatch.setattr(snapshot, "get_task", forbid_async)
    monkeypatch.setattr(snapshot, "_sign_display", forbid)
    monkeypatch.setattr(api, "schedule_inbox_wake", forbid)


async def test_unread_http_never_creates_an_assistant_or_advances_read_cursor(monkeypatch):
    owner, _, workspace = await accounts()
    statements = []
    engine = get_engine().sync_engine
    def observed(_connection, _cursor, statement, *_args):
        statements.append(statement.lower())
    async with client_for(owner, workspace, monkeypatch) as client:
        no_details(monkeypatch)
        monkeypatch.setattr(snapshot, "_answer_digests", forbid_async)
        event.listen(engine, "before_cursor_execute", observed)
        try:
            response = await client.get("/api/assistant/unread")
        finally:
            event.remove(engine, "before_cursor_execute", observed)
        assert response.status_code == 200
        assert response.json() == {"unread_count": 0, "unread_count_is_lower_bound": False}
    assert not any(sql.lstrip().startswith(("insert", "update", "delete")) for sql in statements)
    async with get_db_session() as db:
        for model in (Session, AgentDriverState, AgentInboxItem, AssistantReadCursor):
            assert await db.scalar(select(func.count()).select_from(model).where(model.user_id == owner)) == 0


async def test_unread_only_digests_unread_answers_and_preserves_display_cursor_contract(monkeypatch, record_property):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    await complete_answer(owner, workspace, main, "older")
    await complete_answer(owner, workspace, main, "newer")
    full = await snapshot.get_snapshot(user_id=owner, workspace_id=workspace)
    newest, older = full["answers"]
    assert full["unread_count"] == 2
    await snapshot.advance_read_cursor(user_id=owner, workspace_id=workspace, main_id=main.id,
        last_seen_sequence=older["sequence"], display_token=older["display_token"])
    expected = badge(await snapshot.get_snapshot(user_id=owner, workspace_id=workspace))
    original = snapshot._answer_digests
    checked = []
    async def observe_digests(db, messages, **kwargs):
        checked.extend(message.id for message in messages)
        return await original(db, messages, **kwargs)
    monkeypatch.setattr(snapshot, "_answer_digests", observe_digests)
    async with client_for(owner, workspace, monkeypatch) as client:
        with monkeypatch.context() as patch:
            no_details(patch)
            response = await client.get("/api/assistant/unread")
            assert response.status_code == 200 and response.json() == expected
            assert checked == [newest["message_id"]]
        async with get_db_session() as db:
            assert (await db.get(AssistantReadCursor, (main.id, owner))).last_seen_sequence == older["sequence"]
            assert (await db.get(AgentDriverState, main.id)).generation == 2
        # The full view's original signed token still works. A badge read
        # neither mints a token nor changes what is considered displayed.
        advanced = await client.post("/api/assistant/read-cursor", json={
            "last_seen_sequence": newest["sequence"], "display_token": newest["display_token"]})
        assert advanced.status_code == 200
        checked.clear()
        statements = []
        def observed(_connection, _cursor, statement, *_args):
            statements.append(statement.lower())
        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", observed)
        try:
            all_read = await snapshot.get_snapshot(user_id=owner, workspace_id=workspace)
        finally:
            event.remove(engine, "before_cursor_execute", observed)
        assert all_read["unread_count"] == 0
        full_selects = sum(sql.lstrip().startswith("select") for sql in statements)
        full_part_reads = sum("from parts" in sql or "join parts" in sql for sql in statements)
        statements.clear()
        checked.clear()
        with monkeypatch.context() as patch:
            no_details(patch)
            event.listen(engine, "before_cursor_execute", observed)
            try:
                response = await client.get("/api/assistant/unread")
            finally:
                event.remove(engine, "before_cursor_execute", observed)
        assert response.json() == {"unread_count": 0, "unread_count_is_lower_bound": False}
        assert checked == []
        assert not any("from parts" in sql or "join parts" in sql or "assistant_tasks" in sql for sql in statements)
        assert not any(sql.lstrip().startswith(("insert", "update", "delete")) for sql in statements)
        light_selects = sum(sql.lstrip().startswith("select") for sql in statements)
        assert light_selects < full_selects and full_part_reads > 0
        record_property("full_snapshot_selects_all_read", full_selects)
        record_property("unread_summary_selects_all_read", light_selects)
        record_property("full_snapshot_part_reads_all_read", full_part_reads)
        record_property("unread_summary_part_reads_all_read", 0)


@pytest.mark.parametrize(("changed_part", "unread"), [("answer_text_removed", 0), ("human_source_edited", 1)])
async def test_unread_rechecks_the_answer_but_not_its_sources_after_each_http_read(monkeypatch, changed_part, unread):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    _, answer, answer_part = await complete_answer(owner, workspace, main, "source-bound")
    async with client_for(owner, workspace, monkeypatch) as client:
        first = await client.get("/api/assistant/unread")
        assert first.status_code == 200 and first.json()["unread_count"] == 1
        async with get_db_session() as db:
            part = await db.get(Part, answer_part.id) if changed_part == "answer_text_removed" else await db.scalar(
                select(Part).where(Part.message_id == answer.parent_id, Part.type == "text"))
            assert part is not None
            part.data = {**part.data, "text": "" if changed_part == "answer_text_removed"
                         else "Changed after the badge was checked"}
        # Revocation is not retroactive (D1): an edited human source does not
        # hide the answer; an answer without visible text is not counted.
        expected = badge(await snapshot.get_snapshot(user_id=owner, workspace_id=workspace))
        assert expected == {"unread_count": unread, "unread_count_is_lower_bound": False}
        assert (await client.get("/api/assistant/unread")).json() == expected
    async with get_db_session() as db:
        assert await db.get(AssistantReadCursor, (main.id, owner)) is None


async def test_unread_actor_workspace_and_live_membership_never_borrow_another_badge(monkeypatch):
    owner, peer, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    await complete_answer(owner, workspace, main, "owned")
    async with client_for(peer, workspace, monkeypatch) as client:
        assert (await client.get("/api/assistant/unread")).json() == {
            "unread_count": 0, "unread_count_is_lower_bound": False}
    async with get_db_session() as db:
        peer_workspace = (await db.get(User, peer)).default_workspace_id
    async with client_for(owner, peer_workspace, monkeypatch) as client:
        assert (await client.get("/api/assistant/unread")).status_code == 403
    async with client_for(owner, workspace, monkeypatch) as client:
        assert (await client.get("/api/assistant/unread")).json()["unread_count"] == 1
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
        assert (await client.get("/api/assistant/unread")).status_code == 403


async def test_exact_51_metadata_window_preserves_lower_bound_even_for_unavailable_candidates():
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    now = datetime.now(timezone.utc)
    sequences = []
    ids = []
    # These are deliberately unavailable metadata, not fabricated valid
    # source proofs. Real source availability is covered above. This fixture
    # exercises the exact 51-row window without creating a 51-turn source DAG.
    async with get_db_session() as db:
        await begin_session_write(db)
        row = await _lock_fenced(db, main.id, owner)
        for index in range(51):
            message_id = uuid4().hex
            ids.append(message_id)
            db.add(Message(id=message_id, session_id=main.id, user_id=owner,
                role="assistant", finish="stop", summary=False,
                error={"name": "UnavailableFixture", "message": str(index)}, created_at=now))
            terminal = await append_agent_event_locked(db, row, kind="turn.finished", message_id=message_id,
                payload={"finish": "stop", "test": "unavailable metadata window"})
            sequences.append(terminal.sequence)
        # A repeated recovery terminal must not move the oldest message into
        # the newest window. A summary must not consume a candidate slot.
        await append_agent_event_locked(db, row, kind="turn.finished", message_id=ids[0], payload={"recovery": True})
        summary_id = uuid4().hex
        db.add(Message(id=summary_id, session_id=main.id, user_id=owner, role="assistant",
            finish="stop", summary=True, created_at=now))
        await append_agent_event_locked(db, row, kind="turn.finished", message_id=summary_id, payload={"summary": True})
    # Exact 50 unread candidates + an older read one retains the original
    # conservative lower-bound=true. A LIMIT over unread rows alone differs.
    for seen, lower_bound in ((0, True), (sequences[0], True), (sequences[1], False), (sequences[-1], False)):
        async with get_db_session() as db:
            cursor = await db.get(AssistantReadCursor, (main.id, owner))
            if cursor is None:
                db.add(AssistantReadCursor(assistant_session_id=main.id, user_id=owner,
                    last_seen_sequence=seen, updated_at=now))
            else:
                cursor.last_seen_sequence = seen
        full = await snapshot.get_snapshot(user_id=owner, workspace_id=workspace)
        light = await snapshot.get_unread(user_id=owner, workspace_id=workspace)
        assert len(full["answers"]) == 50
        assert [item["message_id"] for item in full["answers"]] == list(reversed(ids[1:]))
        assert light == badge(full) == {"unread_count": 0, "unread_count_is_lower_bound": lower_bound}


async def test_unread_window_and_answer_parts_share_one_postgres_snapshot(monkeypatch):
    async with get_db_session() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("Independent writer consistency requires PostgreSQL MVCC")
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    _, _, answer_part = await complete_answer(owner, workspace, main, "snapshot")
    original = snapshot._answer_candidates
    changed = False
    async def race(db, **kwargs):
        nonlocal changed
        rows = await original(db, **kwargs)
        if not changed:
            changed = True
            async with get_db_session() as writer:
                part = await writer.get(Part, answer_part.id)
                part.data = {**part.data, "text": ""}  # The answer itself loses its text.
        return rows
    monkeypatch.setattr(snapshot, "_answer_candidates", race)
    assert await snapshot.get_unread(user_id=owner, workspace_id=workspace) == {
        "unread_count": 1, "unread_count_is_lower_bound": False}
    assert await snapshot.get_unread(user_id=owner, workspace_id=workspace) == {
        "unread_count": 0, "unread_count_is_lower_bound": False}
