"""Chat views read history newest turns first, and catch up from a cursor.

A client opening a long chat used to page through every message, oldest
first, and re-download all of it about once a second while a run was live:
4,515 requests and 2.6 GB for one 350-message session in a day (2026-09-14).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from core.identifier import ascending
from db.base import get_db_session
from db.models.message import Message as MessageORM
from db.models.part import Part as PartORM
from db.repository.user_repo import PgUserRepo
from session.session import HistoryCursorGone, create_session, get_message_window, get_messages


async def _account():
    user = await PgUserRepo().create(id=uuid4().hex, username=uuid4().hex, password_hash="test")
    session = await create_session(user_id=user["id"], title="History", model="gpt-5.6-luna")
    return user["id"], session


async def _seed(session_id: str, user_id: str, roles: list[str], *, same_instant: bool = False) -> list[str]:
    """Messages with the given roles, oldest first, each with one text part."""
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    ids: list[str] = []
    async with get_db_session() as db:
        for index, role in enumerate(roles):
            message_id = ascending("message")
            created = start if same_instant else start + timedelta(seconds=index)
            ids.append(message_id)
            db.add(MessageORM(id=message_id, session_id=session_id, user_id=user_id, role=role,
                              finish=None if role == "user" else "stop", created_at=created))
        await db.flush()
        for index, message_id in enumerate(ids):
            part_id = ascending("part")
            db.add(PartORM(id=part_id, message_id=message_id, session_id=session_id, user_id=user_id,
                           type="text", created_at=start + timedelta(seconds=index),
                           data={"id": part_id, "type": "text", "text": f"#{index}",
                                 "session_id": session_id, "message_id": message_id}))
    return ids


def _turns(*steps: int) -> list[str]:
    """One user message then `n` assistant steps, per turn."""
    return [role for n in steps for role in ["user"] + ["assistant"] * n]


def _ids(window) -> list[str]:
    return [m.id for m in window.messages]


async def test_the_newest_window_holds_whole_turns():
    user_id, session = await _account()
    ids = await _seed(session.id, user_id, _turns(1, 3, 2, 5, 1))

    window = await get_message_window(session.id, user_id=user_id, turns=2)

    # The last two turns: 1 + 5 steps and 1 + 1, never a run cut in half.
    assert _ids(window) == ids[-8:]
    assert window.has_more
    assert all(len(m.parts) == 1 for m in window.messages)


async def test_paging_back_with_before_reaches_the_first_message():
    user_id, session = await _account()
    ids = await _seed(session.id, user_id, ["assistant"] + _turns(2, 1, 4, 3, 1, 2, 6))

    pages, before = [], None
    while True:
        window = await get_message_window(session.id, user_id=user_id, before=before, turns=2)
        pages.insert(0, _ids(window))
        if not window.has_more:
            break
        before = window.messages[0].id

    assert [i for page in pages for i in page] == ids
    # A message said before the first user message is on the oldest page.
    assert pages[0][0] == ids[0]


async def test_a_short_history_is_one_page():
    user_id, session = await _account()
    ids = await _seed(session.id, user_id, _turns(2, 1))

    window = await get_message_window(session.id, user_id=user_id, turns=20)

    assert _ids(window) == ids
    assert not window.has_more


async def test_after_returns_the_anchor_and_everything_newer():
    user_id, session = await _account()
    ids = await _seed(session.id, user_id, _turns(3, 4))

    window = await get_message_window(session.id, user_id=user_id, after=ids[5])

    assert _ids(window) == ids[5:]
    assert not window.has_more


async def test_pages_do_not_skip_or_repeat_messages_written_in_one_instant():
    user_id, session = await _account()
    ids = await _seed(session.id, user_id, _turns(1, 1, 1, 1), same_instant=True)

    pages, before = [], None
    while True:
        window = await get_message_window(session.id, user_id=user_id, before=before, turns=1)
        pages.insert(0, _ids(window))
        if not window.has_more:
            break
        before = window.messages[0].id

    assert [i for page in pages for i in page] == ids
    assert [m.id for m in await get_messages(session.id, user_id=user_id)] == ids


async def test_a_vanished_anchor_is_reported_not_guessed():
    user_id, session = await _account()
    await _seed(session.id, user_id, _turns(1))

    with pytest.raises(HistoryCursorGone):
        await get_message_window(session.id, user_id=user_id, before="message_gone")
    with pytest.raises(HistoryCursorGone):
        await get_message_window(session.id, user_id=user_id, after="message_gone")


async def test_another_user_sees_nothing():
    user_id, session = await _account()
    await _seed(session.id, user_id, _turns(1))

    window = await get_message_window(session.id, user_id="someone-else")

    assert window.messages == [] and not window.has_more


async def test_turns_are_clamped_to_at_least_one():
    user_id, session = await _account()
    ids = await _seed(session.id, user_id, _turns(1, 2))

    window = await get_message_window(session.id, user_id=user_id, turns=0)

    assert _ids(window) == ids[-3:]


async def test_the_route_returns_a_page_and_names_a_gone_cursor(monkeypatch):
    from api import sessions as route

    user_id, session = await _account()
    ids = await _seed(session.id, user_id, _turns(1, 2))
    monkeypatch.setattr(route.session_mod, "get_session_in_workspace",
                        AsyncMock(return_value=SimpleNamespace(user_id=user_id)))
    current_user = {"user_id": user_id, "workspace_id": "w1"}

    page = await route.get_history(session.id, turns=1, current_user=current_user)
    assert [m["id"] for m in page["messages"]] == ids[-3:]
    assert page["has_more"] is True

    with pytest.raises(HTTPException) as gone:
        await route.get_history(session.id, before="message_gone", current_user=current_user)
    assert gone.value.status_code == 409
    assert gone.value.detail["code"] == "HISTORY_CURSOR_GONE"

    with pytest.raises(HTTPException) as both:
        await route.get_history(session.id, before=ids[0], after=ids[1], current_user=current_user)
    assert both.value.status_code == 400
