"""Current assistant session observations must not borrow held ORM values."""
from datetime import timedelta

import pytest
from sqlalchemy import text

from assistant.business_context import capture_locked, validate
from assistant.policy import AssistantError
from assistant.reads import list_sessions
from assistant.service import ensure_main_session
from db.base import get_db_session, get_engine
from db.models.session import Session
from session.session import create_session
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def session_world():
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    session = await create_session(user_id=owner, workspace_id=workspace, title="Observed conversation")
    return main, session.id


@pytest.mark.parametrize("include_link", [False, True])
async def test_fresh_observation_detects_progress_committed_after_held_session_read(include_link):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent READ COMMITTED connections require PostgreSQL")
    main, session_id = await session_world()
    async with get_db_session() as db:
        held = await db.get(Session, session_id)
        original_status, original_updated = held.status, held.updated_at
        _, observation = await capture_locked(db, main, "sessions.list", {"include_link": include_link})
        await validate(db, main, observation, fresh=True)
        reader_pid = await db.scalar(text("select pg_backend_pid()"))
        async with get_db_session() as writer:
            assert await writer.scalar(text("select pg_backend_pid()")) != reader_pid
            changed = await writer.get(Session, session_id)
            changed.status = "busy"
            changed.updated_at += timedelta(seconds=1)
        assert (held.status, held.updated_at) == (original_status, original_updated)
        # Historical evidence remains valid; only the current provider view
        # must be rebuilt after progress changes in another transaction.
        await validate(db, main, observation)
        with pytest.raises(AssistantError) as changed:
            await validate(db, main, observation, fresh=True)
        assert changed.value.code == "ASSISTANT_BUSINESS_SNAPSHOT_CHANGED"
        assert (held.status, held.updated_at) == (original_status, original_updated)


@pytest.mark.parametrize("include_link", [False, True])
async def test_current_list_does_not_flush_or_overwrite_pending_session_edits(include_link):
    main, session_id = await session_world()
    async with get_db_session() as db:
        held = await db.get(Session, session_id)
        old_title, old_status = held.title, held.status
        held.title, held.status = "Pending caller edit", "busy"
        with db.no_autoflush:
            result = await list_sessions(db=db, user_id=main.user_id,
                workspace_id=main.workspace_id, main_id=main.id, include_link=include_link)
        item = next(row for row in result["items"] if row["id"] == session_id)
        assert (item["title"], item["status"]) == (old_title, old_status)
        assert (held.title, held.status) == ("Pending caller edit", "busy")
        await db.rollback()
