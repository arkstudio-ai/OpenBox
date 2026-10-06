"""V2 history pages are stored transcripts behind one current authority read.

PERSONAL_ASSISTANT_DESIGN_V2.md 4.2: history, snapshots and unread state are
ordinary paged reads plus current permission; nothing re-validates saved
answers, so there is no per-snapshot authority memo to reuse either.
"""
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from assistant import commands
from assistant.policy import AssistantError
from assistant.public_history import public_messages
from assistant.transactions import source_snapshot
from db.base import get_db_session, get_engine
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from session.session import get_message_window, get_session
from tests.unit.assistant_helpers import no_task_dispatch  # noqa: F401
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.assistant_helpers import stable
from tests.unit.assistant_helpers import sql_reads
from tests.unit.assistant_helpers import repeated_result  # noqa: F401


@contextmanager
def history_sql_reads():
    statements = []
    def count(_connection, _cursor, statement, _parameters, _context, _many):
        upper = statement.lstrip().upper()
        if upper.startswith("SELECT"):
            statements.append(statement)
        else:
            assert upper == "BEGIN" or upper.startswith("SET TRANSACTION")
    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", count)


async def test_history_page_is_returned_as_stored_with_one_timing_read(repeated_result):
    scope, _ = repeated_result
    session = await get_session(scope["main_id"], user_id=scope["user_id"])
    window = await get_message_window(session.id, user_id=scope["user_id"], turns=8)
    answers = [message for message in window.messages if message.role == "assistant" and message.finish == "stop"]
    assert len(answers) == 4
    with history_sql_reads() as queries:
        page = await public_messages(session, window.messages, actor_user_id=scope["user_id"])
    # No source graph walk: one batched timing read for all four answers
    # (V2 4.2/15 budgets a whole history page at six statements).
    assert len(queries) <= 2
    for value, message in zip(page, window.messages):
        assert {key: item for key, item in value.items() if key != "assistant_timing"} == message.model_dump()
    timed = [value for value in page if "assistant_timing" in value]
    assert {value["id"] for value in timed} == {message.id for message in answers}
    assert stable(await public_messages(session, window.messages, actor_user_id=scope["user_id"])) == stable(page)


async def test_authority_is_one_current_read_per_call_inside_a_snapshot(repeated_result):
    scope, _ = repeated_result
    async with source_snapshot() as (db, _):
        with sql_reads() as reads:
            main = await commands._authority(db, **scope)
            assert await commands._authority(db, **scope) is main
        # No snapshot memo: each authority check is its own current read.
        assert len(reads) == 2


@pytest.mark.parametrize("changed", ["membership", "main"])
async def test_next_authority_read_rejects_external_revocation(repeated_result, changed):
    scope, _ = repeated_result
    expected = "ASSISTANT_WORKSPACE_FORBIDDEN" if changed == "membership" else "ASSISTANT_UNAVAILABLE"
    async with source_snapshot() as (db, _):
        held = await commands._authority(db, **scope)
    async with get_db_session() as writer:
        if changed == "membership":
            (await writer.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))).status = "removed"
        else:
            (await writer.get(Session, held.id)).memory_policy = "standard"
    async with source_snapshot() as (db, _):
        with pytest.raises(AssistantError) as rejected:
            await commands._authority(db, **scope)
        assert rejected.value.code == expected
