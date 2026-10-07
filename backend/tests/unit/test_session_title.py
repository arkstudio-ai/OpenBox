"""Titles use the chat selection and its provider, independent of helper models."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent import llm, loop
from core.config import OpenBoxConfig
from db.models.session import Session
from session.session import create_user_message, update_session
from tests.unit.test_durable_questions import read, state  # noqa: F401
from tests.unit.test_run_fencing_api import loop_harness  # noqa: F401


@pytest.fixture
def completion(monkeypatch):
    config = OpenBoxConfig.model_validate({
        "model": "default/model",
        "mcp_filter_model": "helper/small",
        "models": [{"id": name} for name in ("chat/selected", "chat/later", "default/model")],
        "provider": {
            name: {"api_key": f"{name}-test-key", "base_url": f"https://{name}.invalid/v1"}
            for name in ("chat", "default", "helper")
        },
    })
    monkeypatch.setattr("core.config.get_config", lambda: config)
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content='<think>internal reasoning</think>"新店活动规划"',
    ))])
    mock = AsyncMock(return_value=response)
    monkeypatch.setattr(llm, "metered_completion", mock)
    return mock


@pytest.mark.parametrize(("message_model", "session_model", "expected"), [
    ("chat/selected", "chat/later", "chat/selected"),
    (None, "chat/selected", "chat/selected"),
    (None, None, "default/model"),
    ("retired/model", "chat/selected", "default/model"),
    (None, "retired/model", "default/model"),
])
async def test_title_follows_turn_selection_and_matching_provider(
    state, completion, message_model, session_model, expected,
):
    message = await create_user_message("s1", "帮我规划新店活动", model=message_model, user_id="u1")
    # A later session preference must not change the model captured at send time.
    await update_session("s1", user_id="u1", model=session_model)

    await loop._ensure_title("s1", message, user_id="u1")

    completion.assert_awaited_once()
    request = completion.call_args.kwargs
    assert request["model"] == expected
    provider = expected.split("/", 1)[0]
    assert request["api_key"] == f"{provider}-test-key"
    assert request["api_base"] == f"https://{provider}.invalid/v1"
    assert request["billing_kind"] == "title"
    assert (request["ctx"].session_id, request["ctx"].user_id) == ("s1", "u1")
    assert (await read(Session, "s1")).title == "新店活动规划"


async def test_title_failure_keeps_text_fallback_without_switching_models(state, completion):
    completion.side_effect = RuntimeError("provider unavailable")
    message = await create_user_message(
        "s1", "帮我规划新店活动\n预算一万元", model="chat/selected", user_id="u1",
    )

    await loop._ensure_title("s1", message, user_id="u1")

    completion.assert_awaited_once()
    assert completion.call_args.kwargs["model"] == "chat/selected"
    assert (await read(Session, "s1")).title == "帮我规划新店活动"


@pytest.mark.parametrize("outcome", ["success", "failure", "superseded"])
async def test_reply_streams_and_finishes_while_title_is_still_pending(
    state, loop_harness, monkeypatch, outcome,
):
    import litellm
    from agent import driver
    from bus.events import MESSAGE_TEXT_DELTA, SESSION_TITLE
    from session.session import get_messages

    title_started = asyncio.Event()
    release_title = asyncio.Event()
    title_tasks = []

    async def title_provider(**kwargs):
        assert kwargs["model"] == "openai/selected"
        title_tasks.append(asyncio.current_task())
        title_started.set()
        # No clock-based delay: the title cannot finish until this test has
        # observed the main reply finish and release the session driver.
        await release_title.wait()
        if outcome == "failure":
            raise RuntimeError("title provider failed")
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(
            message=SimpleNamespace(content="异步标题验证"),
        )])

    async def reply_provider(**kwargs):
        await asyncio.wait_for(title_started.wait(), 5)
        yield {"type": "text_delta", "text": "正文"}
        yield {"type": "text_delta", "text": "正常输出"}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(litellm, "acompletion", title_provider)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _model: {})
    monkeypatch.setattr(loop_harness.processor, "stream_llm", reply_provider)
    monkeypatch.setattr("agent.suggestions.generate_suggestions", AsyncMock(return_value=[]))
    await update_session("s1", user_id="u1", title="", model="openai/selected")
    await create_user_message("s1", "测试标题与正文并发", model="openai/selected", user_id="u1")

    main_task = asyncio.create_task(loop.run_loop("s1", user_id="u1"))
    try:
        answer = await asyncio.wait_for(main_task, 5)
        assert answer is not None
        assert title_started.is_set() and not title_tasks[0].done()
        assert any(event == MESSAGE_TEXT_DELTA for event, _ in state)
        assert not any(event == SESSION_TITLE for event, _ in state)
        saved = next(message for message in await get_messages("s1", user_id="u1")
                     if message.id == answer.id)
        assert saved.finish == "stop"
        assert any(getattr(part, "text", None) == "正文正常输出" for part in saved.parts)
        session = await read(Session, "s1")
        assert (session.status, session.title) == ("idle", "")
        assert (await driver.get_driver_state("s1")).phase == "idle"

        if outcome == "superseded":
            await create_user_message("s1", "新的对话内容", model="openai/selected", user_id="u1")
        release_title.set()
        await asyncio.wait_for(title_tasks[0], 5)

        expected = {"success": "异步标题验证", "failure": "测试标题与正文并发", "superseded": ""}
        assert (await read(Session, "s1")).title == expected[outcome]
        assert any(event == SESSION_TITLE for event, _ in state) == (outcome != "superseded")
    finally:
        release_title.set()
        if not main_task.done():
            main_task.cancel()
        await asyncio.gather(main_task, *title_tasks, return_exceptions=True)
