"""Canonical reads reuse one locked prefix without hiding recovery or corruption."""
from contextlib import contextmanager

import pytest
from sqlalchemy import event, func, select, update

import db.base as database
from db.models.agent_event import AgentEvent
from models.message import TextPart
from session.agent_event_log import (
    AgentEventProjectionError,
    load_canonical_model_surface,
)
from session.session import (
    create_assistant_message,
    create_user_message,
    save_part,
    update_message_info,
)
from tests.unit.test_durable_questions import state  # noqa: F401


@contextmanager
def history_reads():
    """Count full payload scans, excluding small seed/head/fence queries."""
    queries = []

    def count(conn, cursor, statement, parameters, context, executemany):
        sql = " ".join(statement.lower().split())
        if ("from agent_events" in sql and "agent_events.payload" in sql
                and "order by agent_events.sequence" in sql):
            queries.append(sql)

    engine = database._engine.sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        yield queries
    finally:
        event.remove(engine, "before_cursor_execute", count)


async def closed_turn(text):
    user = await create_user_message("s1", text, user_id="u1")
    answer = await create_assistant_message("s1", user.id, user_id="u1")
    await save_part(TextPart(text="Answer " + text, session_id="s1", message_id=answer.id),
                    is_new=True, user_id="u1")
    answer.finish = "stop"
    await update_message_info(answer, user_id="u1")
    return answer


async def test_closed_history_is_fetched_once_and_later_commits_are_visible(state):
    answers = [await closed_turn(str(index)) for index in range(8)]
    with history_reads() as reads:
        before = await load_canonical_model_surface("s1", user_id="u1")
    assert len(reads) == 1
    assert len(before.messages) == 16
    assert before.messages[-1].id == answers[-1].id

    # Reuse ends at the transaction boundary: the next read must include new
    # input and cannot inherit a caller's mutation of its returned objects.
    before.messages[-1].parts[0].text = "caller mutation"
    latest = await closed_turn("new turn")
    with history_reads() as reads:
        after = await load_canonical_model_surface("s1", user_id="u1")
    assert len(reads) == 1
    assert after.event_sequence > before.event_sequence
    assert after.event_digest != before.event_digest
    assert after.messages[-1].id == latest.id
    assert after.messages[-3].parts[0].text == "Answer 7"


async def test_recovery_returns_the_appended_prefix_then_returns_to_one_read(state):
    user = await create_user_message("s1", "Interrupted before answering", user_id="u1")
    before = await load_canonical_model_surface("s1", user_id="u1", repair_tail=False)
    with history_reads() as reads:
        repaired = await load_canonical_model_surface("s1", user_id="u1")
    assert len(reads) == 2
    assert repaired.event_sequence > before.event_sequence
    assert repaired.messages[-1].parent_id == user.id
    assert repaired.messages[-1].finish == "aborted"
    with history_reads() as reads:
        stable = await load_canonical_model_surface("s1", user_id="u1")
    assert len(reads) == 1
    assert stable == repaired


@pytest.mark.parametrize("repair_tail", [False, True])
async def test_full_prefix_validation_still_rejects_sequence_gaps(state, repair_tail):
    await closed_turn("A valid turn")
    async with database.get_db_session() as db:
        last = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == "s1")
                               .order_by(AgentEvent.sequence.desc()).limit(1))
        await db.execute(update(AgentEvent).where(AgentEvent.id == last.id)
                         .values(sequence=last.sequence + 1))
    with pytest.raises(AgentEventProjectionError, match="sequence gap"):
        await load_canonical_model_surface("s1", user_id="u1", repair_tail=repair_tail)


async def test_large_replay_allows_another_request_to_progress(state, monkeypatch):
    import asyncio
    import threading
    import session.agent_event_log as event_log

    await closed_turn("Concurrent replay")
    monkeypatch.setattr(event_log, "PROJECTION_THREAD_MIN_EVENTS", 1)
    original = event_log._project_model_surface
    started = asyncio.Event()
    other_request_finished = threading.Event()
    progress = []
    loop = asyncio.get_running_loop()

    def heavy_projection(events, public):
        loop.call_soon_threadsafe(started.set)
        # A second request must be able to run while replay is still working.
        # The timeout makes a regression fail rather than hang the test suite.
        progress.append(other_request_finished.wait(timeout=2))
        return original(events, public)

    async def other_request():
        await started.wait()
        other_request_finished.set()

    monkeypatch.setattr(event_log, "_project_model_surface", heavy_projection)
    replay, _ = await asyncio.gather(
        load_canonical_model_surface("s1", user_id="u1"), other_request(),
    )
    assert progress == [True]
    assert replay.messages[-1].parts[0].text == "Answer Concurrent replay"


def test_normalized_replay_preserves_legacy_digest_and_original_payload():
    from copy import deepcopy
    from session.agent_event_log import (
        event_prefix_digest, project_model_agent_events, json_safe_copy,
    )
    payload = {
        "version": 1,
        "surface": {"version": 1, "session_id": "s", "messages": []},
        "model": {"version": 1, "part_replay": {}, "provider_replay": []},
        "values": {"中文": "字😀\x00", "literal": r"\u0000", "big": 2 ** 100,
                   "floats": [0.0, -0.0, 1e-20], "nested": [{"value": "kept"}]},
    }
    event = dict(session_id="s", sequence=1, event_key="a" * 64, kind="surface.seed",
                 run_id=None, generation=None, turn_id=None, step_id=None,
                 message_id=None, part_id=None, tool_call_id=None, payload=payload)
    original = deepcopy(event)
    projected = project_model_agent_events([event])
    assert projected.event_digest == event_prefix_digest([event])
    # Golden digest produced by d50f49b, before the latency changes.
    assert projected.event_digest == "8e761e05cc94d0d16cd6e5703014061dd4b3de56a53fab1933143ca8f6cd83b1"
    assert event == original
    copied = json_safe_copy(payload)
    assert copied["values"]["中文"] == "字😀"
    assert copied["values"]["literal"] == r"\u0000"
    assert copied["values"]["big"] == 2 ** 100


async def test_cancelled_large_projection_cannot_commit_tail_recovery(state, monkeypatch):
    import asyncio
    import threading
    import session.agent_event_log as event_log

    await create_user_message("s1", "Interrupted turn", user_id="u1")
    async with database.get_db_session() as db:
        count_before = await db.scalar(select(func.count()).select_from(AgentEvent))
    monkeypatch.setattr(event_log, "PROJECTION_THREAD_MIN_EVENTS", 1)
    original = event_log._project_model_surface
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_projection(events, public):
        loop.call_soon_threadsafe(started.set)
        try:
            release.wait(timeout=2)
            return original(events, public)
        finally:
            finished.set()

    monkeypatch.setattr(event_log, "_project_model_surface", blocked_projection)
    task = asyncio.create_task(load_canonical_model_surface("s1", user_id="u1"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 2)
    async with database.get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AgentEvent)) == count_before

    monkeypatch.setattr(event_log, "_project_model_surface", original)
    recovered = await load_canonical_model_surface("s1", user_id="u1")
    assert recovered.messages[-1].finish == "aborted"
