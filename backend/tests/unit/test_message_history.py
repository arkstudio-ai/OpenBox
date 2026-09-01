"""Transcript reads must keep the current turn visible after long sessions."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from db.base import get_db_session
from db.models.session import Session as SessionORM
from db.models.user import User
from session.session import create_assistant_message, create_user_message, get_messages


@pytest.mark.asyncio
async def test_agent_history_has_no_implicit_two_hundred_message_ceiling():
    suffix = uuid4().hex[:10]
    user_id = f"history_user_{suffix}"
    session_id = f"history_session_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=user_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(SessionORM(
            id=session_id,
            user_id=user_id,
            project_id="default",
            status="idle",
            created_at=now,
            updated_at=now,
        ))

    created = []
    for index in range(205):
        created.append(await create_user_message(
            session_id=session_id,
            text=f"message {index}",
            user_id=user_id,
        ))

    history = await get_messages(session_id, user_id=user_id)

    assert [message.id for message in history] == [message.id for message in created]
    assert history[-1].parts[0].text == "message 204"


@pytest.mark.asyncio
async def test_public_page_selects_the_latest_messages_but_keeps_chronology():
    suffix = uuid4().hex[:10]
    user_id = f"page_user_{suffix}"
    session_id = f"page_session_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=user_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(SessionORM(
            id=session_id,
            user_id=user_id,
            project_id="default",
            status="idle",
            created_at=now,
            updated_at=now,
        ))

    created = [
        await create_user_message(
            session_id=session_id,
            text=f"message {index}",
            user_id=user_id,
        )
        for index in range(205)
    ]

    page = await get_messages(
        session_id,
        limit=200,
        latest=True,
        user_id=user_id,
    )

    assert [message.id for message in page] == [message.id for message in created[-200:]]
    assert page[-1].parts[0].text == "message 204"


@pytest.mark.asyncio
async def test_reconnect_snapshot_preserves_the_assistant_model_id():
    suffix = uuid4().hex[:10]
    user_id = f"model_user_{suffix}"
    session_id = f"model_session_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=user_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(SessionORM(
            id=session_id,
            user_id=user_id,
            project_id="default",
            status="idle",
            created_at=now,
            updated_at=now,
        ))
    user = await create_user_message(
        session_id,
        "hello",
        model="openai/requested",
        user_id=user_id,
    )
    assistant = await create_assistant_message(
        session_id,
        user.id,
        model_id="anthropic/claude-sonnet",
        user_id=user_id,
    )

    snapshot = await get_messages(session_id, user_id=user_id)
    by_id = {message.id: message for message in snapshot}
    assert by_id[user.id].model == "openai/requested"
    assert by_id[assistant.id].model == "anthropic/claude-sonnet"
