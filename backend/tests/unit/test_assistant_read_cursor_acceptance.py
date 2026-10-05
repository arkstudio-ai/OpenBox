"""Independent HTTP workers retain one monotonic user read position."""
import asyncio
import json

import pytest
from sqlalchemy import func, select, text

from assistant import snapshot
from assistant.service import ensure_main_session
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantReadCursor
from tests.unit.test_assistant_api import client_for, complete_answer, signing_key  # noqa: F401
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def test_two_clients_reverse_and_duplicate_read_receipts_on_distinct_connections(monkeypatch, record_property):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Three concurrent independent transactions require PostgreSQL")
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    await complete_answer(owner, workspace, main, "older")
    await complete_answer(owner, workspace, main, "newer")
    async with get_db_session() as db:
        url = db.get_bind().url.render_as_string(hide_password=False)
        driver = await db.get(AgentDriverState, main.id)
        before = (driver.generation, driver.run_id,
            await db.scalar(select(func.count()).select_from(AgentInboxItem).where(AgentInboxItem.session_id == main.id)),
            await db.scalar(select(func.count()).select_from(AgentEvent).where(AgentEvent.session_id == main.id)))
        assert await db.get(AssistantReadCursor, (main.id, owner)) is None
    async with client_for(owner, workspace, monkeypatch) as device_a, client_for(owner, workspace, monkeypatch) as device_b:
        shown_a = (await device_a.get("/api/assistant")).json()
        shown_b = (await device_b.get("/api/assistant")).json()
        newest, older = shown_a["answers"]
        assert shown_b["answers"][1]["sequence"] == older["sequence"]
        assert shown_a["unread_count"] == shown_b["unread_count"] == 2
        connections, states = {}, []
        arrived, newest_holds_lock, release_newest = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_lock = snapshot.main_session_locked

        async def before_lock(db, *args, **kwargs):
            # Each actual HTTP worker has already opened its own SQL
            # transaction. All three see the initially absent user cursor.
            name = asyncio.current_task().get_name()
            connections[name] = await db.scalar(text("SELECT pg_backend_pid()"))
            states.append(await db.scalar(select(AssistantReadCursor.last_seen_sequence).where(
                AssistantReadCursor.assistant_session_id == main.id, AssistantReadCursor.user_id == owner)))
            if len(states) == 3:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), 5)
            if name != "newest-read":
                await asyncio.wait_for(newest_holds_lock.wait(), 5)
                return await original_lock(db, *args, **kwargs)
            locked = await original_lock(db, *args, **kwargs)
            newest_holds_lock.set()
            # Hold the real production row lock before its first cursor
            # insert, while both old HTTP requests attempt the same lock.
            await asyncio.wait_for(release_newest.wait(), 5)
            return locked

        monkeypatch.setattr(snapshot, "main_session_locked", before_lock)

        async def update(client, row):
            return await client.post("/api/assistant/read-cursor", json={
                "last_seen_sequence": row["sequence"], "display_token": row["display_token"]})

        workers = [asyncio.create_task(update(device_a, newest), name="newest-read"),
                   asyncio.create_task(update(device_b, older), name="older-read"),
                   asyncio.create_task(update(device_b, older), name="duplicate-read")]
        try:
            await asyncio.wait_for(newest_holds_lock.wait(), 5)
            async with asyncio.timeout(4):
                while True:
                    # Observe the actual PostgreSQL wait graph from a fourth
                    # connection. A pre-lock rendezvous alone would not prove
                    # that production's first-insert critical region overlaps.
                    async with get_db_session() as db:
                        waits = [dict(row) for row in (await db.execute(text(
                            "SELECT pid, wait_event_type, pg_blocking_pids(pid) AS blockers "
                            "FROM pg_stat_activity WHERE pid = ANY(CAST(:pids AS integer[]))"
                        ), {"pids": list(connections.values())})).mappings()]
                    graph = {row["pid"]: row["blockers"] for row in waits}

                    def blocked_by_newest(pid, seen=None):
                        if pid == connections["newest-read"]:
                            return True
                        seen = set() if seen is None else seen
                        if pid in seen:
                            return False
                        return any(blocked_by_newest(parent, seen | {pid}) for parent in graph.get(pid, []))

                    old_pids = {connections["older-read"], connections["duplicate-read"]}
                    if all(any(row["pid"] == pid and row["wait_event_type"] == "Lock" for row in waits)
                           and blocked_by_newest(pid) for pid in old_pids):
                        break
                    assert not any(worker.done() for worker in workers), "A worker bypassed the held production lock"
                    await asyncio.sleep(.01)
            release_newest.set()
            replies = await asyncio.wait_for(asyncio.gather(*workers), 15)
        finally:
            release_newest.set()
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            monkeypatch.setattr(snapshot, "main_session_locked", original_lock)
        assert len(set(connections.values())) == 3 and states == [None, None, None]
        assert all(reply.status_code == 200 and reply.json()["last_seen_sequence"] == newest["sequence"]
                   for reply in replies)
        # A higher integer cannot borrow an older displayed answer's token.
        refused = await device_b.post("/api/assistant/read-cursor", json={
            "last_seen_sequence": shown_b["high_water_mark"] + 1,
            "display_token": older["display_token"]})
        assert refused.status_code == 409
        await close_engine()
        init_engine(url)
        reread_a = (await device_a.get("/api/assistant")).json()
        reread_b = (await device_b.get("/api/assistant")).json()
        assert reread_a["last_seen_sequence"] == reread_b["last_seen_sequence"] == newest["sequence"]
        assert reread_a["unread_count"] == reread_b["unread_count"] == 0
    async with get_db_session() as db:
        driver = await db.get(AgentDriverState, main.id)
        after = (driver.generation, driver.run_id,
            await db.scalar(select(func.count()).select_from(AgentInboxItem).where(AgentInboxItem.session_id == main.id)),
            await db.scalar(select(func.count()).select_from(AgentEvent).where(AgentEvent.session_id == main.id)))
        count = await db.scalar(select(func.count()).select_from(AssistantReadCursor).where(
            AssistantReadCursor.assistant_session_id == main.id, AssistantReadCursor.user_id == owner))
        assert count == 1 and after == before and driver.phase == "idle"
    record_property("assistant_acceptance", json.dumps({"scenario": "PA-29", "main_id": main.id,
        "independent_http_clients": 2, "concurrent_sql_connections": len(set(connections.values())),
        "actual_lock_wait_graph": waits, "connection_roles": connections,
        "initial_positions": states, "received_sequences": [newest["sequence"], older["sequence"], older["sequence"]],
        "returned_sequences": [reply.json()["last_seen_sequence"] for reply in replies],
        "cursor_rows": count, "post_reopen_client_positions": [reread_a["last_seen_sequence"], reread_b["last_seen_sequence"]],
        "undisplayed_sequence_status": refused.status_code, "unchanged_driver_inbox_events": list(after)}))
