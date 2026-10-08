"""The decision model behind a call (voice/router.py) and the recall it checks replies against (voice/recall.py).

On only where the assistant's own memory routing uses JEV for the user; any
failure is "no verdict", never a broken call.
"""
import json

import httpx
import pytest

from core.config import get_config
from voice import recall, router


def jev(answers, model="jev-1.13.0", status=200):
    def handler(request):
        handler.sent.append(json.loads(request.content))
        return httpx.Response(status, json={"model": model, "answers": answers,
                                            "usage": {"input_tokens": 510, "output_tokens": 3}})
    handler.sent = []
    return handler


def choice(name, picked, confidence, options):
    return {name: {"type": "choice", "choice": picked, "confidence": confidence,
                   "probabilities": {option: (confidence if option == picked else 0.0) for option in options}}}


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("JEV_KEY", "test-only")
    monkeypatch.setattr(get_config(), "memory", get_config().memory.model_copy(update={"route_jev": True}))


def test_on_only_with_the_memory_routing_rollout_and_a_key(monkeypatch):
    monkeypatch.delenv("JEV_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    config = get_config()
    monkeypatch.setattr(config, "memory", config.memory.model_copy(update={"route_jev": True, "allowed_user_ids": []}))
    assert not router.enabled("u1")  # no key
    monkeypatch.setenv("JEV_KEY", "test-only")
    assert router.enabled("u1")
    monkeypatch.setattr(config, "memory", config.memory.model_copy(update={"allowed_user_ids": ["someone-else"]}))
    assert not router.enabled("u1")  # the rollout covers other users only
    monkeypatch.setattr(config, "memory", config.memory.model_copy(update={"allowed_user_ids": [], "route_jev": False}))
    assert not router.enabled("u1")
    monkeypatch.setattr(config, "memory", config.memory.model_copy(update={"route_jev": True}))
    monkeypatch.setattr(config, "voice", config.voice.model_copy(update={"router": False}))
    assert not router.enabled("u1")


async def test_a_verdict_for_the_utterance_with_the_calls_last_lines(keyed):
    handler = jev(choice("route", "read", 0.91, router.ROUTES))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        verdict = await router.route("云山项目的负责人是谁？", [("user", "我周末去哪"), ("assistant", "去游泳。")] * 3,
                                     client=client)
    assert verdict == router.Route("read", 0.91)
    [sent] = handler.sent
    assert sent["model"] == get_config().memory.jev_model and set(sent["questions"]) == {"route"}
    assert sent["state"]["utterance"] == "云山项目的负责人是谁？"
    assert len(sent["state"]["recent_call"]) == router.RECENT_LINES  # the last four lines only
    assert sent["state"]["recent_call"][-1] == {"role": "front_desk", "text": "去游泳。"}


async def test_the_reply_check_sends_the_question_the_reply_and_the_records(keyed):
    handler = jev(choice("complement", "add", 0.92, ("add", "covered", "irrelevant")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        verdict = await router.complement("云杉项目负责人是谁", "没查到。", ["云杉项目负责人是小李"], client=client)
        assert await router.complement("x", "y", [], client=client) is None  # nothing recalled: nothing to check
    assert verdict == router.Route("add", 0.92)
    [sent] = handler.sent
    assert sent["state"] == {"question": "云杉项目负责人是谁", "front_desk_reply": "没查到。",
                             "records": ["云杉项目负责人是小李"]}


@pytest.mark.parametrize("handler", [
    jev(choice("route", "maybe", 0.9, ("maybe",))),                  # not one of the choices
    jev(choice("route", "read", 1.7, router.ROUTES)),                # not a probability
    jev(choice("route", "read", 0.9, router.ROUTES), model="gpt"),   # not the decision model
    jev(choice("route", "read", 0.9, router.ROUTES), status=429),    # rate limited
    jev({}),                                                         # no answer
])
async def test_anything_unexpected_is_no_verdict(keyed, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await router.route("帮我建个项目", [], client=client) is None


async def test_a_slow_or_unreachable_decision_model_is_no_verdict(keyed):
    def unreachable(request):
        raise httpx.ConnectTimeout("slow")
    async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as client:
        assert await router.route("帮我建个项目", [], client=client) is None
    assert await router.route("   ", []) is None


async def test_the_core_memories_are_the_ones_every_assistant_turn_reads(monkeypatch):
    """Profile, preferences, constraints and notes alike: "云杉项目负责人是小李" was a note the front desk lacked."""
    assert await recall.core_memories("u1", "w1") == ""  # retrieval off: nothing, the call goes on
    monkeypatch.setattr(get_config(), "memory", get_config().memory.model_copy(update={"retrieval_v2": True}))
    scopes = []

    async def resolve(db, **kwargs):
        scopes.append(kwargs)
        return "scope"

    async def background(scope, config):
        return {"items": [{"text": "云杉项目负责人是小李"}, {"text": "用户对海鲜过敏。"}, {"text": "# 笔记\n松鼠青柠的演示草稿"},
                          {"text": " "}]}
    monkeypatch.setattr("memory.policy.resolve_access_scope", resolve)
    monkeypatch.setattr("memory.orchestrator._stable_background", background)
    assert await recall.core_memories("u1", "w1") == "云杉项目负责人是小李；用户对海鲜过敏；笔记。松鼠青柠的演示草稿"
    assert scopes == [{"user_id": "u1", "workspace_id": "w1", "include_all_projects": True}]

    async def many(scope, config):
        return {"items": [{"text": "很长的记忆" * 40}] * 10}
    monkeypatch.setattr("memory.orchestrator._stable_background", many)
    assert len(await recall.core_memories("u1", "w1")) == recall.CORE_CHARS


async def test_the_follow_through_check_sends_the_utterance_and_the_reply(keyed):
    handler = jev(choice("followthrough", "undone", 0.92, ("undone", "handled", "no_request")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        verdict = await router.followthrough("明天早上九点提醒我给小李打电话", "好的，记住了。", client=client)
        after = await router.followthrough("算了，不要了。", "好的。", "这回建好了，每五分钟回你一句hello。", client=client)
    assert verdict == router.Route("undone", 0.92) and after == verdict
    first, second = handler.sent
    assert set(first["questions"]) == {"followthrough"}
    assert first["state"] == {"utterance": "明天早上九点提醒我给小李打电话", "front_desk_reply": "好的，记住了。"}
    assert second["state"] == {"just_told_result": "这回建好了，每五分钟回你一句hello。", "utterance": "算了，不要了。",
                               "front_desk_reply": "好的。"}


async def test_the_handover_plan_asks_bailian_with_the_voice_key_and_thinking_off(monkeypatch):
    from voice import handover
    config = get_config()
    monkeypatch.setattr(config, "voice", config.voice.model_copy(update={"api_key": "voice-key-test"}))
    sent = []

    def handler(request):
        sent.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"brief": "帮我查一下云杉项目的负责人是谁。"}'}}]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("memory.providers.common.shared_client", lambda timeout: client)
    assert await handover.complete("SYSTEM", "TEXT", 4.0) == '{"brief": "帮我查一下云杉项目的负责人是谁。"}'
    [(url, auth, body)] = sent
    assert url == config.voice.handover_url and auth == "Bearer voice-key-test"
    assert body["model"] == config.voice.handover_model == "qwen3.8-flash" and body["enable_thinking"] is False
    assert body["messages"] == [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "TEXT"}]
    monkeypatch.setattr(config, "voice", config.voice.model_copy(update={"api_key": ""}))
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        await handover.complete("SYSTEM", "TEXT", 4.0)
    await client.aclose()
