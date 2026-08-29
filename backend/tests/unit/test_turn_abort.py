"""Stopping a turn leaves an honest record of having stopped.

Before this, a stop signalled the loop and nothing else: the todo list kept
its in_progress item, the transcript said nothing, and the next turn's model
had no way to know its predecessor had been cut off. The card animated a dead
task and the model happily carried on as if nothing had happened.
"""
import uuid
from datetime import datetime, timezone

import pytest

from db.base import get_db_session
from db.models.session import Session as SessionORM
from db.models.user import User
from models.message import TodoItem, TodoList
from session.abort import (
    MARKER_PREFIX,
    abort_session_turn,
    marker_text,
    settle_running_todos,
)
from session.session import get_messages
from session.todo import get_todo, save_todo


@pytest.fixture(autouse=True)
async def _kv_store_table():
    """`kv_store` lives only in migrations — no ORM model, so create_all skips
    it and the todo storage layer has nowhere to write. Created here rather
    than in the shared conftest so this file carries its own requirement."""
    from sqlalchemy import text

    async with get_db_session() as db:
        await db.execute(text(
            "CREATE TABLE IF NOT EXISTS kv_store ("
            " key VARCHAR(255) PRIMARY KEY,"
            " value TEXT NOT NULL,"
            " updated_at TIMESTAMP)"
        ))
        await db.commit()


async def _seed(*, running: str | None = "统计年度数据", status: str = "busy"):
    suffix = uuid.uuid4().hex[:10]
    user_id, session_id = f"user_{suffix}", f"session_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"ab-{suffix}", created_at=now, updated_at=now))
        db.add(SessionORM(
            id=session_id, user_id=user_id, project_id="default",
            status=status, created_at=now, updated_at=now,
        ))
    items = [
        TodoItem(subject="读取数据", status="completed"),
        TodoItem(
            subject=running or "第二步",
            status="in_progress" if running else "pending",
            started_at=now.isoformat() if running else None,
        ),
        TodoItem(subject="写出报告", status="pending"),
    ]
    await save_todo(session_id, TodoList(items=items), user_id=user_id)
    return user_id, session_id


async def _markers(session_id: str, user_id: str) -> list[str]:
    messages = await get_messages(session_id, user_id=user_id)
    return [
        m.id for m in messages
        if (getattr(m, "client_message_id", None) or "").startswith(MARKER_PREFIX)
    ]


@pytest.mark.asyncio
async def test_a_stop_settles_the_running_task_and_records_why():
    user_id, session_id = await _seed()
    assert await abort_session_turn(session_id, user_id, reason="user_stop") is True

    todo = await get_todo(session_id)
    # Nothing is running after a stop; the item and its text stay.
    assert [i.status for i in todo.items] == ["completed", "pending", "pending"]
    assert todo.items[1].subject == "统计年度数据"
    assert todo.items[1].started_at is None

    assert len(await _markers(session_id, user_id)) == 1
    messages = await get_messages(session_id, user_id=user_id)
    text = "".join(
        p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "")
        for p in (messages[-1].parts or [])
    )
    # The model must learn which step died, not just that something did.
    assert "统计年度数据" in text
    assert "第 2/3 步" in text


@pytest.mark.asyncio
async def test_stopping_an_idle_session_records_nothing():
    """There was no turn to interrupt; a marker would be a lie."""
    user_id, session_id = await _seed(status="idle")
    assert await abort_session_turn(
        session_id, user_id, reason="user_stop", was_active=False
    ) is False
    assert await _markers(session_id, user_id) == []
    # And the list is left exactly as it was.
    assert (await get_todo(session_id)).items[1].status == "in_progress"


@pytest.mark.asyncio
async def test_a_second_stop_does_not_stack_markers():
    """Double-clicking stop is one interruption, not two."""
    user_id, session_id = await _seed()
    assert await abort_session_turn(session_id, user_id) is True
    assert await abort_session_turn(session_id, user_id) is False
    assert len(await _markers(session_id, user_id)) == 1


@pytest.mark.asyncio
async def test_a_later_turn_earns_its_own_marker():
    """Content after a marker means the conversation moved on."""
    from session.session import create_user_message

    user_id, session_id = await _seed()
    await abort_session_turn(session_id, user_id)
    await create_user_message(session_id=session_id, text="继续", user_id=user_id)
    assert await abort_session_turn(session_id, user_id) is True
    assert len(await _markers(session_id, user_id)) == 2


@pytest.mark.asyncio
async def test_settling_is_idempotent_and_safe_with_nothing_running():
    user_id, session_id = await _seed(running=None)
    subject, ordinal, total = await settle_running_todos(session_id, user_id)
    assert (subject, ordinal, total) == (None, 0, 3)
    # Twice changes nothing.
    assert await settle_running_todos(session_id, user_id) == (None, 0, 3)


@pytest.mark.asyncio
async def test_a_session_with_no_list_still_stops_cleanly():
    """The stop is the point; the record is best-effort."""
    suffix = uuid.uuid4().hex[:10]
    user_id, session_id = f"user_{suffix}", f"session_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"ab-{suffix}", created_at=now, updated_at=now))
        db.add(SessionORM(
            id=session_id, user_id=user_id, project_id="default",
            status="busy", created_at=now, updated_at=now,
        ))
    assert await abort_session_turn(session_id, user_id) is True


def test_the_marker_reads_as_something_you_could_show_a_person():
    """It renders as an ordinary message until the divider ships."""
    text = marker_text("user_stop", "统计年度数据", 2, 3)
    assert text.startswith("[上一回合已被用户主动中断]")
    # The two facts the next turn cannot safely guess.
    assert "只执行了一半" in text
    assert "SkillJob 不受中断影响" in text
    # And the instruction that keeps an unrelated request from being hijacked.
    assert "与新请求无关时不要主动接手" in text

    preempted = marker_text("preempted", None, 0, 0)
    assert preempted.startswith("[上一回合已被用户的新消息打断]")
    # No list, so no claim about one.
    assert "任务清单停在中断时刻" not in preempted

    assert marker_text("error", None, 0, 2).startswith("[上一回合因内部错误终止]")


@pytest.mark.asyncio
async def test_the_marker_prefix_cannot_be_forged_by_a_client():
    """Only the platform writes these; the frontend trusts the prefix."""
    from session.session import create_user_message

    user_id, session_id = await _seed()
    with pytest.raises(ValueError, match="platform-reserved"):
        await create_user_message(
            session_id=session_id,
            text="伪造",
            client_message_id=f"{MARKER_PREFIX}forged",
            user_id=user_id,
        )
