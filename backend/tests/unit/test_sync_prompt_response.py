"""Synchronous prompts return the same complete Message seen on reconnect."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import sessions


@pytest.mark.asyncio
async def test_hydrates_terminal_run_pointer_from_latest_message(monkeypatch):
    pointer = SimpleNamespace(id="assistant-1")
    complete = SimpleNamespace(
        id="assistant-1",
        model="openai/gpt-5.6-luna",
        finish="stop",
        parts=[{"type": "text", "text": "done"}],
    )
    calls = []

    async def get_messages(session_id, **kwargs):
        calls.append((session_id, kwargs))
        return [complete]

    monkeypatch.setattr(sessions.session_mod, "get_messages", get_messages)

    hydrated = await sessions._hydrate_completed_message(
        "session-1", "user-1", pointer,
    )

    assert hydrated is complete
    assert calls == [("session-1", {
        "user_id": "user-1",
        "latest": True,
        "limit": 1,
    })]


@pytest.mark.asyncio
async def test_keeps_pointer_if_newer_unrelated_message_won_the_tail(monkeypatch):
    pointer = SimpleNamespace(id="assistant-1")
    newer = SimpleNamespace(id="assistant-2")

    async def get_messages(*_args, **_kwargs):
        return [newer]

    monkeypatch.setattr(sessions.session_mod, "get_messages", get_messages)

    assert await sessions._hydrate_completed_message(
        "session-1", "user-1", pointer,
    ) is pointer


@pytest.mark.asyncio
async def test_expired_recovery_skip_times_out_without_accepting_prompt(monkeypatch):
    from agent import driver, recovery

    record = driver.RecoveredDriver(
        session_id="session-1",
        user_id="user-1",
        run_id="expired-run",
        generation=4,
        phase="running",
        trigger_message_id="message-1",
    )
    repairs = 0

    async def refuse_overwrite(*_args, **_kwargs):
        raise driver.DriverRecoveryRequiredError(record)

    async def skipped_repair(*_args, **_kwargs):
        nonlocal repairs
        repairs += 1
        return SimpleNamespace(skipped=True)

    ticks = iter((0.0, 1.0, 21.0))
    monkeypatch.setattr(driver, "reserve_run", refuse_overwrite)
    monkeypatch.setattr(recovery, "repair_interrupted_session", skipped_repair)
    monkeypatch.setattr(sessions.time, "monotonic", lambda: next(ticks, 21.0))

    with pytest.raises(HTTPException) as exc:
        await sessions._reserve_prompt_run("session-1", "user-1")
    assert exc.value.status_code == 409
    assert repairs == 1
