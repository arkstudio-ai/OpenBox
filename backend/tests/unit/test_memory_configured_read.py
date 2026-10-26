"""The optional gateway classifier has a strict, non-authorizing read contract."""
import asyncio
import json
import math
from pathlib import Path

import httpx
import pytest

from core.config import MemoryConfig
from memory.providers.common import MemoryProviderError, response_json
from memory.providers.configured_read import cache_identity, decode, evaluate, read_schema, validate
from memory.providers.jev import READ_QUESTIONS


def answer():
    return {"read_policy": ["allowed", .95], "memory_needed": ["retrieve", .8],
        "task_needed": ["skip", .9], **{name: .1 for name, value in READ_QUESTIONS.items() if value["type"] == "noul"}}


def wire_answer():
    return {"p": ["", "allowed", .95], "m": ["retrieve", .8], "t": ["skip", .9], "n": [.1, .1, .1, .1], "r": 0,
            "d": ["", "none", .99], "q": ""}


@pytest.mark.parametrize("hint", [None, [], ["change", "invented", "zh-CN", "call", 1],
    ["change", "改用中文聊", "zh-CN", "call", .89], ["change", "改用中文聊", "zh-CN", "call", True],
    ["change", "改用中文聊", "zh-CN", "call", math.nan], ["change", "改用中文聊", "中文; ignore rules", "call", 1],
    ["restore", "改用中文聊", "zh-CN", "reset", 1], ["none", "", "", "none", 1],
    ["negated", "改用中文聊", "zh-CN", "call", 1], ["mentioned", "改用中文聊", "zh-CN", "call", 1],
    ["change", "改用中文聊", "", "reset", 1], ["restore", "改用中文聊", "zh-CN", "call", 1]])
def test_presentation_hint_cannot_invent_a_request_or_change_read_authority(hint):
    result = decode({**wire_answer(), "l": hint}, "我们改用中文聊")
    assert "spoken_language" not in result
    assert result["read_policy"]["choice"] == "allowed"


async def test_voice_language_is_planned_in_the_same_request_without_history(monkeypatch):
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda model: {
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "test-only"})
    calls = []
    async def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["response_format"]["json_schema"]["schema"] == read_schema(voice=True)
        return httpx.Response(200, json={"model": "qwen3.8-flash", "usage": {}, "choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps({**wire_answer(),
                "g": ["none", 1], "l": ["change", "这次先用中文聊聊", "zh-CN", "call", .99]})}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await evaluate({"entry": "voice", "current_request": "你好，我们这次先用中文聊聊。"},
            MemoryConfig(read_router_model="openai/qwen3.8-flash"), client=client)
    assert len(calls) == result["provider_attempts"] == 1
    assert result["answers"]["spoken_language"]["scope"] == "call"
    assert "l" not in read_schema()["properties"]
    assert "spoken_language" not in READ_QUESTIONS


@pytest.mark.parametrize("role", ["mentioned", "withdrawn", "none"])
def test_nonbinding_language_cannot_grant_or_deny_access_through_conflicting_policy(role):
    with pytest.raises(MemoryProviderError, match="restriction_role_conflict"):
        decode({**wire_answer(), "g": [role, 1], "p": ["只查上传资料", "documents_only", 1]},
            "翻译：只查上传资料")
    assert decode({**wire_answer(), "g": [role, 1]})["read_policy"]["choice"] == "allowed"


@pytest.mark.parametrize("role", [["access", 1], ["none", .3], ["uncertain", 1]])
def test_unresolved_or_contradictory_permission_witness_is_not_fail_open(role):
    with pytest.raises(MemoryProviderError, match="restriction_role_conflict"):
        decode({**wire_answer(), "g": role})


@pytest.mark.parametrize("role", [["invented", 1], ["none", True], ["none", math.nan], [], {}])
def test_malformed_restriction_role_is_rejected(role):
    with pytest.raises(MemoryProviderError):
        decode({**wire_answer(), "g": role})


def test_strict_schema_fixes_tuple_types_without_authorizing_fabricated_evidence():
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator(read_schema())
    wire = {**wire_answer(), "g": ["none", 1]}
    validator.validate(wire)
    for invalid in ({**wire, "g": [1, "none"]}, {**wire, "n": [1, 0]},
                    {**wire, "p": ["", "invented_policy", 1]}, {**wire, "r": True}):
        assert list(validator.iter_errors(invalid))
    fabricated = {**wire, "g": ["access", 1], "p": ["invented restriction", "documents_only", 1]}
    validator.validate(fabricated)
    with pytest.raises(MemoryProviderError, match="restriction_evidence_invalid"):
        decode(fabricated, "Just explain this sentence")


async def test_verified_official_read_route_sends_strict_schema(monkeypatch):
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda model: {
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "test-only"})
    async def respond(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_schema", "json_schema": {
            "name": "memory_read_plan", "strict": True, "schema": read_schema()}}
        return httpx.Response(200, json={"model": "qwen3.8-flash", "usage": {}, "choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps({**wire_answer(), "g": ["none", 1]})}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await evaluate({"current_request": "今天怎么安排"},
            MemoryConfig(read_router_model="openai/qwen3.8-flash"), client=client)
    assert result["provider_attempts"] == 1 and result["protocol_errors"] == []


@pytest.mark.parametrize("query", [None, [], {"read_policy": "allowed"}, 1, "x" * 129])
def test_invalid_optional_search_hint_does_not_discard_valid_read_policy(query):
    result = decode({**wire_answer(), "q": query})
    assert result["read_policy"]["choice"] == "allowed"
    assert "search_query" not in result


def test_search_hint_is_separate_from_time_and_permissions():
    result = decode({**wire_answer(), "q": "用户的兴趣 爱好 在家喜欢的休闲活动",
                     "d": ["今天", "keep_older", .95]}, "今天想在家休息")
    assert result["search_query"] == "用户的兴趣 爱好 在家喜欢的休闲活动"
    assert result["time_intent"]["expression"] == "今天"
    assert "search_query" not in READ_QUESTIONS
    legacy = wire_answer()
    legacy.pop("q")
    assert "search_query" not in decode(legacy)


@pytest.mark.parametrize("decision", [["昨天", "filter_records", .99], ["今天", "keep_older", .95],
                                      ["", "none", 1], ["", "uncertain", .3]])
def test_temporal_intent_is_grounded_without_expanding_legacy_jev_contract(decision):
    result = decode({**wire_answer(), "d": decision}, "昨天提过的安排，今天还能用吗？")
    assert result["time_intent"] == {"expression": decision[0], "choice": {"filter_records": "source_time", "keep_older": "event_time"}.get(decision[1], decision[1]), "confidence": decision[2]}
    assert "time_intent" not in READ_QUESTIONS


@pytest.mark.parametrize("decision", [None, [], ["昨天", "filter_records", True], ["昨天", "filter_records", math.nan],
    ["昨天", "filter_records", 1.1], ["", "filter_records", 1], ["上周", "filter_records", 1],
    ["上周", "none", 1], ["昨天", "invented", 1], ["昨天", [], 1]])
def test_temporal_filter_cannot_be_invented_by_classifier(decision):
    with pytest.raises(MemoryProviderError):
        decode({**wire_answer(), "d": decision}, "昨天的安排")


@pytest.mark.parametrize("policy", ["allowed", "no_history", "current_only", "uncertain"])
def test_compact_transport_preserves_policies_confidence_and_need_order(policy):
    value = wire_answer()
    quote = "这次不参考历史记录" if policy in {"no_history", "current_only"} else ""
    value["p"] = [quote, policy, .91]
    value["n"] = [.1, .2, .3, .4]
    result = decode(value, "这次不参考历史记录，先看我刚发的东西。")
    assert result["read_policy"] == {"choice": policy, "confidence": .91, "restriction_quote": quote,
        "restriction_target": "information" if quote else "none"}
    assert [result[key]["noul"] for key in ("history_useful", "knowledge_useful", "assets_useful", "experience_useful")] == [.1, .2, .3, .4]


@pytest.mark.parametrize("patch", [{"n": [0, 0, 0]}, {"n": [0, 0, True, 0]}, {"p": ["allowed", math.nan]},
                                    {"t": ["execute", 1]}, {"unexpected": "allowed"}, {"r": True},
                                    {"r": math.nan}, {"r": 1.1}])
def test_incomplete_or_corrupt_compact_decision_never_grants_reads(patch):
    with pytest.raises(MemoryProviderError):
        decode({**wire_answer(), **patch})


@pytest.mark.parametrize("decision", [["", "no_history", .95], ["invented restriction", "current_only", 1],
                                      ["ignore old records", "allowed", 1]])
def test_restriction_requires_an_exact_current_input_quote(decision):
    with pytest.raises(MemoryProviderError, match="restriction_evidence_invalid"):
        decode({**wire_answer(), "p": decision}, "ignore old records")


def test_action_restriction_does_not_prohibit_reads_or_authorize_execution():
    decision = {**wire_answer(), "p": ["先不要执行", "action_only", .95]}
    result = decode(decision, "先不要执行，请看看准备得怎么样了。")
    assert result["read_policy"] == {"choice": "allowed", "confidence": .95,
        "restriction_quote": "先不要执行", "restriction_target": "action"}


def test_current_input_only_cannot_require_prior_context():
    wire = {**wire_answer(), "g": ["access", 1],
            "p": ["照这些条件", "current_only", .95], "r": .9}
    with pytest.raises(MemoryProviderError, match="restriction_context_conflict"):
        decode(wire, "照这些条件，还要买什么吗？")
    # The check must not turn a restriction into permission, nor reject a
    # consistent restriction just because the message contains a reference.
    wire.update(p=["只看本句，不参考前文", "current_only", 1], r=0)
    assert decode(wire, "只看本句，不参考前文")["read_policy"]["choice"] == "current_only"


@pytest.mark.parametrize("second_policy", ["allowed", "current_only", "uncertain"])
async def test_context_conflict_repair_is_bounded_and_keeps_original_request(monkeypatch, second_policy):
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda model: {
        "api_base": "https://test.invalid/v1", "api_key": "test-only"})
    query = "照这些条件，还要买什么吗？"
    seen = []
    invalid = {**wire_answer(), "g": ["access", 1],
               "p": ["照这些条件", "current_only", .95], "r": .9}
    async def respond(request):
        state = json.loads(json.loads(request.content)["messages"][1]["content"])
        seen.append(state)
        assert state["current_request"] == query
        if len(seen) == 1:
            wire = invalid
        else:
            assert "prohibited prior context" in state["protocol_feedback"]
            wire = {**wire_answer(), "g": ["none" if second_policy == "allowed" else "uncertain", 1],
                    "p": ["", second_policy, 1]} if second_policy != "current_only" else invalid
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(wire)}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        call = evaluate({"current_request": query}, MemoryConfig(read_router_model="openai/qwen3.8-flash"), client=client)
        if second_policy == "current_only":
            with pytest.raises(MemoryProviderError, match="restriction_context_conflict"):
                await call
        else:
            result = await call
            assert result["answers"]["read_policy"]["choice"] == second_policy
            assert result["protocol_errors"] == ["restriction_context_conflict"]
            assert result["provider_attempts"] == 2
    assert len(seen) == 2


@pytest.mark.parametrize("invalid", [True, math.nan, math.inf, -1, 1.1, "0.9", None])
def test_invalid_score_cannot_grant_read(invalid):
    value = answer(); value["read_policy"][1] = invalid
    with pytest.raises(MemoryProviderError):
        validate(value)


def test_truncated_schema_and_unknown_choice_are_rejected():
    value = answer(); value.pop("assets_useful")
    with pytest.raises(MemoryProviderError):
        validate(value)
    value = answer(); value["read_policy"][0] = "execute"
    with pytest.raises(MemoryProviderError):
        validate(value)


def test_legal_region_error_is_separate_from_model_latency():
    with pytest.raises(MemoryProviderError) as caught:
        response_json(httpx.Response(451, json={"title": "Region unavailable"}))
    assert caught.value.code == "region_unavailable"


async def test_explicit_gateway_identity_does_not_reuse_another_providers_secret(monkeypatch):
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda model: {"api_base": "https://old.invalid/v1", "api_key": "old-key"})
    monkeypatch.setenv("MEMORY_READ_ROUTER_API_KEY", "gateway-key")
    async def respond(request):
        assert str(request.url) == "https://gateway.invalid/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer gateway-key"
        assert callable(request.extensions["trace"])
        value = json.loads(request.content)
        assert value["reasoning_effort"] == "none"
        assert request.extensions["timeout"]["read"] == 6
        assert "private-history" not in request.content.decode()
        return httpx.Response(200, json={"model": "qwen-gateway-alias", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(wire_answer())}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await evaluate({"current_request": "Arrange my weekend"},
            MemoryConfig(read_router_model="openai/qwen3.8-flash", read_router_url="https://gateway.invalid/v1"), client=client)
    assert result["classifier"] == "configured" and result["answers"]["read_policy"]["choice"] == "allowed"
    assert result["model"] == "openai/qwen3.8-flash"
    assert result["provider_model"] == "qwen-gateway-alias"


async def test_qwen_official_binding_does_not_inherit_gateway_credentials(monkeypatch):
    from core.config import OpenBoxConfig, _load_json, _resolve_env_vars
    from agent.llm import _get_provider_kwargs

    monkeypatch.setenv("BAILIAN_KEY", "official-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "uee-test-key")
    data = _resolve_env_vars(_load_json(Path(__file__).parents[2] / "openbox.jsonc.example"))
    data["provider"]["openai"]["base_url"] = "https://uee.invalid/v1"
    config = OpenBoxConfig(**data)
    monkeypatch.setattr("core.config.get_config", lambda: config)
    # All Qwen callers (chat, memory extraction, voice summary and read router)
    # resolve this same declared binding, despite retaining the openai/ wire id.
    for model in ("openai/qwen3.8-max", "openai/qwen3.8-flash"):
        assert _get_provider_kwargs(model) == {
            "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key": "official-test-key"}

    seen = []
    def respond(request):
        body = json.loads(request.content)
        seen.append((str(request.url), request.headers["authorization"], body["model"]))
        assert ("reasoning_effort" in body) == body["model"].startswith("qwen")
        return httpx.Response(200, json={"model": body["model"], "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(wire_answer())}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for model in ("openai/qwen3.8-flash", "openai/qwen3.8-max"):
            await evaluate({"current_request": "按我的兴趣推荐一下"},
                MemoryConfig(read_router_model=model), client=client)
    assert seen == [
        ("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "Bearer official-test-key", "qwen3.8-flash"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "Bearer official-test-key", "qwen3.8-max"),
    ]
    config.provider["bailian"].api_key = None
    with pytest.raises(MemoryProviderError, match="read_router_not_configured"):
        await evaluate({}, MemoryConfig(read_router_model="openai/qwen3.8-flash"))
    assert len(seen) == 2  # Missing official credentials cannot fall back to UEE.


@pytest.mark.parametrize("model", ["jev-1.13.0", "jev-latest", "jev-preview", "jev-1.14.0"])
@pytest.mark.parametrize("prefix", ["", "openai/"])
async def test_native_jev_cannot_be_sent_as_chat_completions(monkeypatch, model, prefix):
    from memory.providers.configured_read import warm_connection

    def unexpected_provider(*args, **kwargs):
        pytest.fail("A native JEV model must not resolve Chat Completions credentials or transport")

    monkeypatch.setattr("agent.llm._get_provider_kwargs", unexpected_provider)
    monkeypatch.setattr("memory.providers.configured_read.shared_client", unexpected_provider)
    config = MemoryConfig(read_router_model=prefix + model)
    await warm_connection(config)
    with pytest.raises(MemoryProviderError, match="read_router_protocol_mismatch"):
        await evaluate({"current_request": "按我的兴趣推荐一下"}, config)


def test_gateway_endpoint_and_channel_credentials_partition_cache(monkeypatch):
    provider = {"api_base": "https://gateway-a.invalid/v1", "api_key": "channel-a"}
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda model: provider)
    config = MemoryConfig(read_router_model="openai/qwen3.8-flash")
    original = cache_identity(config)
    assert cache_identity(config) == original
    provider["api_base"] = "https://gateway-b.invalid/v1"
    moved = cache_identity(config)
    assert moved != original
    provider["api_key"] = "channel-b"
    assert cache_identity(config) not in {original, moved}
    assert "channel-b" not in cache_identity(config)


async def test_custom_endpoint_without_its_own_key_fails_closed(monkeypatch):
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda model: {"api_base": "https://old.invalid", "api_key": "old-key"})
    monkeypatch.delenv("MEMORY_READ_ROUTER_API_KEY", raising=False)
    with pytest.raises(MemoryProviderError, match="read_router_not_configured"):
        await evaluate({}, MemoryConfig(read_router_model="other/model", read_router_url="https://new.invalid"))


@pytest.mark.parametrize("result", [
    {"choices": [None]},
    {"choices": [{"finish_reason": "length", "message": {"content": json.dumps(answer())}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer())}}], "error": {"code": "gateway_failed"}},
])
async def test_incomplete_gateway_result_never_records_completed(monkeypatch, result):
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda model: {"api_base": "https://gateway.invalid", "api_key": "test-key"})
    outcomes=[]
    monkeypatch.setattr("agent.gateway.Observation.finish", lambda self, outcome, error: outcomes.append(outcome))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,json=result))) as client:
        with pytest.raises(MemoryProviderError, match="invalid_response"):
            await evaluate({}, MemoryConfig(read_router_model="openai/qwen3.8-flash"),client=client)
    assert outcomes == ["failed"]


async def test_protocol_repair_stays_in_original_budget_and_counts_both_attempts(monkeypatch):
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda _: {"api_base": "https://gateway.invalid", "api_key": "test-key"})
    sent = []
    async def respond(request):
        sent.append(json.loads(request.content))
        decision = wire_answer() if len(sent) == 2 else {**wire_answer(), "p": ["misquoted", "allowed", 1]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(decision)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await evaluate({"current_request": "Arrange my weekend"}, MemoryConfig(read_router_model="openai/qwen3.8-flash"), client=client)
    assert len(sent) == result["provider_attempts"] == 2
    assert result["usage"] == {"input_tokens": 200, "output_tokens": 80}
    assert "protocol_feedback" in json.loads(sent[1]["messages"][1]["content"])
    assert "misquoted" not in sent[1]["messages"][1]["content"]


@pytest.mark.parametrize("query,quote", [
    ("我们以后用英语聊吧，你说的短一些就好。", "以后"),
    ("From now on, please keep replies concise.", "From now on"),
    ("今天发活动介绍，按我的习惯来。", "今天"),
])
async def test_incidental_time_quote_keeps_policy_without_retry_or_record_filter(monkeypatch, query, quote):
    from memory.time_context import resolve_query_time
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda _: {
        "api_base": "https://gateway.invalid", "api_key": "test-key"})
    sent = []
    def respond(request):
        sent.append(request)
        decision = {**wire_answer(), "d": [quote, "none", .99]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(decision)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await evaluate({"current_request": query}, MemoryConfig(read_router_model="openai/qwen3.8-flash"), client=client)
    assert len(sent) == result["provider_attempts"] == 1
    assert result["usage"] == {"input_tokens": 100, "output_tokens": 40}
    assert result["answers"]["read_policy"]["choice"] == "allowed"
    assert result["answers"]["time_intent"]["choice"] == "none"
    assert not resolve_query_time(query, "Asia/Shanghai", intent=result["answers"]["time_intent"])["hard_filter_applied"]


async def test_transport_error_is_not_retried_as_a_protocol_repair(monkeypatch):
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda _: {"api_base": "https://gateway.invalid", "api_key": "test-key"})
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(503, json={"error": "unavailable"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MemoryProviderError, match="provider_error"):
            await evaluate({}, MemoryConfig(read_router_model="openai/qwen3.8-flash"), client=client)
    assert len(requests) == 1


async def test_protocol_repair_cannot_restart_the_total_deadline(monkeypatch):
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda _: {"api_base": "https://gateway.invalid", "api_key": "test-key"})
    deadlines = []
    timeout = asyncio.timeout

    def record_timeout(seconds):
        scope = timeout(seconds)
        deadlines.append(scope)
        return scope

    monkeypatch.setattr("memory.providers.configured_read.asyncio.timeout", record_timeout)
    calls = 0
    cancelled = asyncio.Event()

    async def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(.02)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]})
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MemoryProviderError, match="timeout"):
            await evaluate({"current_request": "Arrange my weekend"},
                MemoryConfig(read_router_model="openai/qwen3.8-flash", read_router_timeout_seconds=.1), client=client)
    assert calls == 2 and cancelled.is_set()
    assert len(deadlines) == 1 and deadlines[0].expired()


async def test_transport_warmup_is_input_free_throttled_and_shares_the_actual_read_pool(monkeypatch):
    from memory.providers import common, configured_read
    seen = []
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda _: {
        "api_base": "https://gateway.invalid/v1", "api_key": "test-key"})

    def respond(request):
        seen.append(request)
        if request.method == "HEAD":
            assert not request.content and "authorization" not in request.headers
            assert request.url.path == "/v1/models"
            return httpx.Response(404)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(wire_answer())}}]})

    original = httpx.AsyncClient
    clients = []
    def factory(**kwargs):
        client = original(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(common.httpx, "AsyncClient", factory)
    config = MemoryConfig(read_router_model="openai/qwen3.8-flash")
    try:
        await configured_read.warm_connection(config)
        await configured_read.warm_connection(config)
        result = await configured_read.evaluate({"current_request": "Arrange my weekend"}, config)
        assert [request.method for request in seen] == ["HEAD", "POST"]
        assert len(clients) == 1 and result["answers"]["read_policy"]["choice"] == "allowed"
        assert seen[-1].extensions["timeout"]["read"] == config.read_router_timeout_seconds
    finally:
        await common.close_shared_clients()


async def test_warmup_failure_does_not_retry_or_resolve_read_scope(monkeypatch):
    from memory.providers import common, configured_read
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda _: {
        "api_base": "https://gateway.invalid/v1", "api_key": "test-key"})
    methods = []
    def unavailable(request):
        methods.append(request.method)
        raise httpx.ConnectTimeout("connection unavailable")
    original = httpx.AsyncClient
    monkeypatch.setattr(common.httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(unavailable), **kw))
    config = MemoryConfig(read_router_model="openai/qwen3.8-flash")
    try:
        await configured_read.warm_connection(config)
        with pytest.raises(MemoryProviderError, match="timeout"):
            await configured_read.evaluate({"current_request": "Only use this message"}, config)
        assert methods == ["HEAD", "POST"]
    finally:
        await common.close_shared_clients()


async def test_startup_warmup_is_gated_nonblocking_and_cancellable(monkeypatch):
    from memory.providers import configured_read
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def pending(_config):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    monkeypatch.setattr(configured_read, "warm_connection", pending)
    assert configured_read.schedule_transport_warmup(MemoryConfig(read_router_model="openai/qwen3.8-flash")) is None
    config = MemoryConfig(semantic_recall=True, route_jev=True, read_router_model="openai/qwen3.8-flash")
    task = configured_read.schedule_transport_warmup(config)
    await entered.wait()
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set()


async def test_startup_transport_error_cannot_break_service_startup(monkeypatch):
    from memory.providers import configured_read
    async def unavailable(_config):
        raise ValueError("gateway configuration unavailable")
    monkeypatch.setattr(configured_read, "warm_connection", unavailable)
    task = configured_read.schedule_transport_warmup(MemoryConfig(
        semantic_recall=True, route_jev=True, read_router_model="openai/qwen3.8-flash"))
    assert await task is None
