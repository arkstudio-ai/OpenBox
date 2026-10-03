"""Replay cursor, execution outbox and actor-scoped public event evidence."""
import asyncio
import json

import pytest
from sqlalchemy import select

from assistant import events
from assistant.commands import accept_task_command
from assistant.control import accept_control_command
from assistant.inputs import accept_turn
from assistant.policy import AssistantError
from assistant.service import ensure_main_session
from assistant.snapshot import get_snapshot
from core.config import get_config
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantEventProjection
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from session.agent_event_log import append_agent_event_locked, ensure_surface_seed_locked, verify_agent_event_parity
from session.internal_parts import _lock_fenced, begin_session_write
from tests.unit.test_assistant_api import client_for
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


@pytest.fixture(autouse=True)
def cursor_secret(monkeypatch):
    monkeypatch.setattr(get_config(), "jwt_secret", "events-test-only")


async def setup():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    scope = {"user_id": owner, "workspace_id": workspace}
    return scope, main, other


async def test_snapshot_cursor_pages_private_events_without_exposing_payload_or_dispatching(monkeypatch):
    scope, main, _ = await setup()
    snapshot = await get_snapshot(**scope)
    assert snapshot["high_water_mark"] == 0
    await accept_turn(**scope, main_id=main.id, client_id="one", text="private-body-marker")
    async with client_for(scope["user_id"], scope["workspace_id"], monkeypatch) as client:
        first = (await client.get("/api/assistant/events", params={"after": snapshot["event_cursor"], "limit": 1})).json()
        assert first["events"] == [] and first["next_sequence"] == 1 and first["has_more"]
        second = (await client.get("/api/assistant/events", params={"after": first["next_cursor"]})).json()
        assert len(second["events"]) == 1 and not second["has_more"]
        assert second["events"][0]["kind"] == "assistant.turn.accepted"
        assert second["events"][0]["inbox_id"]
        assert "private-body-marker" not in json.dumps(second) and "origin_ref" not in json.dumps(second)
        replay = (await client.get("/api/assistant/events", params={"after": first["next_cursor"]})).json()
        assert replay["events"] == second["events"]
        assert (await events.read_events(**scope, after=second["next_cursor"]))["events"] == []
    async with get_db_session() as db:
        assert await db.get(AgentDriverState, main.id) is None


@pytest.mark.parametrize("change", ["tampered", "expired", "other_actor", "membership", "future", "window", "gap"])
async def test_cursor_never_silently_skips_a_gap_or_crosses_scope(change, monkeypatch):
    scope, main, other = await setup()
    snap = await get_snapshot(**scope)
    await accept_turn(**scope, main_id=main.id, client_id="one", text="original")
    token = snap["event_cursor"]
    if change == "tampered":
        with pytest.raises(AssistantError) as error:
            await events.read_events(**scope, after="bad")
        assert error.value.status == 400
        return
    if change == "membership":
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))).status = "removed"
        with pytest.raises(AssistantError) as error:
            await events.read_events(**scope, after=token)
        assert error.value.status == 403
        return
    if change == "expired":
        monkeypatch.setattr(events.time, "time", lambda: 9_000_000_000)
    elif change == "other_actor":
        await ensure_main_session(user_id=other, workspace_id=scope["workspace_id"])
        scope["user_id"] = other
    elif change == "future":
        token = events.event_cursor(**scope, main_id=main.id, sequence=100)
    elif change == "window":
        monkeypatch.setattr(events, "REPLAY_WINDOW", 1)
    else:
        async with get_db_session() as db:
            first = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == main.id, AgentEvent.sequence == 1))
            await db.delete(first)
    result = await events.read_events(**scope, after=token)
    assert result["state"] == "snapshot_required" and "events" not in result


async def test_projection_rollback_and_competing_workers_do_not_lose_or_duplicate_control(monkeypatch):
    owner, _, workspace, main, args = await setup_task()
    scope = {"user_id": owner, "workspace_id": workspace}
    accepted = await accept_task_command(**args)
    await accept_control_command(**scope, main_id=main.id, task_id=accepted["task_id"],
        action="pause", expected_revision=1, idempotency_key="pause")
    before = await get_snapshot(**scope)
    append = events.append_agent_event_locked
    async def fail(*args, **kwargs):
        await append(*args, **kwargs)
        raise RuntimeError("projector interrupted before commit")
    monkeypatch.setattr(events, "append_agent_event_locked", fail)
    with pytest.raises(RuntimeError):
        await events.project_task_events(accepted["task_id"])
    async with get_db_session() as db:
        assert await db.get(AssistantEventProjection, accepted["task_id"]) is None
        assert not await db.scalar(select(AgentEvent.id).where(AgentEvent.session_id == main.id,
            AgentEvent.kind == "assistant.control.changed"))
    monkeypatch.setattr(events, "append_agent_event_locked", append)
    counts = await asyncio.gather(*(events.project_task_events(accepted["task_id"]) for _ in range(2)))
    assert sorted(counts)[0] == 0 and sum(counts) >= 3
    assert await events.project_task_events(accepted["task_id"]) == 0
    page = await events.read_events(**scope, after=before["event_cursor"])
    assert any(row["kind"] == "assistant.control.changed" for row in page["events"])
    assert all(row.get("task_id") == accepted["task_id"] for row in page["events"])
    assert len({row["event_id"] for row in page["events"]}) == len(page["events"])
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok


async def test_projection_rechecks_task_audience_and_emits_no_revoked_identifiers():
    owner, _, workspace, main, args = await setup_task()
    accepted = await accept_task_command(**args)
    scope = {"user_id": owner, "workspace_id": workspace}
    before = await get_snapshot(**scope)
    assert await events.project_task_events(accepted["task_id"]) > 0
    async with get_db_session() as db:
        (await db.get(Session, accepted["execution_session_id"])).visibility = "workspace"
    page = await events.read_events(**scope, after=before["event_cursor"])
    assert page["events"] and all(row["kind"] == "assistant.scope.changed" for row in page["events"])
    serialized = json.dumps(page)
    assert accepted["task_id"] not in serialized and accepted["execution_session_id"] not in serialized


async def test_replay_stays_on_one_committed_sql_snapshot(monkeypatch):
    scope, main, _ = await setup()
    async with get_db_session() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("Independent MVCC writer requires PostgreSQL")
    before = await get_snapshot(**scope)
    await accept_turn(**scope, main_id=main.id, client_id="one", text="first")
    project = events._public_event
    wrote = False
    async def interleaved(*args):
        nonlocal wrote
        if not wrote:
            wrote = True
            await accept_turn(**scope, main_id=main.id, client_id="two", text="second")
        return await project(*args)
    monkeypatch.setattr(events, "_public_event", interleaved)
    first = await events.read_events(**scope, after=before["event_cursor"])
    assert len(first["events"]) == 1 and first["next_sequence"] == first["high_water_mark"]
    second = await events.read_events(**scope, after=first["next_cursor"])
    assert len(second["events"]) == 1 and second["events"][0]["sequence"] > first["high_water_mark"]


async def test_large_source_manifests_are_not_loaded_and_byte_limited_replay_advances(monkeypatch):
    scope, main, _ = await setup()
    before = await get_snapshot(**scope)
    async with get_db_session() as db:
        await begin_session_write(db)
        locked = await _lock_fenced(db, main.id, scope["user_id"])
        await ensure_surface_seed_locked(db, locked)
        for _ in range(4):
            await append_agent_event_locked(db, locked, kind="assistant.message.committed",
                payload={"sources": [{"text": "private-source" * 100000}], "report_attempt": 2})
    actual = events._public_event
    async def bounded(db, event, main):
        assert len(json.dumps(event.payload)) < 500
        return await actual(db, event, main)
    monkeypatch.setattr(events, "_public_event", bounded)
    monkeypatch.setattr(events, "PAGE_BYTES", 500)
    cursor, ids, previous = before["event_cursor"], [], 0
    for _ in range(8):
        page = await events.read_events(**scope, after=cursor)
        assert page["next_sequence"] > previous
        assert sum(len(json.dumps(e, ensure_ascii=False).encode()) for e in page["events"]) <= 500
        assert all(e["report_attempt"] == 2 for e in page["events"])
        ids.extend(e["event_id"] for e in page["events"])
        cursor, previous = page["next_cursor"], page["next_sequence"]
        if not page["has_more"]:
            break
    assert len(ids) == len(set(ids)) == 4


async def test_projection_pages_keep_source_identity_and_attachment_receipts_do_not_reapply():
    owner, _, workspace, main, args = await setup_task()
    accepted = await accept_task_command(**args)
    before = await get_snapshot(user_id=owner, workspace_id=workspace)
    async with get_db_session() as db:
        await begin_session_write(db)
        execution = await _lock_fenced(db, accepted["execution_session_id"], owner)
        for status in (None, "failed", "succeeded"):
            await append_agent_event_locked(db, execution, kind="inbox.claimed",
                payload={"item_id": accepted["inbox_id"], "message_id": "fixture-message",
                         "delivery_status": status})
    for _ in range(4):
        assert await events.project_task_events(accepted["task_id"], limit=1) == 1
    assert await events.project_task_events(accepted["task_id"], limit=1) == 0
    page = await events.read_events(user_id=owner, workspace_id=workspace, after=before["event_cursor"])
    applied = [e for e in page["events"] if e["kind"] == "assistant.submission.applied"]
    assert len(applied) == 1 and applied[0]["inbox_id"] == accepted["inbox_id"]
    assert len(page["events"]) == 2


async def test_projection_scan_is_bounded_and_oldest_checkpoint_does_not_starve():
    async with get_db_session() as db:
        if db.get_bind().dialect.name != "sqlite":
            pytest.skip("Global scan ordering uses a fresh isolated database")
    owner, _, workspace, main, args = await setup_task()
    tasks = [await accept_task_command(**{**args, "idempotency_key": f"create-{i}"}) for i in range(3)]
    for index in range(3):
        assert await events.recover_event_projections(limit=1) == 1
        async with get_db_session() as db:
            projected = set((await db.scalars(select(AssistantEventProjection.task_id))).all())
        assert projected == {task["task_id"] for task in tasks[:index + 1]}
    assert await events.recover_event_projections(limit=1) == 0
