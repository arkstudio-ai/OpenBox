"""A long conversation is loaded whole.

`get_messages` used to default to a 200-row page, oldest first, and only the
HTTP route ever asked for another page. Past message 200 the agent loop kept
sending the model the same frozen prefix: no new tool results, no answers to
its questions, no new user messages (session_7YBXNNJ7KGM2YPWK39MXAJZXCF,
2026-09-14). Prune, abort, fork and revert read the same stale slice.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from sqlalchemy import select, text

from core.identifier import ascending
from db.base import get_db_session
from db.models.message import Message as MessageORM
from db.models.part import Part as PartORM
from db.repository.user_repo import PgUserRepo
from session.session import create_session, create_user_message, get_messages


async def _account():
    user = await PgUserRepo().create(id=uuid4().hex, username=uuid4().hex, password_hash="test")
    session = await create_session(user_id=user["id"], title="Long chat", model="gpt-5.6-luna")
    return user["id"], session


async def _seed(session_id: str, user_id: str, turns: int, *, tool_output: str | None = None) -> list[str]:
    """`turns` finished exchanges, oldest first, a second apart, an hour ago.

    Assistants answer in text, or — with `tool_output` — each carries one
    completed read whose output is that string.
    """
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    ids: list[str] = []
    parts: list[PartORM] = []
    async with get_db_session() as db:
        for turn in range(turns):
            for role in ("user", "assistant"):
                message_id = ascending("message")
                created = start + timedelta(seconds=len(ids))
                ids.append(message_id)
                finish = None if role == "user" else "stop" if tool_output is None else "tool_calls"
                db.add(MessageORM(id=message_id, session_id=session_id, user_id=user_id, role=role,
                                  agent="build", model="gpt-5.6-luna", finish=finish, created_at=created))
                part_id = ascending("part")
                base = {"id": part_id, "session_id": session_id, "message_id": message_id}
                if role == "user" or tool_output is None:
                    data = {**base, "type": "text", "text": f"{'问题' if role == 'user' else '回答'} {turn}"}
                else:
                    data = {**base, "type": "tool", "tool": "read", "call_id": f"call_{turn}",
                            "status": "completed", "input": {"path": f"/workspace/{turn}.txt"},
                            "output": tool_output, "title": "", "metadata": {}, "state": {"time": {}}}
                parts.append(PartORM(id=part_id, message_id=message_id, session_id=session_id,
                                     user_id=user_id, type=data["type"], data=data, created_at=created))
        await db.flush()
        db.add_all(parts)
    return ids


async def test_every_message_is_loaded_in_creation_order():
    user_id, session = await _account()
    ids = await _seed(session.id, user_id, turns=130)

    messages = await get_messages(session.id, user_id=user_id)

    assert [m.id for m in messages] == ids
    assert all(len(m.parts) == 1 for m in messages)


async def test_pages_still_work_when_a_caller_asks_for_them():
    user_id, session = await _account()
    ids = await _seed(session.id, user_id, turns=130)

    first = await get_messages(session.id, limit=200, user_id=user_id)
    rest = await get_messages(session.id, offset=200, limit=200, user_id=user_id)

    assert [m.id for m in first + rest] == ids
    assert all(len(m.parts) == 1 for m in first + rest)


async def test_created_at_ties_are_ordered_by_id():
    # A fork writes a whole history in one instant. Pages must neither skip nor
    # repeat a row, and the prompt must come out the same on every step.
    user_id, session = await _account()
    instant = datetime.now(timezone.utc)
    ids = [ascending("message") for _ in range(5)]
    async with get_db_session() as db:
        for message_id in reversed(ids):
            db.add(MessageORM(id=message_id, session_id=session.id, user_id=user_id,
                              role="user", created_at=instant))

    paged = [m.id for offset in (0, 2, 4)
             for m in await get_messages(session.id, offset=offset, limit=2, user_id=user_id)]

    assert paged == ids
    assert [m.id for m in await get_messages(session.id, user_id=user_id)] == ids


async def test_the_http_route_keeps_its_200_row_page(monkeypatch):
    # The web and mobile clients page with offset/limit and stop on a short
    # page; the route's own default is what they rely on.
    from api import sessions as route

    seen = {}

    async def fake_get_messages(session_id, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(route.session_mod, "get_messages", fake_get_messages)
    monkeypatch.setattr(route.session_mod, "get_session_in_workspace",
                        AsyncMock(return_value=SimpleNamespace(user_id="u1")))

    await route.get_messages("s1", current_user={"user_id": "u1", "workspace_id": "w1"})

    assert (seen["offset"], seen["limit"]) == (0, 200)


async def test_prune_reaches_outputs_past_the_first_200_messages():
    from agent.compaction import prune_tool_outputs

    user_id, session = await _account()
    ids = await _seed(session.id, user_id, turns=130, tool_output="x" * 8_000)  # 2k tokens each

    await prune_tool_outputs(session.id, user_id=user_id)

    async with get_db_session() as db:
        rows = (await db.execute(select(PartORM.message_id, PartORM.data).where(
            PartORM.session_id == session.id, PartORM.type == "tool"))).all()
    cleared = {message_id for message_id, data in rows
               if ((data.get("state") or {}).get("time") or {}).get("compacted")}
    # Behind the two newest turns, the newest 40k tokens of output (turns
    # 108-127) are protected; turn 107's output, message 216, is the newest one
    # pruned. Reading the first 200 rows, prune never got that far.
    assert ids[2 * 107 + 1] in cleared
    assert not cleared & set(ids[2 * 108:])


async def test_the_loop_answers_a_message_past_the_first_page(monkeypatch):
    from agent import loop, processor
    from sandbox import sandbox_manager
    from sandbox.entitlement import SandboxSubscriptionRequired

    # kv_store is migration-owned, not part of ORM create_all in the fixture.
    async with get_db_session() as db:
        await db.execute(text("CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"))
    user_id, session = await _account()
    await _seed(session.id, user_id, turns=120)
    await create_user_message(session_id=session.id, text="字幕重新配一下", agent="build",
                              model=session.model, user_id=user_id)
    monkeypatch.setattr(sandbox_manager, "get_client", AsyncMock(side_effect=SandboxSubscriptionRequired()))
    monkeypatch.setattr(loop, "_ensure_title", AsyncMock())
    sent = []

    async def stream(**kwargs):
        sent.append(kwargs["messages"])
        yield {"type": "text_delta", "text": "好的，马上重新配字幕。"}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 1, "output": 1}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    await loop.run_loop(session.id, user_id=user_id)
    if loop._background_tasks:
        await asyncio.gather(*list(loop._background_tasks))

    # Reading the first 200 rows, the newest thing the loop saw was an answered
    # exchange, so it ended the run without calling the model at all.
    assert sent, "the model was never called"
    user_turns = [str(m.get("content")) for m in sent[0] if m.get("role") == "user"]
    assert "问题 0" in user_turns[0]
    assert "字幕重新配一下" in user_turns[-1]
