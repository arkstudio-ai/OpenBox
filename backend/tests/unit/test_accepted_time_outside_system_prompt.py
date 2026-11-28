"""Accepted time moves from the system prompt into each user request.

With the ``accepted_time_outside_system_prompt`` switch on, the system prompt
keeps only the date and time zone, so it is byte-identical across turns, and
each user request shows its own local accepted time, rendered when the
request is built from the Inbox item's ``accepted_at``. The rendering is not
stored. With the switch off, nothing changes.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from agent import loop as loop_module
from agent.agent import AgentDef
from core.device_locale import DeviceLocale
from models.message import MessageWithParts, TextPart
from session.session import get_messages
from tests.unit.test_durable_questions import state  # noqa: F401
from tests.unit.test_full_history import _seed
from tests.unit.test_long_history_context import long_chat  # noqa: F401
from tests.unit.test_run_fencing_api import loop_harness  # noqa: F401

REAL_SYSTEM_PROMPT = loop_module._build_system_prompt
MARKER = "Local time when this user request was accepted"
ZONE = "Asia/Shanghai"


def _switch(monkeypatch, on: bool):
    from core.config import get_config
    monkeypatch.setattr(get_config().agent_tuning, "accepted_time_outside_system_prompt", on)


# ── the system prompt ──

@pytest.mark.parametrize("on", [True, False])
async def test_system_prompt_drops_the_accepted_time_only_when_switched_on(monkeypatch, on):
    from core.config import get_config
    config = get_config().model_copy(deep=True)
    config.agent_tuning.accepted_time_outside_system_prompt = on
    monkeypatch.setattr("core.config.get_config", lambda: config)
    agent = AgentDef(name="build", description="Test")
    locale = DeviceLocale(timezone=ZONE)
    first = await REAL_SYSTEM_PROMPT(agent, "test/model", device_locale=locale,
                                     turn_started_at="2026-11-28T01:00:00+00:00")
    second = await REAL_SYSTEM_PROMPT(agent, "test/model", device_locale=locale,
                                      turn_started_at="2026-11-28T03:30:00+00:00")
    later_step = await REAL_SYSTEM_PROMPT(agent, "test/model", device_locale=locale)
    text = "\n".join(second)
    assert f"User's time zone: {ZONE}." in text
    if on:
        assert first == second == later_step
        assert "Local time" not in text
    else:
        assert first != second
        assert f"{MARKER}: 2026-11-28T11:30+08:00" in text


# ── rendering one request ──

def _user(message_id, *, origin="human", created_at="2026-11-28T03:00:00+00:00", reference=None):
    return MessageWithParts(id=message_id, session_id="s", role="user", created_at=created_at,
                            parts=[TextPart(text="hi", origin=origin, origin_ref=reference or {})])


async def test_prefixes_use_inbox_acceptance_frozen_zone_and_skip_non_requests():
    cache = {"inbox_q": datetime(2026, 11, 28, 2, 50, tzinfo=timezone.utc)}
    messages = [
        # Queued: accepted 02:50, materialized (created_at) at 03:00.
        _user("m1", reference={"inbox_id": "inbox_q"}),
        # Not through the Inbox: created_at is the stored time.
        _user("m2", created_at="2026-11-28T04:00:00+00:00"),
        # The time zone frozen on the input wins over the turn's.
        _user("m3", reference={"device_locale": {"timezone": "Europe/London"}}),
        # A task result is not a user request.
        _user("m4", origin="task_result"),
    ]
    prefixes = await loop_module._accepted_time_prefixes(
        messages, user_id="u1", timezone_name=ZONE, accepted_at=cache)
    assert prefixes == {
        "m1": f"[{MARKER}: 2026-11-28T10:50+08:00]",
        "m2": f"[{MARKER}: 2026-11-28T12:00+08:00]",
        "m3": f"[{MARKER}: 2026-11-28T03:00+00:00]",
    }
    # No time zone known: nothing is rendered, as the system prompt did.
    assert await loop_module._accepted_time_prefixes(
        [_user("m2")], user_id="u1", timezone_name=None, accepted_at={}) == {}


async def test_prefixes_read_accepted_at_from_the_inbox_not_created_at():
    from zoneinfo import ZoneInfo
    from agent import inbox
    from tests.unit.test_agent_inbox import _seed as seed_inbox

    user_id, _, session_id, _ = await seed_inbox()
    receipt = await inbox.accept_inbox_item(
        session_id=session_id, user_id=user_id, delivery="followup", prompt="Queued request",
        client_id="queued-1", origin="human", origin_ref={"actor_user_id": user_id})
    from db.base import get_db_session
    from db.models.agent_inbox import AgentInboxItem
    async with get_db_session() as db:
        accepted = (await db.execute(select(AgentInboxItem.accepted_at).where(
            AgentInboxItem.id == receipt.id))).scalar_one()
    accepted = accepted if accepted.tzinfo else accepted.replace(tzinfo=timezone.utc)
    # Materialized long after acceptance, as when it waited in the queue.
    message = _user("m1", created_at="2030-01-01T00:00:00+00:00", reference={"inbox_id": receipt.id})
    cache: dict = {}
    prefixes = await loop_module._accepted_time_prefixes(
        [message], user_id=user_id, timezone_name=ZONE, accepted_at=cache)
    expected = accepted.astimezone(ZoneInfo(ZONE)).isoformat(timespec="minutes")
    assert prefixes == {"m1": f"[{MARKER}: {expected}]"}
    assert cache[receipt.id] is not None
    # Another user cannot read this item's time: it falls back to created_at.
    other = await loop_module._accepted_time_prefixes(
        [message], user_id="someone-else", timezone_name=ZONE, accepted_at={})
    assert other == {"m1": f"[{MARKER}: 2030-01-01T08:00+08:00]"}


def test_to_llm_messages_prefixes_only_named_user_messages():
    messages = [_user("m1"), _user("m2")]
    plain = loop_module._to_llm_messages(messages)
    prefixed = loop_module._to_llm_messages(messages, user_prefixes={"m1": "[when]"})
    assert plain[0]["content"] == "hi"
    assert prefixed[0]["content"] == "[when]\n\nhi"
    assert prefixed[1] == plain[1]


# ── whole turns ──

@pytest.fixture
def timed_chat(long_chat, monkeypatch):
    monkeypatch.setattr(long_chat.loop, "_build_system_prompt", REAL_SYSTEM_PROMPT)
    monkeypatch.setattr(long_chat.loop, "device_locale_for_turn",
                        AsyncMock(return_value=DeviceLocale("en-US", ZONE)))
    requests = []

    async def provider(**kwargs):
        requests.append(kwargs)
        yield {"type": "text_delta", "text": "Done."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 100, "output": 2}}

    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    long_chat.requests = requests
    return long_chat


async def _send(harness, text):
    """Through the durable Inbox, so the request has an accepted_at."""
    import asyncio
    from api import sessions as api
    from tests.unit.test_long_history_context import USER, _drain_tasks
    await asyncio.wait_for(api.send_message(
        "s1", api.PromptBody(text=text, model="openai/gpt-4o", delivery="followup"), current_user=USER,
    ), timeout=20)
    await _drain_tasks(harness)


def _user_content(request, text):
    [content] = [m["content"] for m in request["messages"] if m["role"] == "user" and text in m["content"]]
    return content


async def _accepted(text):
    from db.base import get_db_session
    from db.models.agent_inbox import AgentInboxItem
    async with get_db_session() as db:
        accepted = (await db.execute(select(AgentInboxItem.accepted_at).where(
            AgentInboxItem.session_id == "s1", AgentInboxItem.prompt == text))).scalar_one()
    if accepted.tzinfo is None:
        accepted = accepted.replace(tzinfo=timezone.utc)
    from zoneinfo import ZoneInfo
    return accepted.astimezone(ZoneInfo(ZONE)).isoformat(timespec="minutes")


async def test_switch_on_stable_system_prompt_and_per_request_times(state, timed_chat, monkeypatch):
    _switch(monkeypatch, True)
    await _seed("s1", "u1", turns=1)
    await _send(timed_chat, "First request")
    await _send(timed_chat, "Second request")
    first, second = timed_chat.requests

    # The system prompt is byte-identical across the two turns.
    assert first["system"] == second["system"]
    system = "\n".join(second["system"])
    assert f"User's time zone: {ZONE}." in system and "Local time" not in system

    # Each request shows its own accepted time (the Inbox item's accepted_at).
    first_time, second_time = await _accepted("First request"), await _accepted("Second request")
    assert _user_content(first, "First request") == f"[{MARKER}: {first_time}]\n\nFirst request"
    assert _user_content(second, "Second request") == f"[{MARKER}: {second_time}]\n\nSecond request"
    # An earlier request renders the same bytes on the next turn.
    assert _user_content(second, "First request") == _user_content(first, "First request")
    # Seeded history without a request origin is left as it was.
    assert _user_content(second, "问题 0") == "问题 0"

    # Nothing of the rendering is stored.
    stored = [part.text for message in await get_messages("s1", user_id="u1")
              for part in message.parts if getattr(part, "type", "") == "text"]
    assert "First request" in stored and "Second request" in stored
    assert not any(MARKER in text for text in stored)


async def test_switch_off_leaves_the_request_unchanged(state, timed_chat, monkeypatch):
    _switch(monkeypatch, False)
    await _seed("s1", "u1", turns=1)
    await _send(timed_chat, "First request")
    await _send(timed_chat, "Second request")
    first, second = timed_chat.requests

    # The time stays in the system prompt (this harness is not portable mode,
    # so it is the current local time), and user messages carry none.
    assert f"User's time zone: {ZONE}. Local time: " in "\n".join(second["system"])
    assert _user_content(first, "First request") == "First request"
    assert _user_content(second, "Second request") == "Second request"
