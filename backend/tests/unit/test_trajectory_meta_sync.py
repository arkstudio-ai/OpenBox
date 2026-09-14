"""Metadata sync (SPEC §5.8): paged snapshots, cursors, the file_assets rescan, bounded queries."""
import asyncio
from contextlib import suppress
from datetime import timedelta

import pytest
from sqlalchemy import event, update

import db.base as database
from db.models.file_asset import FileAsset
from db.models.session import Session
from db.models.user import User
from db.models.workspace import Workspace
from question import runtime
from tests.unit.test_durable_questions import state  # noqa: F401
from tests.unit.trajectory_producer_support import recording_spool  # noqa: F401
from trajectory import meta_sync

SESSION_FIELDS = {"id", "user_id", "workspace_id", "project_id", "parent_id", "kind", "title", "status", "model",
                  "agent", "is_deleted", "deleted_at", "created_at", "updated_at"}
USER_FIELDS = {"id", "username", "email", "role", "is_active", "is_deleted", "updated_at"}
ASSET_FIELDS = {"id", "user_id", "workspace_id", "session_id", "oss_key", "mime", "size", "status", "is_deleted",
                "deleted_at", "updated_at"}


class Clock:
    """The monotonic clock of the rescan interval, advanced by the test."""

    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


class Now:
    def __init__(self):
        self.value = runtime.now()

    def __call__(self):
        return self.value


@pytest.fixture
def selects(state):
    seen = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)
    engine = database._engine.sync_engine
    event.listen(engine, "before_cursor_execute", listener)
    yield seen
    event.remove(engine, "before_cursor_execute", listener)


async def _seed(*, sessions=(), assets=(), updated_at=None):
    async with database.get_db_session() as db:
        for session_id in sessions:
            db.add(Session(id=session_id, user_id="u1", workspace_id="w1", project_id="p1", title=session_id,
                           agent="build", status="idle", created_at=runtime.now(), updated_at=runtime.now()))
        for asset_id in assets:
            db.add(FileAsset(id=asset_id, user_id="u1", workspace_id="w1", session_id="s1", name=f"{asset_id}.png",
                             oss_key=f"assets/u1/{asset_id}/{asset_id}.png", mime="image/png", size=3,
                             status="ready", created_at=runtime.now()))
        if updated_at is not None:
            await db.flush()
            for model in (Session, User, Workspace):
                await db.execute(update(model).values(updated_at=updated_at))


async def _sync_until_idle(sync, limit: int = 20) -> int:
    for cycle in range(1, limit):
        if not await sync.cycle():
            return cycle
    raise AssertionError("metadata sync never became idle")


def _meta(spool, kind: str, key: str) -> list[dict]:
    return [control[key] for control in spool.controls(kind)]


def test_defaults_follow_the_spec(monkeypatch):
    assert (meta_sync.PAGE_ROWS, meta_sync.RESCAN_SECONDS) == (500, 600)
    monkeypatch.delenv("TRAJECTORY_META_SYNC_SECONDS", raising=False)
    assert meta_sync.MetaSync.interval() == 30
    monkeypatch.setenv("TRAJECTORY_META_SYNC_SECONDS", "5")
    assert meta_sync.MetaSync.interval() == 5


async def test_the_sync_task_runs_only_with_the_spool_sink(monkeypatch):
    started = asyncio.Event()

    async def run(self):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(meta_sync.MetaSync, "run", run)
    monkeypatch.setenv("TRAJECTORY_SINK", "db")
    assert meta_sync.start_meta_sync() is None
    monkeypatch.setenv("TRAJECTORY_SINK", "spool")
    task = meta_sync.start_meta_sync()
    assert task is not None and meta_sync.start_meta_sync() is task
    await asyncio.wait_for(started.wait(), timeout=1)
    await meta_sync.stop_meta_sync()
    assert task.done()
    await meta_sync.stop_meta_sync()


async def test_the_snapshot_pages_each_table_by_primary_key_with_at_most_four_queries_a_cycle(
        state, recording_spool, selects):
    await _seed(sessions=("s3", "s4", "s5"), assets=("a1", "a2", "a3"))
    sync = meta_sync.MetaSync(page_rows=2, clock=Clock(), now=Now())
    cycles = []
    while True:
        before = len(selects)
        pending = await sync.cycle()
        cycles.append(selects[before:])
        if not pending:
            break

    assert len(cycles) == 3
    assert all(len(statements) <= 4 for statements in cycles)
    assert all("LIMIT" in statement.upper() for statements in cycles for statement in statements)
    sessions = _meta(recording_spool, "session.meta", "session")
    assert [row["id"] for row in sessions] == ["s1", "s2", "s3", "s4", "s5"]
    assert set(sessions[0]) == SESSION_FIELDS and sessions[0]["is_deleted"] is False
    assert sessions[0]["updated_at"].endswith("Z")
    users = _meta(recording_spool, "user.meta", "user")
    assert [row["id"] for row in users] == ["u1", "u2"] and set(users[0]) == USER_FIELDS
    assert [(row["id"], set(row)) for row in _meta(recording_spool, "workspace.meta", "workspace")] == [
        ("w1", {"id", "name", "updated_at"})]
    assets = _meta(recording_spool, "asset.meta", "asset")
    assert [row["id"] for row in assets] == ["a1", "a2", "a3"] and set(assets[0]) == ASSET_FIELDS


async def test_changed_rows_follow_the_cursor_once_settled_and_only_once(state, recording_spool):
    clock, now = Clock(), Now()
    start = now.value
    await _seed(updated_at=start - timedelta(hours=1))
    sync = meta_sync.MetaSync(clock=clock, now=now)
    await _sync_until_idle(sync)
    snapshot = len(recording_spool.controls())

    async with database.get_db_session() as db:
        await db.execute(update(Session).where(Session.id == "s1").values(
            title="Renamed", updated_at=start + timedelta(seconds=5)))
        await db.execute(update(Session).where(Session.id == "s2").values(
            title="Later", updated_at=start + timedelta(seconds=55)))
    now.value = start + timedelta(seconds=60)
    await _sync_until_idle(sync)
    changed = recording_spool.controls()[snapshot:]
    # s2 changed too recently: its transaction could still be committing.
    assert [(control["type"], control["session"]["id"], control["session"]["title"]) for control in changed] == [
        ("session.meta", "s1", "Renamed")]

    now.value = start + timedelta(seconds=120)
    await _sync_until_idle(sync)
    await _sync_until_idle(sync)
    assert [control["session"]["id"] for control in recording_spool.controls()[snapshot:]] == ["s1", "s2"]


async def test_file_assets_are_rescanned_in_full_every_ten_minutes(state, recording_spool):
    clock, now = Clock(), Now()
    await _seed(assets=("a1", "a2"))
    sync = meta_sync.MetaSync(clock=clock, now=now)
    await _sync_until_idle(sync)
    first = _meta(recording_spool, "asset.meta", "asset")
    assert [row["id"] for row in first] == ["a1", "a2"]

    async with database.get_db_session() as db:
        await db.execute(update(FileAsset).where(FileAsset.id == "a1").values(
            is_deleted=True, deleted_at=runtime.now()))
    clock.value += meta_sync.RESCAN_SECONDS - 1
    now.value += timedelta(seconds=meta_sync.RESCAN_SECONDS - 1)
    await _sync_until_idle(sync)
    assert len(_meta(recording_spool, "asset.meta", "asset")) == 2

    clock.value += 2
    now.value += timedelta(seconds=2)
    await _sync_until_idle(sync)
    second = _meta(recording_spool, "asset.meta", "asset")[2:]
    assert [(row["id"], row["is_deleted"]) for row in second] == [("a1", True), ("a2", False)]
    # Without an updated_at column the rescan's read time orders the replicas.
    assert second[0]["updated_at"] > first[0]["updated_at"]


async def test_a_failing_table_is_retried_next_cycle_without_stalling_the_others(state, recording_spool,
                                                                              monkeypatch):
    sync = meta_sync.MetaSync(clock=Clock(), now=Now())
    real = sync._scan_page
    failures = {"users": 1}

    async def flaky(table, emitter):
        if failures.get(table.name):
            failures[table.name] -= 1
            raise RuntimeError("business database unavailable")
        return await real(table, emitter)
    monkeypatch.setattr(sync, "_scan_page", flaky)
    await sync.cycle()
    assert _meta(recording_spool, "user.meta", "user") == []
    assert [row["id"] for row in _meta(recording_spool, "session.meta", "session")] == ["s1", "s2"]
    await _sync_until_idle(sync)
    assert [row["id"] for row in _meta(recording_spool, "user.meta", "user")] == ["u1", "u2"]


async def test_rows_the_emitter_refused_are_sent_again(state, recording_spool, monkeypatch):
    from trajectory.emitter import get_emitter
    emitter = get_emitter()
    real = emitter.emit_control
    refusals = {"count": 1}

    def full_queue(control):
        if control["type"] == "session.meta" and refusals["count"]:
            refusals["count"] -= 1
            return False
        return real(control)
    monkeypatch.setattr(emitter, "emit_control", full_queue)
    sync = meta_sync.MetaSync(clock=Clock(), now=Now())
    assert await sync.cycle()
    await _sync_until_idle(sync)
    assert [row["id"] for row in _meta(recording_spool, "session.meta", "session")] == ["s1", "s2"]


async def test_the_loop_survives_a_failed_cycle_and_retries_after_the_interval(monkeypatch):
    calls = []

    async def cycle(self):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("business database down")
        return False
    monkeypatch.setattr(meta_sync.MetaSync, "cycle", cycle)
    monkeypatch.setattr(meta_sync.MetaSync, "interval", staticmethod(lambda: 0.01))
    task = asyncio.create_task(meta_sync.MetaSync().run())
    for _ in range(200):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert len(calls) >= 3
