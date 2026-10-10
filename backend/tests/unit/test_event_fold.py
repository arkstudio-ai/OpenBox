"""EventFold: incremental replay that equals a full replay (V2 P3b).

The single personal-assistant conversation is never replaced by a new session,
so its event log only grows. Loads resume the fold of the previous prefix and
apply the newer events; these tests hold that to the frozen full-replay
projectors in tests/unit/event_fold_oracle.py.
"""
from contextlib import contextmanager

import pytest
from sqlalchemy import delete, event, select

import db.base as database
from db.models.agent_event import AgentEvent
from models.message import TextPart
from session import agent_event_log as log
from session.agent_event_log import AgentEventProjectionError, EventFold, load_canonical_model_surface
from session.session import create_assistant_message, create_user_message, save_part, update_message_info
from tests.unit.event_fold_oracle import oracle_model, oracle_public, oracle_turn_maps
from tests.unit.test_durable_questions import state  # noqa: F401

FIELDS = ("id", "session_id", "sequence", "event_key", "kind", "run_id", "generation", "turn_id", "step_id",
          "message_id", "part_id", "tool_call_id", "payload")


async def turn(text, *, tool=False):
    user = await create_user_message("s1", text, user_id="u1")
    answer = await create_assistant_message("s1", user.id, user_id="u1")
    await save_part(TextPart(text="Answer " + text, session_id="s1", message_id=answer.id), is_new=True, user_id="u1")
    answer.finish = "stop"
    await update_message_info(answer, user_id="u1")
    return answer


async def stored_events(session_id="s1"):
    async with database.get_db_session() as db:
        rows = (await db.scalars(select(AgentEvent).where(AgentEvent.session_id == session_id)
                                 .order_by(AgentEvent.sequence))).all()
        return [{name: getattr(row, name) for name in FIELDS} for row in rows]


def outcome(projector):
    try:
        return "ok", projector()
    except AgentEventProjectionError as exc:
        return "error", type(exc).__name__


@contextmanager
def history_reads():
    queries = []

    def count(conn, cursor, statement, parameters, context, executemany):
        sql = " ".join(statement.lower().split())
        if "from agent_events" in sql and "agent_events.payload" in sql and "order by agent_events.sequence" in sql:
            queries.append(sql)

    engine = database._engine.sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        yield queries
    finally:
        event.remove(engine, "before_cursor_execute", count)


async def test_every_prefix_and_every_resume_point_equals_a_full_replay(state):
    for index in range(6):
        await turn(f"turn {index}")
    await load_canonical_model_surface("s1", user_id="u1")
    events = await stored_events()
    assert len(events) > 20
    for end in range(1, len(events) + 1):
        prefix = events[:end]
        fold = EventFold.replay(prefix)
        assert fold.public_surface() == oracle_public(prefix)
        assert outcome(fold.model_surface) == outcome(lambda: oracle_model(prefix))
        assert outcome(fold.turn_index) == outcome(lambda: oracle_turn_maps(prefix))
        assert fold.digest == log.event_prefix_digest(prefix)
        assert fold.digest_at(end) == fold.digest
    full = EventFold.replay(events)
    for start in range(1, len(events)):
        resumed = EventFold.replay(events[:start])
        resumed.shared = True  # published folds are never changed in place
        copy = resumed.clone()
        for item in events[start:]:
            copy.apply(item)
        assert copy.public_surface() == full.public_surface()
        assert copy.model_surface() == full.model_surface()
        assert copy.turn_index() == full.turn_index()
        assert copy.digest == full.digest
        # The resumed prefix is untouched by its copy's progress.
        assert resumed.public_surface() == oracle_public(events[:start])


async def test_a_published_fold_cannot_change_and_its_copy_does_not_leak(state):
    await turn("first")
    events = await stored_events()
    fold = EventFold.replay(events[:-1])
    fold.shared = True
    with pytest.raises(RuntimeError):
        fold.apply(events[-1])
    copy = fold.clone()
    copy.apply(events[-1])
    assert fold.sequence == len(events) - 1 and copy.sequence == len(events)
    before = oracle_public(events[:-1])
    assert fold.public_surface() == before


async def test_a_long_session_reads_only_new_events_and_rejects_a_changed_prefix(state, monkeypatch):
    monkeypatch.setattr(log, "FOLD_CACHE_MIN_EVENTS", 1)
    for index in range(3):
        await turn(f"turn {index}")
    first = await load_canonical_model_surface("s1", user_id="u1")
    assert ("s1", "u1") in log._FOLD_CACHE
    cached = log._FOLD_CACHE[("s1", "u1")]
    assert cached.shared and cached.sequence == first.event_sequence
    await turn("later")
    with history_reads() as reads:
        later = await load_canonical_model_surface("s1", user_id="u1")
    # One read, starting at the cached prefix's last event.
    assert len(reads) == 1 and "agent_events.sequence >=" in reads[0]
    assert later.event_sequence > first.event_sequence
    assert later == (await load_canonical_model_surface("s1", user_id="u1"))
    events = await stored_events()
    assert later.event_digest == log.event_prefix_digest(events)
    assert later.messages == oracle_model(events).messages

    # A different history under the same ids (a replaced database, a restore)
    # is detected by the last event's id, then replayed in full.
    async with database.get_db_session() as db:
        last = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == "s1")
                               .order_by(AgentEvent.sequence.desc()).limit(1))
        await db.execute(delete(AgentEvent).where(AgentEvent.id == last.id))
        replacement = AgentEvent(**{name: getattr(last, name) for name in FIELDS if name != "id"},
                                 id=last.id + "x", created_at=last.created_at, user_id=last.user_id)
        db.add(replacement)
    with history_reads() as reads:
        again = await load_canonical_model_surface("s1", user_id="u1", repair_tail=False)
    assert len(reads) == 2  # the stale check, then the full replay
    assert again.event_digest == later.event_digest


async def test_a_fold_read_in_a_rolled_back_transaction_is_not_published(state, monkeypatch):
    monkeypatch.setattr(log, "FOLD_CACHE_MIN_EVENTS", 1)
    await turn("only")
    with pytest.raises(RuntimeError, match="abandon"):
        async with database.get_db_session() as db:
            row = await log.prepare_agent_event_write(db, session_id="s1", user_id="u1", run_fence=None)
            await log.load_event_fold_locked(db, row)
            raise RuntimeError("abandon")
    assert ("s1", "u1") not in log._FOLD_CACHE
    await load_canonical_model_surface("s1", user_id="u1", repair_tail=False)
    assert ("s1", "u1") in log._FOLD_CACHE
