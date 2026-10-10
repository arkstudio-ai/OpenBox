"""Startup fold warm-up for the long assistant conversation (V2 P3b)."""
from datetime import timedelta

from sqlalchemy import update

import db.base as database
from assistant import fold_warmup
from db.models.session import Session
from db.models.user import User
from models.message import TextPart
from question import runtime
from session import agent_event_log as log
from session.agent_event_log import load_canonical_model_surface
from session.session import create_assistant_message, create_user_message, save_part, update_message_info
from tests.unit.event_fold_oracle import oracle_model
from tests.unit.test_durable_questions import state  # noqa: F401
from tests.unit.test_event_fold import stored_events


async def chat(session_id, user_id, text, agent="assistant"):
    user = await create_user_message(session_id, text, agent=agent, user_id=user_id)
    answer = await create_assistant_message(session_id, user.id, user_id=user_id)
    await save_part(TextPart(text="Answer " + text, session_id=session_id, message_id=answer.id),
                    is_new=True, user_id=user_id)
    answer.finish = "stop"
    await update_message_info(answer, user_id=user_id)


async def assistants(*rows):
    """One assistant conversation per user, as the unique index requires."""
    async with database.get_db_session() as db:
        for session_id, user_id, _deleted, _age in rows:
            if await db.get(User, user_id) is None:
                db.add(User(id=user_id, username=user_id, created_at=runtime.now(), updated_at=runtime.now()))
                await db.flush()
            db.add(Session(id=session_id, user_id=user_id, workspace_id="w1", project_id="p1", title="Assistant",
                           agent="assistant", kind="assistant", status="idle",
                           created_at=runtime.now(), updated_at=runtime.now()))


async def settle(*rows):
    async with database.get_db_session() as db:
        for session_id, _user_id, deleted, age in rows:
            await db.execute(update(Session).where(Session.id == session_id).values(
                is_deleted=deleted, updated_at=runtime.now() - timedelta(minutes=age)))


async def test_the_first_turn_after_a_restart_reads_only_new_events(state, monkeypatch):
    monkeypatch.setattr(log, "FOLD_CACHE_MIN_EVENTS", 1)
    rows = (("main", "u3", False, 0),)
    await assistants(*rows)
    for index in range(3):
        await chat("main", "u3", f"turn {index}")
    await chat("s1", "u1", "an ordinary conversation", agent="build")
    log.clear_event_fold_cache()  # a restarted process

    assert await fold_warmup.warm_assistant_folds() == 1
    assert ("main", "u3") in log._FOLD_CACHE
    assert ("s1", "u1") not in log._FOLD_CACHE  # not an assistant conversation

    warmed = log._FOLD_CACHE[("main", "u3")].sequence
    starts = []
    rows = log._event_rows

    async def recorded(db, session_row, *, first_sequence, limit=None):
        starts.append(first_sequence)
        return await rows(db, session_row, first_sequence=first_sequence, limit=limit)

    monkeypatch.setattr(log, "_event_rows", recorded)
    await chat("main", "u3", "after the restart")
    surface = await load_canonical_model_surface("main", user_id="u3")
    # The first turn resumes from the warmed prefix; nothing replays from the start.
    assert starts and starts[0] == warmed and 1 not in starts
    assert surface.messages == oracle_model(await stored_events("main")).messages

    # Without the warm-up the same turn replays the whole conversation.
    log.clear_event_fold_cache()
    starts.clear()
    await chat("main", "u3", "after another restart")
    await load_canonical_model_surface("main", user_id="u3")
    assert starts[0] == 1


async def test_deleted_empty_and_older_conversations_are_left_cold(state, monkeypatch):
    monkeypatch.setattr(log, "FOLD_CACHE_MIN_EVENTS", 1)
    rows = (("a-new", "u3", False, 1), ("a-old", "u4", False, 30),
            ("a-gone", "u5", True, 0), ("a-empty", "u6", False, 0))
    await assistants(*rows)
    for session_id, user_id, *_ in rows[:3]:
        await chat(session_id, user_id, "hello")
    await settle(*rows)
    log.clear_event_fold_cache()

    # The newest two live ones are the empty one (no events, skipped) and "a-new".
    assert await fold_warmup.warm_assistant_folds(limit=2) == 1
    assert set(log._FOLD_CACHE) == {("a-new", "u3")}
    assert await fold_warmup.warm_assistant_folds() == 2
    assert set(log._FOLD_CACHE) == {("a-new", "u3"), ("a-old", "u4")}
    # The most recently active is the freshest entry.
    assert list(log._FOLD_CACHE)[-1] == ("a-new", "u3")

    monkeypatch.setattr(log, "FOLD_CACHE_SIZE", 0)  # caching turned off
    log.clear_event_fold_cache()
    assert await fold_warmup.warm_assistant_folds() == 0


async def test_a_failed_warm_up_never_reaches_startup(state, monkeypatch):
    async def broken(limit=fold_warmup.WARM_LIMIT):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(fold_warmup, "warm_assistant_folds", broken)
    fold_warmup.schedule_fold_warmup()
    task = fold_warmup._task
    await task
    assert task.exception() is None
    await fold_warmup.stop_fold_warmup()
    assert fold_warmup._task is None
