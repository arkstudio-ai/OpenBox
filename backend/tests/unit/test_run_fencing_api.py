"""Run fencing is enforced by the execution runtime, whether recording is on or off.

These tests drive question.runtime against the migration-backed durable
question schema, so OBX_QUESTION_TEST_DATABASE_URL runs the same statements on
PostgreSQL. The fixtures and helpers here are shared by the other
test_run_fencing modules.
"""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import orjson
import pytest
from sqlalchemy import event, update

import db.base as database
from db.models.question import SessionExecution
from db.models.session import Session
from question import runtime
from session import status
from tests.unit.test_durable_questions import read, state  # noqa: F401
from trajectory.types import TrajectoryError


@pytest.fixture(params=[False, True], ids=["recording-off", "recording-on"])
def recording(request, monkeypatch):
    """Fencing must not depend on recording; recording on hands facts to the spool emitter."""
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true" if request.param else "false")
    monkeypatch.delenv("TRAJECTORY_RECORD_USER_IDS", raising=False)
    return request.param


@pytest.fixture
def statements(state):
    """SQL statements sent while the test runs (transaction control excluded)."""
    seen = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)
    engine = database._engine.sync_engine
    event.listen(engine, "before_cursor_execute", listener)
    yield seen
    event.remove(engine, "before_cursor_execute", listener)


@pytest.fixture
def loop_harness(state, monkeypatch):
    """The real agent loop with a scripted provider and no sandbox."""
    from agent import loop, processor
    from agent.agent import AgentDef
    from agent.tool_resolution import ResolvedStepTools
    from core.config import get_config
    config = get_config().model_copy(deep=True)
    config.tool_exposure.mode = "legacy_eager"
    config.model = "openai/gpt-4o"
    config.models = []
    config.permission = {"*": "allow"}
    config.jwt_secret = ""
    monkeypatch.setattr("core.config.get_config", lambda: config)
    harness = SimpleNamespace(loop=loop, processor=processor, tools={})

    async def no_sandbox(*args, **kwargs):
        return None

    async def system(*args, **kwargs):
        return ["Test conversation"]

    async def tools(*args, **kwargs):
        return ResolvedStepTools(tools=dict(harness.tools), catalogue_availability="available")
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", no_sandbox)
    monkeypatch.setattr("sandbox.entitlement.subscription_sandbox_enabled", lambda: False)
    monkeypatch.setattr(loop, "_build_system_prompt", system)
    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    monkeypatch.setattr(loop, "get_agent", lambda _name: AgentDef(name="build", description="test"))
    return harness


@contextmanager
def acting_as(ticket):
    """Bind a run the way run_loop binds it for everything the run awaits."""
    token = runtime.current_run.set(ticket)
    try:
        yield ticket
    finally:
        runtime.current_run.reset(token)


async def supersede_elsewhere(session_id: str = "s1") -> None:
    """Another worker accepted new input: the row moves on and this process is not told."""
    async with database.get_db_session() as db:
        await db.execute(update(SessionExecution).where(SessionExecution.session_id == session_id).values(
            generation=SessionExecution.generation + 1, run_id=None, lease_until=None))


async def expire_lease(session_id: str = "s1") -> None:
    async with database.get_db_session() as db:
        await db.execute(update(SessionExecution).where(SessionExecution.session_id == session_id).values(
            lease_until=runtime.now() - timedelta(seconds=1)))


async def add_session(session_id: str, *, user_id: str = "u1", **fields) -> None:
    async with database.get_db_session() as db:
        db.add(Session(id=session_id, user_id=user_id, workspace_id="w1", project_id="p1", title=session_id,
                       agent="build", status="idle", created_at=runtime.now(), updated_at=runtime.now(),
                       **fields))


@pytest.fixture
def emitted(monkeypatch) -> list[dict]:
    """Every event the spool emitter accepts, decoded, in order: the facts recording writes.

    A fact bound to a transaction reaches the emitter only when that transaction
    commits; the trajectory worker keeps the first copy of a repeated event id.
    """
    from trajectory.emitter import EVENT, Emitter
    events = []
    emit_bytes, enqueue_encoded = Emitter.emit_bytes, Emitter.enqueue_encoded

    def emitted_now(self, event_json, **routing):
        events.append(orjson.loads(event_json))
        return emit_bytes(self, event_json, **routing)

    def emitted_after_commit(self, kind, payload, routing):
        if kind == EVENT:
            events.append(orjson.loads(payload))
        return enqueue_encoded(self, kind, payload, routing)
    monkeypatch.setattr(Emitter, "emit_bytes", emitted_now)
    monkeypatch.setattr(Emitter, "enqueue_encoded", emitted_after_commit)
    return events


def facts(events: list[dict], *types: str) -> list[dict]:
    return [fact for fact in events if fact["type"] in types]


def published(events: list, kind: str) -> list:
    return [data for published_kind, data in events if published_kind == kind]


def test_run_revoked_is_not_an_ordinary_error_and_revokes_its_run():
    ticket = runtime.RunTicket("s1", "u1", 0, uuid4().hex)
    error = runtime.RunRevoked(ticket, "superseded", "write")
    # Plain `except ValueError` handlers (question APIs, agent switches) must not swallow it.
    assert not isinstance(error, (ValueError, TrajectoryError))
    assert (error.ticket, error.reason, error.boundary) == (ticket, "superseded", "write")
    # A handler that swallows it still leaves the run revoked for its next boundary.
    assert runtime.is_revoked(ticket.run_id)


def test_write_rule_outlives_the_run_while_the_start_rule_needs_the_live_lease():
    ticket = runtime.RunTicket("s1", "u1", 3, "run")
    moment = runtime.now()
    live = SimpleNamespace(generation=3, run_id="run", lease_until=moment + timedelta(seconds=30))
    finished = SimpleNamespace(generation=3, run_id=None, lease_until=None)
    replaced = SimpleNamespace(generation=3, run_id="other", lease_until=live.lease_until)
    superseded = SimpleNamespace(generation=4, run_id=None, lease_until=None)
    expired = SimpleNamespace(generation=3, run_id="run", lease_until=moment - timedelta(seconds=1))
    rows = (live, finished, replaced, superseded, expired)
    assert [runtime.write_verdict(row, ticket) for row in rows] == [
        None, None, "replaced", "superseded", None]
    assert [runtime.start_verdict(row, ticket, moment) for row in rows] == [
        None, "lease_lost", "replaced", "superseded", "lease_lost"]
    assert runtime.write_verdict(None, ticket) == runtime.start_verdict(None, ticket, moment) == "deleted"


async def test_lease_check_is_one_statement_and_classifies_refusals(state, statements):
    ticket = await runtime.start_run("s1", "u1")
    statements.clear()
    with acting_as(ticket):
        await runtime.assert_current("tool", progress=True)
        # The adapter's own request check reuses the loop's check just before it.
        await runtime.assert_current("request")
    assert len(statements) == 1 and statements[0].lstrip().upper().startswith("UPDATE")
    assert (await read(SessionExecution, "s1")).run_progress

    await expire_lease()
    with acting_as(ticket), pytest.raises(runtime.RunRevoked) as refused:
        await runtime.assert_current("step")
    assert (refused.value.reason, refused.value.boundary) == ("lease_lost", "step")
    statements.clear()
    with acting_as(ticket), pytest.raises(runtime.RunRevoked):
        await runtime.assert_current("tool", progress=True)
    assert statements == []  # An in-process revocation is refused for free.

    await runtime.recover_expired_runs()
    second = await runtime.start_run("s1", "u1")
    await supersede_elsewhere()
    with acting_as(second), pytest.raises(runtime.RunRevoked) as refused:
        await runtime.assert_current("spawn")
    assert refused.value.reason == "superseded"
    assert not await runtime.still_current(second)


async def test_transaction_applies_the_write_rule_to_the_bound_session_only(state):
    from session.session import create_user_message
    ticket = await runtime.start_run("s1", "u1")
    with acting_as(ticket):
        async with runtime.transaction("s1", "u1") as (_, session, _):
            session.title = "written while owned"
    await create_user_message("s1", "Replacement", user_id="u1")
    with acting_as(ticket):
        with pytest.raises(runtime.RunRevoked) as refused:
            async with runtime.transaction("s1", "u1") as (_, session, _):
                session.title = "late write"
        assert (refused.value.reason, refused.value.boundary) == ("superseded", "write")
        # Runtime transitions decide ownership themselves.
        async with runtime.transaction("s1", "u1", fence=False) as (_, _, execution):
            assert not runtime.owns(execution, ticket)
        # Another session is not fenced by this run's ticket.
        async with runtime.transaction("s2", "u2") as (_, other, _):
            other.title = "unrelated write"
    assert (await read(Session, "s1")).title == "written while owned"
    assert (await read(Session, "s2")).title == "unrelated write"


async def test_auxiliary_ticket_may_outlive_its_run_but_never_its_turn(state):
    from session.session import create_user_message
    ticket = await runtime.start_run("s1", "u1")
    await runtime.finish_run(ticket, completed=True)

    async def identities():
        await runtime.assert_current("request")
        return runtime.current_run.get(), runtime.auxiliary_run.get()
    with acting_as(ticket):
        run, auxiliary = await runtime.run_auxiliary(ticket, "title", identities())
        assert runtime.current_run.get() == ticket
    assert run is None and (auxiliary.run_id, auxiliary.purpose) == (ticket.run_id, "title")
    # A later run of the same turn does not end the auxiliary work.
    later = await runtime.start_run("s1", "u1")
    await runtime.run_auxiliary(ticket, "suggestions", runtime.assert_current("request"))
    await runtime.finish_run(later)

    await create_user_message("s1", "A new turn", user_id="u1")
    with pytest.raises(runtime.RunRevoked) as refused:
        await runtime.run_auxiliary(ticket, "title", runtime.assert_current("request"))
    assert refused.value.reason == "superseded"


async def test_session_fields_are_fenced_but_token_counters_are_not(state):
    from models.message import TokenUsage
    from session.session import update_session, update_session_tokens
    ticket = await runtime.start_run("s1", "u1")
    with acting_as(ticket):
        await update_session("s1", user_id="u1", agent="plan")
    await supersede_elsewhere()
    with acting_as(ticket):
        with pytest.raises(runtime.RunRevoked):
            await update_session("s1", user_id="u1", agent="explore")
        # Consumption that already happened is still counted.
        await update_session_tokens("s1", TokenUsage(input=5, output=2, total=7), user_id="u1")
    saved = await read(Session, "s1")
    assert saved.agent == "plan" and saved.token_usage["total"] == 7


async def test_revoke_signals_only_that_run_and_arms_nothing_for_later_runs(state):
    first_run, second_run = uuid4().hex, uuid4().hex
    first = status.register_run("s1", first_run)
    runtime.revoke(first_run, "superseded")
    assert first.is_set()
    status.clear_abort("s1", first)

    second = status.register_run("s1", second_run)
    runtime.revoke(first_run, "superseded")  # A late revocation of the replaced run.
    assert not second.is_set()
    status.clear_abort("s1", second)

    runtime.revoke(uuid4().hex, "superseded")  # No run of this process holds a signal.
    assert not status.register_run("s1", uuid4().hex).is_set()


async def test_heartbeat_extends_the_lease_and_stops_a_run_superseded_elsewhere(state, monkeypatch):
    monkeypatch.setattr(runtime, "LEASE_SECONDS", 0.6)
    ticket = await runtime.start_run("s1", "u1")
    granted = runtime.utc((await read(SessionExecution, "s1")).lease_until)
    abort = asyncio.Event()
    beat = asyncio.create_task(runtime.heartbeat(ticket, abort))
    try:
        await asyncio.sleep(0.5)
        assert runtime.utc((await read(SessionExecution, "s1")).lease_until) > granted
        assert not abort.is_set()
        await supersede_elsewhere()
        await asyncio.wait_for(beat, timeout=5)
    finally:
        beat.cancel()
    assert abort.is_set() and runtime.is_revoked(ticket.run_id)
