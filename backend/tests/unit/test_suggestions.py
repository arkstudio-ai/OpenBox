"""No external models: real persistence/fencing with a controlled provider stream."""
import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from agent import suggestions
from agent.suggestion_context import CONTEXT_BYTES, build_context, load_context
from core.config import OpenBoxConfig, _load_json
from db.base import get_db_session
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from db.models.workspace import Workspace
from models.message import MessageWithParts, SuggestionsPart, TextPart
from question import runtime

PAYLOAD = {"items": [
    {"label": "精简开头", "prompt": "请保留原来的语气，把刚才的开头精简到三句话。", "mode": "send"},
    {"label": "准备发布", "prompt": "请帮我准备发布，我要发布的平台是：", "mode": "draft"},
], "context_summary": "用户正在制作中文文案，需要保留原来的语气。"}


@pytest.fixture
async def chat(monkeypatch):
    key = uuid4().hex
    uid, sid = f"u{key}", f"s{key}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=uid, username=uid, created_at=now, updated_at=now))
        await db.flush()
        db.add(Workspace(id=key, name="Suggestions", owner_user_id=uid, created_at=now, updated_at=now))
        await db.flush()
        db.add(Project(id=key, user_id=uid, workspace_id=key, name="Suggestions", created_at=now, updated_at=now))
        await db.flush()
        db.add(Session(id=sid, user_id=uid, project_id=key, workspace_id=key,
                       status="idle", model="openai/chat-picked", created_at=now, updated_at=now))
    events, calls = [], []
    monkeypatch.setattr(suggestions.bus, "publish", lambda kind, data: events.append((kind, data)))
    monkeypatch.setattr(suggestions, "get_config", lambda: OpenBoxConfig(
        model="openai/global-default", mcp_filter_model="openai/filter"))

    async def stream(**kwargs):
        calls.append(kwargs)
        yield {"type": "tool_call", "tool": suggestions.TOOL_NAME, "args": PAYLOAD}
        yield {"type": "finish", "reason": "tool_calls"}

    monkeypatch.setattr(suggestions, "stream_llm", stream)
    return uid, sid, runtime.RunTicket(sid, uid, 0, "finished-run"), events, calls


async def message(chat, role="user", text="帮我改写文案", finish=None, **data):
    uid, sid, *_ = chat
    mid = uuid4().hex
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(Message(id=mid, session_id=sid, user_id=uid, role=role, finish=finish, summary=False, created_at=now))
        await db.flush()
        db.add(Part(id=uuid4().hex, message_id=mid, session_id=sid, user_id=uid, type="text",
                    data={"type": "text", "id": uuid4().hex, "text": text, **data}, created_at=now))
    return mid


async def completed(chat):
    await message(chat)
    return await message(chat, "assistant", "这是改写后的文案。", "stop", channel="final")


async def saved(chat):
    async with get_db_session() as db:
        return (await db.scalars(select(Part).where(Part.session_id == chat[1], Part.type == "suggestions"))).all()


async def test_default_follows_chat_model_and_persists_part_and_event(chat):
    mid = await completed(chat)
    await suggestions.generate_suggestions(chat[2], mid, "openai/chat-picked")
    assert chat[4][0]["model_id"] == "openai/chat-picked"
    assert chat[4][0]["billing_kind"] == "suggestions"
    assert list(chat[4][0]["tools"]) == [suggestions.TOOL_NAME]
    assert chat[4][0]["ctx"].user_id == chat[0]
    assert chat[4][0]["ctx"].sandbox is None
    rows = await saved(chat)
    assert len(rows) == 1 and rows[0].message_id == mid and rows[0].user_id == chat[0]
    assert rows[0].data["items"] == PAYLOAD["items"]
    assert [kind for kind, _ in chat[3]] == ["part.created", "part.updated"]
    assert chat[3][0][1]["part"]["status"] == "pending"
    assert chat[3][-1][1]["messageId"] == mid and rows[0].data["status"] == "completed"
    # REST/reconnect can decode the persisted union, without regenerating it.
    from session.session import get_messages
    restored = await get_messages(chat[1], user_id=chat[0])
    assert isinstance(restored[-1].parts[-1], SuggestionsPart)
    await suggestions.generate_suggestions(chat[2], mid, "openai/chat-picked")
    assert len(chat[4]) == 1


@pytest.mark.parametrize("configured, expected", [("openai/cheap", "openai/cheap"), ("  ", "openai/chat-picked")])
async def test_independent_model_or_empty_fallback(chat, monkeypatch, configured, expected):
    monkeypatch.setattr(suggestions, "get_config", lambda: OpenBoxConfig(suggestion_model=configured))
    await suggestions.generate_suggestions(chat[2], await completed(chat), "openai/chat-picked")
    assert chat[4][0]["model_id"] == expected


def test_openbox_json_config_support(tmp_path):
    path = tmp_path / "openbox.json"
    path.write_text('{"suggestion_model": "openai/independent"}')
    assert OpenBoxConfig(**_load_json(path)).suggestion_model == "openai/independent"
    assert OpenBoxConfig().suggestion_model == ""


@pytest.mark.parametrize("status", ["busy", "retry", "error", "waiting_input", "queued", "compacting"])
async def test_never_generates_while_running_failed_or_waiting(chat, status):
    mid = await completed(chat)
    async with runtime.transaction(chat[1], chat[0]) as (_, session, _):
        session.status = status
    await suggestions.generate_suggestions(chat[2], mid, "openai/chat-picked")
    assert not chat[4] and not await saved(chat)


@pytest.mark.parametrize("finish", ["aborted", "waiting_input", "tool_calls", None])
async def test_non_final_reply_has_no_suggestions(chat, finish):
    await message(chat)
    mid = await message(chat, "assistant", "Incomplete", finish)
    await suggestions.generate_suggestions(chat[2], mid, "openai/chat-picked")
    assert not chat[4]


@pytest.mark.parametrize("change", ["new_message", "stop", "delete", "other_user", "cron", "child", "error"])
async def test_stale_or_ineligible_sources_are_rejected(chat, change):
    mid = await completed(chat)
    ticket = chat[2]
    if change == "new_message":
        await message(chat, text="新的要求")
    elif change == "other_user":
        ticket = runtime.RunTicket(chat[1], "someone-else", 0, "fake")
    else:
        async with runtime.transaction(chat[1], chat[0]) as (db, session, execution):
            if change == "stop":
                await runtime.invalidate_locked(db, execution)
            elif change == "delete":
                session.is_deleted = True
            elif change == "cron":
                session.kind = "cron"
            elif change == "child":
                session.parent_id = chat[1]
            else:
                (await db.get(Message, mid)).error = {"message": "Failed"}
    await suggestions.generate_suggestions(ticket, mid, "openai/chat-picked")
    assert not chat[4] and not await saved(chat)


async def test_new_run_during_generation_discards_result(chat, monkeypatch):
    mid = await completed(chat)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        yield {"type": "tool_call", "tool": suggestions.TOOL_NAME, "args": PAYLOAD}

    monkeypatch.setattr(suggestions, "stream_llm", delayed)
    task = asyncio.create_task(suggestions.generate_suggestions(chat[2], mid, "openai/chat-picked"))
    await asyncio.wait_for(entered.wait(), 1)
    async with runtime.transaction(chat[1], chat[0]) as (db, _, execution):
        await runtime.invalidate_locked(db, execution)
    release.set()
    await asyncio.wait_for(task, 1)
    rows = await saved(chat)
    assert len(rows) == 1 and rows[0].data["status"] == "unavailable" and rows[0].data["items"] == []
    assert chat[3][-1][1]["part"]["status"] == "unavailable"


async def test_concurrent_results_cannot_duplicate_cached_part(chat):
    mid = await completed(chat)
    await asyncio.gather(*(suggestions.generate_suggestions(chat[2], mid, "openai/chat-picked") for _ in range(2)))
    assert len(await saved(chat)) == 1
    assert len(chat[4]) == 1


async def test_generation_waits_for_run_lease_to_be_released(chat):
    mid = await completed(chat)
    ticket = await runtime.start_run(chat[1], chat[0])
    await suggestions.generate_suggestions(ticket, mid, "openai/chat-picked")
    assert not chat[4]
    await runtime.finish_run(ticket)
    await suggestions.generate_suggestions(ticket, mid, "openai/chat-picked")
    assert len(await saved(chat)) == 1


@pytest.mark.parametrize("model, route", [("openai/gpt-5.2", "responses"), ("anthropic/claude-sonnet-4", "direct")])
async def test_uses_existing_provider_adapter_and_billing_boundary(chat, monkeypatch, model, route):
    from agent import llm
    from billing.service import UsageMeter
    routed, metered = [], []
    async def responses(*args, **kwargs):
        routed.append("responses")
        yield {"type": "tool_call", "tool": suggestions.TOOL_NAME, "args": PAYLOAD}
    async def direct(*args, **kwargs):
        routed.append("direct")
        yield {"type": "tool_call", "tool": suggestions.TOOL_NAME, "args": PAYLOAD}
    async def start(**kwargs):
        metered.append(kwargs)
        return None
    monkeypatch.setattr(UsageMeter, "start", start)
    monkeypatch.setattr(llm, "_stream_responses_api", responses)
    monkeypatch.setattr(llm, "_stream_litellm_direct", direct)
    monkeypatch.setattr(suggestions, "stream_llm", llm.stream_llm)
    mid = await completed(chat)
    await suggestions.generate_suggestions(chat[2], mid, model)
    assert routed == [route]
    assert metered[0] == {"model_id": model, "session_id": chat[1], "user_id": chat[0], "message_id": mid, "kind": "suggestions"}
    assert len(await saved(chat)) == 1


@pytest.mark.parametrize("model, wire", [
    ("openai/qwen3.8-flash", {"reasoning_effort": "none"}),
    ("openai/qwen3.8-max", {"reasoning_effort": "none"}),
    ("deepseek/deepseek-v4-flash", {"thinking": {"type": "disabled"}}),
])
async def test_forced_suggestion_output_disables_supported_thinking(chat, monkeypatch, model, wire):
    import litellm
    from litellm.types.utils import ModelResponseStream
    from agent import llm
    from billing.service import UsageMeter

    requests = []

    async def stream():
        yield ModelResponseStream(choices=[{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0, "id": "suggestion-output", "type": "function",
                "function": {"name": suggestions.TOOL_NAME, "arguments": json.dumps(PAYLOAD)},
            }]},
            "finish_reason": "tool_calls",
        }])

    async def completion(**kwargs):
        requests.append(kwargs)
        # Model-side rejection seen in the local Qwen request logs. Exercise
        # the real adapter so a caller-only mock cannot hide this conflict.
        if kwargs.get("extra_body") != wire:
            raise ValueError("Forced tool output is unsupported in thinking mode")
        return stream()

    async def start(**kwargs):
        return None

    for setting in ("modify_params", "drop_params", "reasoning_auto_summary"):
        monkeypatch.setattr(litellm, setting, getattr(litellm, setting))
    monkeypatch.setattr(UsageMeter, "start", start)
    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _model: {})
    monkeypatch.setattr(suggestions, "stream_llm", llm.stream_llm)
    async with runtime.transaction(chat[1], chat[0]) as (_, session, _):
        session.variant = llm.reasoning_profile(model).default_variant

    await suggestions.generate_suggestions(chat[2], await completed(chat), model)

    rows = await saved(chat)
    assert len(requests) == 1 and requests[0]["tool_choice"] == "required"
    assert rows[0].data["status"] == "completed"
    assert rows[0].data["items"] == PAYLOAD["items"]
    assert chat[3][-1][1]["part"]["status"] == "completed"
    async with get_db_session() as db:
        assert (await db.get(Session, chat[1])).variant == llm.reasoning_profile(model).default_variant


async def test_empty_result_is_cached(chat, monkeypatch):
    async def stream(**kwargs):
        chat[4].append(kwargs)
        yield {"type": "tool_call", "tool": suggestions.TOOL_NAME,
               "args": {"items": [], "context_summary": "已完成，无需下一步。"}}
    monkeypatch.setattr(suggestions, "stream_llm", stream)
    mid = await completed(chat)
    await suggestions.generate_suggestions(chat[2], mid, "openai/chat-picked")
    await suggestions.generate_suggestions(chat[2], mid, "openai/chat-picked")
    assert (await saved(chat))[0].data["items"] == [] and len(chat[4]) == 1


@pytest.mark.parametrize("event", [
    {"type": "error", "error": "failure"},
    {"type": "tool_call", "tool": "bash", "args": {"command": "do not execute"}},
    {"type": "tool_call", "tool": suggestions.TOOL_NAME, "args": {"items": "invalid"}},
    {"type": "text_delta", "text": "not structured"},
])
async def test_invalid_output_closes_placeholder_without_chat_error(chat, monkeypatch, event):
    async def stream(**kwargs):
        yield event
    monkeypatch.setattr(suggestions, "stream_llm", stream)
    await suggestions.generate_suggestions(chat[2], await completed(chat), "openai/chat-picked")
    rows = await saved(chat)
    assert len(rows) == 1 and rows[0].data["status"] == "unavailable" and rows[0].data["items"] == []
    assert chat[3][-1][1]["part"]["status"] == "unavailable"
    async with get_db_session() as db:
        assert (await db.get(Session, chat[1])).status == "idle"


async def test_timeout_closes_stream_and_keeps_chat_idle(chat, monkeypatch):
    closed = asyncio.Event()
    async def stream(**kwargs):
        try:
            await asyncio.Event().wait()
            yield {}
        finally:
            closed.set()
    monkeypatch.setattr(suggestions, "stream_llm", stream)
    monkeypatch.setattr(suggestions, "TIMEOUT_SECONDS", 0.01)
    await suggestions.generate_suggestions(chat[2], await completed(chat), "openai/chat-picked")
    assert closed.is_set() and (await saved(chat))[0].data["status"] == "unavailable"


@pytest.mark.parametrize("cancel", [False, True])
async def test_pending_state_survives_rest_refresh_then_settles(chat, monkeypatch, cancel):
    from session.session import get_messages
    mid = await completed(chat)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        yield {"type": "tool_call", "tool": suggestions.TOOL_NAME, "args": PAYLOAD}

    monkeypatch.setattr(suggestions, "stream_llm", delayed)
    task = asyncio.create_task(suggestions.generate_suggestions(chat[2], mid, "openai/chat-picked"))
    await asyncio.wait_for(entered.wait(), 1)
    restored = await get_messages(chat[1], user_id=chat[0])
    part = restored[-1].parts[-1]
    assert isinstance(part, SuggestionsPart) and part.status == "pending" and not part.items
    remaining = (datetime.fromisoformat(part.expires_at) - datetime.now(timezone.utc)).total_seconds()
    assert 0 < remaining <= 60
    assert chat[3][-1][1]["part"]["id"] == part.id
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        release.set()
        await asyncio.wait_for(task, 1)
    final = (await saved(chat))[0]
    assert final.id == part.id
    assert final.data["status"] == ("unavailable" if cancel else "completed")
    assert final.data["expires_at"] is None
    assert final.data["items"] == ([] if cancel else PAYLOAD["items"])


@pytest.mark.parametrize("status", ["pending", "unavailable"])
async def test_unfinished_suggestions_do_not_erase_previous_summary(chat, status):
    first = await completed(chat)
    await suggestions.generate_suggestions(chat[2], first, "openai/chat-picked")
    later = await completed(chat)
    async with get_db_session() as db:
        part = SuggestionsPart(status=status, session_id=chat[1], message_id=later)
        db.add(Part(id=part.id, message_id=later, session_id=chat[1], user_id=chat[0],
                    type="suggestions", data=part.model_dump(), created_at=datetime.now(timezone.utc)))
    final = await completed(chat)
    context = json.loads(await load_context(chat[1], chat[0], final))
    assert context["previous_summary"] == PAYLOAD["context_summary"]


def test_validation_deduplicates_and_rejects_blank_or_oversized_items():
    result = suggestions.SuggestionResult.model_validate({**PAYLOAD, "items": [PAYLOAD["items"][0]] * 3})
    assert len(result.items) == 1
    for items in ([{**PAYLOAD["items"][0], "label": "   "}], PAYLOAD["items"] * 2):
        with pytest.raises(ValidationError):
            suggestions.SuggestionResult.model_validate({**PAYLOAD, "items": items})


def test_default_three_rounds_and_referential_expansion_to_five():
    rounds = [{"user": f"request {n}", "assistant": f"answer {n}"} for n in range(8)]
    parsed = json.loads(build_context(rounds, "original goal", "persistent constraints"))
    assert [r["user"] for r in parsed["rounds"]] == ["request 5", "request 6", "request 7"]
    rounds[-1]["user"] = "对比之前的第二个方案"
    assert len(json.loads(build_context(rounds, "", ""))["rounds"]) == 5
    assert parsed["initial_goal"] == "original goal" and parsed["previous_summary"] == "persistent constraints"


@pytest.mark.parametrize("value", ["中文需求" * 10_000, "x" * 40_000, "\x01\"\\" * 10_000])
def test_large_context_is_bounded_valid_json_and_preserves_latest_pair(value):
    context = build_context([{"user": value, "assistant": value, "state": value}] * 5, value, value)
    assert len(context.encode()) <= CONTEXT_BYTES
    assert len(json.loads(context)["rounds"]) == 3


async def test_context_ignores_synthetic_reasoning_and_tool_logs_beyond_first_200_messages(chat):
    for n in range(5):
        await message(chat, text=f"真实请求 {n}")
        if n == 4:
            for _ in range(205):
                await message(chat, "assistant", "PRIVATE_PROGRESS", "tool_calls", channel="commentary")
            await message(chat, text="INTERNAL_REMINDER", synthetic=True)
        mid = await message(chat, "assistant", f"完成回复 {n}", "stop")
    context = await load_context(chat[1], chat[0], mid)
    parsed = json.loads(context)
    assert len(parsed["rounds"]) == 3
    assert "真实请求 4" in context and "完成回复 4" in context
    assert "PRIVATE_PROGRESS" not in context and "INTERNAL_REMINDER" not in context


async def test_context_keeps_tool_step_artifacts_and_only_latest_todo_state(chat):
    await message(chat)
    step = await message(chat, "assistant", "进度消息", "tool_calls", channel="commentary")
    async with get_db_session() as db:
        for data in [
            {"type": "file", "path": "/workspace/result.mp4"},
            {"type": "file", "path": "PRIVATE_SCREENSHOT.png", "transient": True},
            {"type": "todo", "items": [{"subject": "生成视频", "status": "in_progress"}]},
            {"type": "todo", "items": [{"subject": "生成视频", "status": "completed"}]},
            {"type": "tool", "tool": "bash", "output": "PRIVATE_TOOL_LOG", "status": "completed"},
            {"type": "reasoning", "text": "PRIVATE_REASONING"},
        ]:
            db.add(Part(id=uuid4().hex, message_id=step, session_id=chat[1], user_id=chat[0],
                        type=data["type"], data=data, created_at=datetime.now(timezone.utc)))
    final = await message(chat, "assistant", "视频已生成", "stop")
    context = await load_context(chat[1], chat[0], final)
    assert "/workspace/result.mp4" in context and "completed: 生成视频" in context
    assert "in_progress" not in context and "PRIVATE_" not in context


async def test_summary_carries_constraints_beyond_the_recent_round_window(chat):
    first = await completed(chat)
    await suggestions.generate_suggestions(chat[2], first, "openai/chat-picked")
    for n in range(6):
        await message(chat, text=f"后续修改 {n}")
        final = await message(chat, "assistant", f"后续结果 {n}", "stop")
    context = json.loads(await load_context(chat[1], chat[0], final))
    assert context["previous_summary"] == PAYLOAD["context_summary"]
    assert len(context["rounds"]) == 3


def test_suggestions_are_never_replayed_into_main_model():
    from agent.loop import _to_llm_messages
    msg = MessageWithParts(id="a1", session_id="s1", role="assistant", finish="stop", parts=[
        TextPart(text="Final answer"), SuggestionsPart(**PAYLOAD),
    ])
    replay = json.dumps(_to_llm_messages([msg]))
    assert "Final answer" in replay and "context_summary" not in replay and "精简开头" not in replay
