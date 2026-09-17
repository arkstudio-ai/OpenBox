"""Provider retries stay inside one durable Assistant step."""

import asyncio
from types import SimpleNamespace

import pytest

from agent import loop
from agent.loop import (
    _acknowledge_dispatched_todo_notices,
    _close_failed_provider_step,
    _insert_todo_notice_snapshot,
    _prepare_checkpointed_provider_attempt,
    _run_provider_attempts,
)
from agent.processor import StepOutcome, StepResult
from models.message import StepFinishPart
from session.agent_event_log import (
    AgentEventPrefixDriftError,
    model_prompt_shape_digest,
    model_tool_definition_digest,
)


@pytest.mark.asyncio
async def test_pre_stream_retries_reuse_one_logical_step_until_success():
    outcomes = [
        StepResult(outcome=StepOutcome.RETRY, retry_reason="busy", error="first"),
        StepResult(outcome=StepOutcome.RETRY, retry_reason="busy", error="second"),
        StepResult(outcome=StepOutcome.CONTINUE, finish_reason="stop", text="ok"),
    ]
    calls = 0
    checkpoints = []
    sleeps = []

    async def attempt():
        nonlocal calls
        calls += 1
        return outcomes.pop(0)

    async def checkpoint(number, maximum, delay, result):
        checkpoints.append((number, maximum, delay, result.error))

    async def sleep(delay):
        sleeps.append(delay)

    result, retries = await _run_provider_attempts(
        attempt,
        checkpoint,
        max_retries=5,
        sleep=sleep,
        delay_for=lambda number, _error: number / 10,
    )

    assert result.outcome is StepOutcome.CONTINUE
    assert result.text == "ok"
    assert retries == 2
    assert calls == 3
    assert checkpoints == [
        (1, 5, 0.1, "first"),
        (2, 5, 0.2, "second"),
    ]
    assert sleeps == [0.1, 0.2]


@pytest.mark.asyncio
async def test_exhaustion_returns_the_final_failure_for_terminal_closure():
    calls = 0
    checkpoints = []

    async def attempt():
        nonlocal calls
        calls += 1
        return StepResult(
            outcome=StepOutcome.RETRY,
            retry_reason="unavailable",
            error=f"failure-{calls}",
        )

    async def checkpoint(number, maximum, _delay, _result):
        checkpoints.append((number, maximum))

    async def no_sleep(_delay):
        return None

    result, retries = await _run_provider_attempts(
        attempt,
        checkpoint,
        max_retries=2,
        sleep=no_sleep,
        delay_for=lambda _number, _error: 0,
    )

    # Initial request + exactly two retries. The caller receives the final
    # failure and can close the already-created Assistant Message once.
    assert calls == 3
    assert retries == 2
    assert checkpoints == [(1, 2), (2, 2)]
    assert result.outcome is StepOutcome.RETRY
    assert result.error == "failure-3"


@pytest.mark.asyncio
async def test_retry_delay_receives_the_original_provider_exception():
    provider_error = RuntimeError("provider busy")
    outcomes = [
        StepResult(
            outcome=StepOutcome.RETRY,
            retry_reason="busy",
            retry_error=provider_error,
            error="provider busy",
        ),
        StepResult(outcome=StepOutcome.CONTINUE, finish_reason="stop"),
    ]
    received = []

    async def attempt():
        return outcomes.pop(0)

    async def checkpoint(*_args):
        return None

    async def no_sleep(_delay):
        return None

    def delay_for(_number, error):
        received.append(error)
        return 0

    result, retries = await _run_provider_attempts(
        attempt,
        checkpoint,
        max_retries=1,
        sleep=no_sleep,
        delay_for=delay_for,
    )

    assert result.outcome is StepOutcome.CONTINUE
    assert retries == 1
    assert received == [provider_error]


@pytest.mark.asyncio
async def test_retry_backoff_is_interrupted_by_abort():
    abort = asyncio.Event()
    calls = 0

    async def attempt():
        nonlocal calls
        calls += 1
        return StepResult(
            outcome=StepOutcome.RETRY,
            retry_reason="busy",
            error="busy",
        )

    async def checkpoint(*_args):
        abort.set()

    result, retries = await asyncio.wait_for(
        _run_provider_attempts(
            attempt,
            checkpoint,
            max_retries=5,
            delay_for=lambda *_args: 60,
            abort=abort,
        ),
        timeout=0.5,
    )

    assert calls == 1
    assert retries == 1
    assert result.outcome is StepOutcome.CONTINUE
    assert result.finish_reason == "aborted"


@pytest.mark.asyncio
async def test_every_provider_attempt_rechecks_generation_fence():
    outcomes = [
        StepResult(outcome=StepOutcome.RETRY, retry_reason="busy"),
        StepResult(outcome=StepOutcome.CONTINUE, finish_reason="stop"),
    ]
    fences = 0

    async def before_attempt():
        nonlocal fences
        fences += 1

    async def no_sleep(_delay):
        return None

    result, retries = await _run_provider_attempts(
        lambda: _pop(outcomes),
        lambda *_args: _noop(),
        max_retries=2,
        sleep=no_sleep,
        delay_for=lambda *_args: 0,
        before_attempt=before_attempt,
    )

    assert result.outcome is StepOutcome.CONTINUE
    assert retries == 1
    assert fences == 2


@pytest.mark.asyncio
async def test_event_and_image_drift_rebuilds_before_one_provider_dispatch():
    surfaces = [
        SimpleNamespace(event_sequence=3, event_digest="1" * 64),
        SimpleNamespace(event_sequence=4, event_digest="2" * 64),
    ]
    built_sequences = []
    checkpoints = []
    provider_calls = []

    async def load_surface():
        return surfaces.pop(0)

    async def build_messages(surface):
        built_sequences.append(surface.event_sequence)
        return [{
            "role": "user",
            "content": "look",
            # Simulates resolve_images observing a new asset after an Event
            # arrived while the first candidate was being built.
            "_images": [f"data:image/png;base64,image-{surface.event_sequence}"],
        }]

    async def checkpoint(surface, tool_digest, prompt_digest):
        checkpoints.append((surface.event_sequence, tool_digest, prompt_digest))
        if surface.event_sequence == 3:
            raise AgentEventPrefixDriftError("test drift")

    prepared = await _prepare_checkpointed_provider_attempt(
        load_surface=load_surface,
        build_messages=build_messages,
        checkpoint=checkpoint,
        system=["system"],
        tools={},
        model_id="openai/test",
        provider_binding_digest="a" * 64,
        payload_dialect="litellm",
        tool_choice=None,
        user_variant=None,
    )

    # A stale candidate is never observable by the provider. The only object
    # dispatched is the rebuilt image-bearing payload proven by the second CAS.
    assert provider_calls == []
    provider_calls.append(prepared.llm_messages)
    assert built_sequences == [3, 4]
    assert [item[0] for item in checkpoints] == [3, 4]
    assert len(provider_calls) == 1
    assert provider_calls[0][0]["_images"] == [
        "data:image/png;base64,image-4"
    ]
    expected_tool_digest = model_tool_definition_digest([])
    assert prepared.tool_schema_digest == expected_tool_digest
    assert prepared.prompt_shape_digest == model_prompt_shape_digest(
        system=prepared.system,
        messages=prepared.llm_messages,
        model_id=prepared.model_id,
        provider_binding_digest="a" * 64,
        tool_schema_digest=expected_tool_digest,
        tool_choice=prepared.tool_choice,
        variant=prepared.user_variant,
    )
    assert checkpoints[-1][1:] == (
        prepared.tool_schema_digest,
        prepared.prompt_shape_digest,
    )


@pytest.mark.asyncio
async def test_todo_notice_is_preserved_for_retry_and_acknowledged_once():
    notices = ("- added: 修复重试 🚀",)
    outcomes = [
        StepResult(outcome=StepOutcome.RETRY, retry_reason="pre-stream"),
        StepResult(outcome=StepOutcome.CONTINUE, finish_reason="stop"),
    ]
    payloads = []
    acknowledged = []
    abort = asyncio.Event()

    async def acknowledge(session_id, snapshot):
        acknowledged.append((session_id, snapshot))
        return True

    async def attempt():
        payload = _insert_todo_notice_snapshot(
            [{"role": "user", "content": "continue"}],
            notices,
        )
        payloads.append(payload)
        result = outcomes.pop(0)
        await _acknowledge_dispatched_todo_notices(
            session_id="session-1",
            notices=notices,
            result=result,
            abort=abort,
            acknowledge=acknowledge,
        )
        return result

    result, retries = await _run_provider_attempts(
        attempt,
        lambda *_args: _noop(),
        max_retries=1,
        sleep=lambda _delay: _noop(),
        delay_for=lambda *_args: 0,
        abort=abort,
    )

    assert result.outcome is StepOutcome.CONTINUE
    assert retries == 1
    assert len(payloads) == 2
    assert all("修复重试" in item[0]["content"] for item in payloads)
    assert acknowledged == [("session-1", list(notices))]


async def _pop(items):
    return items.pop(0)


async def _noop():
    return None


@pytest.mark.asyncio
async def test_fatal_provider_step_gets_error_finish_and_step_finish(monkeypatch):
    info = SimpleNamespace(id="assistant-1", error=None, finish=None)
    updated = []
    saved = []

    async def update(message, **kwargs):
        updated.append((message, kwargs))

    async def save(part, **kwargs):
        saved.append((part, kwargs))

    monkeypatch.setattr(loop, "update_message_info", update)
    monkeypatch.setattr(loop, "save_part", save)

    error = await _close_failed_provider_step(
        info,
        session_id="session-1",
        user_id="user-1",
        run_fence=("session-1", "run-1", 3),
        step=2,
        start_snapshot="snapshot-1",
        duration=1.25,
        code="LLM_ERROR",
        message="stream failed",
    )

    assert error == {"code": "LLM_ERROR", "message": "stream failed"}
    assert info.error == error
    assert info.finish == "error"
    assert updated[0][1]["run_fence"] == ("session-1", "run-1", 3)
    assert isinstance(saved[0][0], StepFinishPart)
    assert saved[0][0].message_id == "assistant-1"
    assert saved[0][1]["run_fence"] == ("session-1", "run-1", 3)
