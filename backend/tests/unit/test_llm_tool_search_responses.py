from __future__ import annotations

import copy
import sys
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field

from core.config import OpenBoxConfig, ProviderConfig
from agent.native_tool_search import (
    NativeCapabilityCache,
    NativeCapabilityKey,
    NativeFeatureUnsupported,
    NativeHTTPError,
    NativeProtocolError,
    OpenAIResponsesNativeNormalizer,
    build_openai_native_replay_input,
    build_openai_responses_native_plan,
    decide_native_adapter,
    is_explicit_native_unsupported,
    stream_openai_responses_json,
    stream_with_native_fallback,
)
from agent import llm as LLM
from agent.tool_exposure import ExposurePlan, build_eligible_catalog
from session.internal_parts import ProviderCapabilityBinding
from tool.tool import ToolInfo, ToolResult


class _Args(BaseModel):
    marker: str = Field(description="unique parameter marker")


async def _execute(_args, _ctx):
    return ToolResult(output="ok")


def _tool(tool_id: str, *, same_response_safe: bool = False) -> ToolInfo:
    return ToolInfo(
        id=tool_id,
        description=f"unique description for {tool_id}",
        parameters=_Args,
        execute=_execute,
        source="builtin",
        plane="platform",
        canonical_id=tool_id,
        provider_name=tool_id,
        same_response_safe=same_response_safe,
    )


def _native_plan(*, deferred: tuple[str, ...] = ("read",)):
    tools = {
        "capability_search": _tool("capability_search"),
        "bash": _tool("bash"),
        **{tool_id: _tool(tool_id) for tool_id in deferred},
    }
    catalog = build_eligible_catalog(tools)
    plan = ExposurePlan(
        direct_ids=("bash", "capability_search"),
        deferred_ids=deferred,
        discovery_ids=deferred,
        reasons={"bash": "resident", "capability_search": "resident"},
        strategy="portable",
        schema_chars=0,
    )
    return catalog, build_openai_responses_native_plan(catalog, plan)


def _budget(
    *,
    max_search_calls: int = 2,
    max_reveals: int = 5,
    max_result_chars: int = 2_000,
    search_calls: int = 0,
    revealed_ids: set[str] | None = None,
    result_chars: int = 0,
):
    return SimpleNamespace(
        _capability_search_calls=search_calls,
        _capability_revealed_ids=set(revealed_ids or ()),
        _capability_result_chars=result_chars,
        _capability_max_search_calls=max_search_calls,
        _capability_max_reveals=max_reveals,
        _capability_max_result_chars=max_result_chars,
    )


def _normalizer(plan, **budget_kwargs):
    return OpenAIResponsesNativeNormalizer(
        plan,
        budget_state=_budget(**budget_kwargs),
    )


def _binding(**changes) -> ProviderCapabilityBinding:
    values = {
        "provider": "openai",
        "endpoint": "https://api.openai.com/path:abc",
        "account_id": "account-config:abc",
        "api_version": "2025-03-01-preview",
        "model": "openai/gpt-5.4",
        "dialect": "responses",
        "beta_headers": (),
    }
    values.update(changes)
    return ProviderCapabilityBinding(**values)


def _deferred_definition(plan, name: str) -> dict:
    return copy.deepcopy(next(tool for tool in plan.tools if tool.get("name") == name))


def test_native_responses_wire_replaces_portable_search_and_defers_only_frontier():
    source_catalog, plan = _native_plan(deferred=("read", "grep"))

    assert [tool["type"] for tool in plan.tools] == [
        "function",
        "tool_search",
        "function",
        "function",
    ]
    assert plan.tools[0]["name"] == "bash"
    assert plan.tools[1]["execution"] == "server"
    assert not any(tool.get("name") == "capability_search" for tool in plan.tools)
    assert all(
        tool.get("defer_loading") is True
        for tool in plan.tools
        if tool.get("name") in {"read", "grep"}
    )
    assert "defer_loading" not in plan.tools[0]
    assert plan.catalogue_wire_chars > plan.initial_visible_chars
    assert plan.schema_digest_by_wire["read"] == (
        source_catalog.entries["read"].schema_digest
    )
    # Serialization must never annotate/mutate source ToolInfo schemas.
    assert all(tool.raw_schema is None for tool in source_catalog.tools.values())


def test_native_wire_is_deterministic_and_partition_must_be_complete():
    catalog, first = _native_plan(deferred=("read", "grep"))
    second = build_openai_responses_native_plan(
        catalog,
        ExposurePlan(
            direct_ids=("bash", "capability_search"),
            deferred_ids=("read", "grep"),
            discovery_ids=("read", "grep"),
            reasons={"bash": "resident", "capability_search": "resident"},
            strategy="portable",
            schema_chars=0,
        ),
    )
    assert first == second

    with pytest.raises(ValueError, match="partition"):
        build_openai_responses_native_plan(
            catalog,
            ExposurePlan(
                direct_ids=("bash", "capability_search"),
                deferred_ids=("read",),
                discovery_ids=("read",),
                reasons={"bash": "resident", "capability_search": "resident"},
                strategy="portable",
                schema_chars=0,
            ),
        )


def test_normalizer_preserves_search_reveal_call_order_and_safe_execution():
    _catalog, plan = _native_plan(deferred=("read",))
    normalizer = _normalizer(plan)
    data = {
        "type": "response.completed",
        "response": {
            "output": [
                {
                    "id": "tsc_1",
                    "type": "tool_search_call",
                    "execution": "server",
                    "status": "completed",
                    "arguments": {"query": "read a file"},
                },
                {
                    "id": "tso_1",
                    "type": "tool_search_output",
                    "execution": "server",
                    "status": "completed",
                    "tools": [_deferred_definition(plan, "read")],
                },
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read",
                    "arguments": '{"marker":"x"}',
                },
            ]
        },
    }
    events = normalizer.feed_sse(data)

    assert [event.type for event in events] == [
        "search_started",
        "search_result",
        "tool_revealed",
        "tool_call",
    ]
    assert [event.stream_seq for event in events] == [0, 1, 2, 3]
    assert events[2].canonical_tool_id == "read"
    assert events[3].same_response_executable is True
    assert events[3].error_code is None


def test_normalizer_marks_non_audited_reveal_call_deferred_until_next_step():
    _catalog, plan = _native_plan(deferred=("video_generate",))
    normalizer = _normalizer(plan)
    items = [
        {
            "id": "tsc_1",
            "type": "tool_search_call",
            "execution": "server",
            "status": "completed",
            "arguments": {"query": "video"},
        },
        {
            "id": "tso_1",
            "type": "tool_search_output",
            "execution": "server",
            "status": "completed",
            "tools": [_deferred_definition(plan, "video_generate")],
        },
        {
            "id": "fc_1",
            "type": "function_call",
            "call_id": "same-call-id",
            "name": "video_generate",
            "arguments": "{}",
        },
    ]
    events = tuple(event for item in items for event in normalizer.feed_item(item))
    call = events[-1]

    assert call.type == "tool_call"
    assert call.call_id == "same-call-id"
    assert call.same_response_executable is False
    assert call.error_code == "deferred_until_next_step"


def test_normalizer_rejects_call_before_reference_unknown_and_schema_substitution():
    _catalog, plan = _native_plan(deferred=("read",))
    with pytest.raises(NativeProtocolError, match="preceded"):
        _normalizer(plan).feed_item(
            {
                "type": "function_call",
                "name": "read",
                "call_id": "call_1",
                "arguments": "{}",
            }
        )

    normalizer = _normalizer(plan)
    normalizer.feed_item(
        {
            "type": "tool_search_call",
            "execution": "server",
            "arguments": {"query": "read"},
        }
    )
    substituted = _deferred_definition(plan, "read")
    substituted["parameters"]["properties"]["marker"]["description"] = "attacker"
    with pytest.raises(NativeProtocolError, match="digest"):
        normalizer.feed_item(
            {
                "type": "tool_search_output",
                "execution": "server",
                "status": "completed",
                "tools": [substituted],
            }
        )

    with pytest.raises(NativeProtocolError, match="unknown"):
        _normalizer(plan).feed_item(
            {
                "type": "function_call",
                "name": "READ",
                "call_id": "call_2",
                "arguments": "{}",
            }
        )


@pytest.mark.parametrize("mutated_field", ["strict", "output_schema", "allowed_callers"])
def test_normalizer_rejects_any_provider_definition_semantic_mutation(mutated_field):
    _catalog, plan = _native_plan(deferred=("read",))
    normalizer = _normalizer(plan)
    normalizer.feed_item(
        {
            "type": "tool_search_call",
            "execution": "server",
            "arguments": {"query": "read"},
        }
    )
    substituted = _deferred_definition(plan, "read")
    substituted[mutated_field] = True if mutated_field == "strict" else {"x": 1}
    with pytest.raises(NativeProtocolError, match="digest"):
        normalizer.feed_item(
            {
                "type": "tool_search_output",
                "execution": "server",
                "status": "completed",
                "tools": [substituted],
            }
        )


def test_normalizer_rejects_mismatched_search_call_and_output_ids():
    _catalog, plan = _native_plan(deferred=("read",))
    normalizer = _normalizer(plan)
    normalizer.feed_item(
        {
            "type": "tool_search_call",
            "execution": "server",
            "call_id": "search-a",
            "arguments": {"query": "read"},
        }
    )
    with pytest.raises(NativeProtocolError, match="id mismatch"):
        normalizer.feed_item(
            {
                "type": "tool_search_output",
                "execution": "server",
                "status": "completed",
                "call_id": "search-b",
                "tools": [],
            }
        )


def test_normalizer_deduplicates_references_and_enforces_aggregate_budgets():
    _catalog, plan = _native_plan(deferred=("read", "grep"))
    normalizer = _normalizer(plan, max_reveals=1)
    normalizer.feed_item(
        {
            "id": "search",
            "type": "tool_search_call",
            "execution": "server",
            "arguments": {"query": "inspect"},
        }
    )
    read = _deferred_definition(plan, "read")
    events = normalizer.feed_item(
        {
            "id": "result",
            "type": "tool_search_output",
            "execution": "server",
            "status": "completed",
            "tools": [read, copy.deepcopy(read)],
        }
    )
    assert [event.type for event in events].count("tool_revealed") == 1

    second = _normalizer(plan, max_reveals=1)
    second.feed_item(
        {
            "type": "tool_search_call",
            "execution": "server",
            "arguments": {"query": "inspect"},
        }
    )
    with pytest.raises(NativeProtocolError, match="reveal budget"):
        second.feed_item(
            {
                "type": "tool_search_output",
                "execution": "server",
                "status": "completed",
                "tools": [
                    _deferred_definition(plan, "read"),
                    _deferred_definition(plan, "grep"),
                ],
            }
        )


def test_idless_repeated_searches_remain_distinct_but_completed_summary_dedupes():
    _catalog, plan = _native_plan(deferred=("read",))
    normalizer = _normalizer(plan)
    call = {
        "type": "tool_search_call",
        "execution": "server",
        "status": "completed",
        "call_id": None,
        "arguments": {"query": "read"},
    }
    output = {
        "type": "tool_search_output",
        "execution": "server",
        "status": "completed",
        "call_id": None,
        "tools": [],
    }
    first = normalizer.feed_sse(
        {"type": "response.output_item.done", "item": call}
    )
    normalizer.feed_sse({"type": "response.output_item.done", "item": output})
    second = normalizer.feed_sse(
        {"type": "response.output_item.done", "item": call}
    )
    normalizer.feed_sse({"type": "response.output_item.done", "item": output})
    assert first[0].stream_seq == 0
    assert second[0].stream_seq == 2
    assert normalizer.search_calls == 2

    # The terminal response repeats both id-less pairs, but they are summaries
    # of the two already-consumed occurrences rather than two new searches.
    assert normalizer.feed_sse(
        {
            "type": "response.completed",
            "response": {"output": [call, output, call, output]},
        }
    ) == ()
    assert normalizer.search_calls == 2

    with pytest.raises(NativeProtocolError, match="call budget"):
        normalizer.feed_item(call)


def test_normalizer_uses_shared_step_aggregate_budget_state():
    _catalog, plan = _native_plan(deferred=("read", "grep"))
    shared = _budget(max_search_calls=1, max_reveals=1, max_result_chars=10_000)
    normalizer = OpenAIResponsesNativeNormalizer(plan, budget_state=shared)
    normalizer.feed_item(
        {
            "type": "tool_search_call",
            "execution": "server",
            "arguments": {"query": "inspect"},
        }
    )
    with pytest.raises(NativeProtocolError, match="reveal budget"):
        normalizer.feed_item(
            {
                "type": "tool_search_output",
                "execution": "server",
                "status": "completed",
                "tools": [
                    _deferred_definition(plan, "read"),
                    _deferred_definition(plan, "grep"),
                ],
            }
        )
    assert shared._capability_search_calls == 1
    assert shared._capability_revealed_ids == {"read"}
    with pytest.raises(NativeProtocolError, match="call budget"):
        normalizer.feed_item(
            {
                "type": "tool_search_call",
                "execution": "server",
                "arguments": {"query": "again"},
            }
        )

    char_state = _budget(max_result_chars=100)
    char_limited = OpenAIResponsesNativeNormalizer(plan, budget_state=char_state)
    char_limited.feed_item(
        {
            "type": "tool_search_call",
            "execution": "server",
            "arguments": {"query": "inspect"},
        }
    )
    with pytest.raises(NativeProtocolError, match="character budget"):
        char_limited.feed_item(
            {
                "type": "tool_search_output",
                "execution": "server",
                "status": "completed",
                "tools": [],
                "opaque_result": "x" * 200,
            }
        )

    reference_state = _budget(max_result_chars=200)
    reference_limited = OpenAIResponsesNativeNormalizer(
        plan,
        budget_state=reference_state,
    )
    reference_limited.feed_item(
        {
            "type": "tool_search_call",
            "execution": "server",
            "arguments": {"query": "inspect"},
        }
    )
    reference_limited.feed_item(
        {
            "type": "tool_search_output",
            "execution": "server",
            "status": "completed",
            "tools": [],
        }
    )
    chars_after_search = reference_state._capability_result_chars
    with pytest.raises(NativeProtocolError, match="character budget"):
        reference_limited.feed_item(
            {
                "type": "tool_reference",
                "tool": _deferred_definition(plan, "read"),
            }
        )
    assert reference_state._capability_result_chars == chars_after_search


def test_native_normalizer_finalize_requires_completed_and_closed_search():
    _catalog, plan = _native_plan(deferred=("read",))
    missing_completed = _normalizer(plan)
    missing_completed.feed_sse(
        {"type": "response.created", "response": {"id": "resp-missing"}}
    )
    with pytest.raises(NativeProtocolError, match="before response.completed"):
        missing_completed.finalize()

    unfinished = _normalizer(plan)
    unfinished.feed_sse(
        {
            "type": "response.completed",
            "response": {
                "id": "resp-unfinished",
                "output": [
                    {
                        "type": "tool_search_call",
                        "execution": "server",
                        "arguments": {"query": "read"},
                    }
                ],
            },
        }
    )
    with pytest.raises(NativeProtocolError, match="unfinished"):
        unfinished.finalize()


def test_native_gate_requires_mode_endpoint_model_binding_and_sticky_state():
    cache = NativeCapabilityCache()
    kwargs = {
        "requested_mode": "native_auto",
        "model_id": "openai/gpt-5.4",
        "configured_endpoint": "https://api.openai.com/v1/",
        "binding": _binding(),
        "endpoint_allowlist": ["https://api.openai.com/v1"],
        "model_allowlist": ["gpt-5.*"],
        "config_generation": "cfg-1",
        "session_id": "session-1",
        "cache": cache,
        "has_deferred_tools": True,
        "catalogue_wire_chars": 10_000,
    }
    first = decide_native_adapter(**kwargs)
    assert first.enabled and first.probe and first.key is not None

    cache.record("session-1", first.key, "unsupported", now=100.0, ttl_seconds=30)
    sticky = decide_native_adapter(**kwargs)
    # The cache record above intentionally uses a historical clock and is
    # expired relative to wall time; record a live state for the sticky check.
    cache.record("session-1", first.key, "unsupported", ttl_seconds=30)
    sticky = decide_native_adapter(**kwargs)
    assert not sticky.enabled and sticky.reason == "sticky_unsupported"

    assert decide_native_adapter(**{**kwargs, "requested_mode": "portable"}).reason == "mode_not_native_auto"
    assert decide_native_adapter(**{**kwargs, "configured_endpoint": "https://proxy.invalid/v1"}).reason == "endpoint_not_allowlisted"
    assert decide_native_adapter(**{**kwargs, "model_id": "openai/gpt-4.1"}).reason == "model_not_allowlisted"
    assert decide_native_adapter(**{**kwargs, "catalogue_wire_chars": 128_001}).reason == "catalogue_wire_hard_limit"

    # Cache identity includes config generation and the complete account/beta
    # binding, so one tenant cannot suppress another tenant's probe.
    other_account = decide_native_adapter(
        **{
            **kwargs,
            "binding": _binding(account_id="account-config:other"),
            "session_id": "session-2",
        }
    )
    assert other_account.enabled and other_account.probe


def test_official_openai_default_endpoint_is_shared_by_wire_binding_and_gate(
    monkeypatch,
):
    config = OpenBoxConfig(
        provider={
            "openai": ProviderConfig(
                api_key="test-key",
                options={
                    "extra_headers": {
                        "OpenAI-Beta": "tools-v2",
                        "X-Api-Key": "must-not-be-persisted",
                    }
                },
            )
        },
    )
    import core.config

    monkeypatch.setattr(core.config, "get_config", lambda: config)
    assert LLM.provider_api_base("openai/gpt-5.4", config=config) == (
        "https://api.openai.com/v1"
    )
    assert LLM._get_provider_kwargs("openai/gpt-5.4")["api_base"] == (
        "https://api.openai.com/v1"
    )
    binding = LLM.provider_tool_binding(
        "openai/gpt-5.4",
        provider_to_canonical={"read": "read"},
        dialect="responses",
        config=config,
    )
    assert binding.endpoint.startswith("https://api.openai.com")
    assert any(value.startswith("openai-beta=sha256:") for value in binding.beta_headers)
    assert "tools-v2" not in repr(binding.beta_headers)
    assert "must-not-be-persisted" not in repr(binding)

    decision = decide_native_adapter(
        requested_mode="native_auto",
        model_id="openai/gpt-5.4",
        configured_endpoint=LLM.provider_api_base(
            "openai/gpt-5.4",
            config=config,
        ),
        binding=binding,
        endpoint_allowlist=["https://api.openai.com/v1"],
        model_allowlist=["gpt-5.*"],
        config_generation="cfg-default-endpoint",
        session_id="session-default-endpoint",
        cache=NativeCapabilityCache(),
        has_deferred_tools=True,
        catalogue_wire_chars=10_000,
    )
    assert decision.enabled is True
    assert decision.probe is True


def test_capability_cache_projects_to_private_session_state_and_expires():
    cache = NativeCapabilityCache()
    key = NativeCapabilityKey("adapter", "a" * 64, "cfg")
    cache.record("s1", key, "unsupported", now=100.0, ttl_seconds=30, reason="no entitlement")
    projected = cache.project_session_state("s1", {"version": 1}, now=110.0)
    restored = NativeCapabilityCache()
    restored.load_session_state("s1", projected, now=120.0)
    assert restored.get("s1", key, now=120.0).status == "unsupported"
    assert restored.get("s1", key, now=131.0) is None


def test_replay_preserves_exact_order_and_rejects_orphans():
    call = {
        "type": "tool_search_call",
        "call_id": "search-1",
        "execution": "server",
        "arguments": {"query": "read"},
    }
    output = {
        "type": "tool_search_output",
        "call_id": "search-1",
        "status": "completed",
        "execution": "server",
        "tools": [],
    }
    records = [
        {"stream_seq": 2, "data": output},
        {"stream_seq": 1, "data": call},
    ]
    assert build_openai_native_replay_input(records) == [call, output]

    with pytest.raises(NativeProtocolError, match="orphan"):
        build_openai_native_replay_input([{"stream_seq": 1, "data": output}])


class _FakeResponse:
    def __init__(self, status_code: int, *, body: bytes = b"", lines=()):
        self.status_code = status_code
        self._body = body
        self._lines = tuple(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def stream(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.response


class _FakeAsyncClient:
    def __init__(self, responses, captures):
        self._responses = responses
        self._captures = captures

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url, **kwargs):
        self._captures.append((method, url, kwargs))
        if not self._responses:
            raise AssertionError("unexpected extra Responses request")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_http_wire_capture_and_pre_stream_error_classification():
    client = _FakeClient(
        _FakeResponse(
            200,
            lines=(
                'data: {"type":"response.created","response":{"id":"r1"}}',
                "data: [DONE]",
            ),
        )
    )
    payload = {"model": "gpt-5.4", "tools": [{"type": "tool_search"}]}
    received = [
        event
        async for event in stream_openai_responses_json(
            client,
            url="https://api.openai.com/v1/responses",
            headers={"Authorization": "Bearer hidden"},
            payload=payload,
        )
    ]
    assert received == [{"type": "response.created", "response": {"id": "r1"}}]
    assert client.requests[0][2]["json"] == payload

    unsupported = _FakeClient(
        _FakeResponse(400, body=b"tool_search is not supported for this model")
    )
    with pytest.raises(NativeFeatureUnsupported):
        _ = [
            event
            async for event in stream_openai_responses_json(
                unsupported,
                url="https://api.openai.com/v1/responses",
                headers={},
                payload=payload,
            )
        ]

    ordinary_schema_error = _FakeClient(
        _FakeResponse(400, body=b"Invalid schema: required field is missing")
    )
    with pytest.raises(NativeHTTPError):
        _ = [
            event
            async for event in stream_openai_responses_json(
                ordinary_schema_error,
                url="https://api.openai.com/v1/responses",
                headers={},
                payload=payload,
            )
        ]

    assert not is_explicit_native_unsupported(
        400,
        "Unknown parameter max_output_tokens; request contains tool_search",
    )
    assert is_explicit_native_unsupported(
        400,
        '{"error":{"param":"tools[1].defer_loading","code":"unknown_parameter"}}',
    )


@pytest.mark.asyncio
async def test_native_fallback_happens_once_only_before_first_event():
    cache = NativeCapabilityCache()
    key = NativeCapabilityKey("adapter", "b" * 64, "cfg")
    calls = {"native": 0, "portable": 0}

    async def unsupported():
        calls["native"] += 1
        raise NativeFeatureUnsupported("unsupported defer_loading")
        yield  # pragma: no cover

    async def portable():
        calls["portable"] += 1
        yield {"type": "finish", "reason": "stop"}

    events = [
        event
        async for event in stream_with_native_fallback(
            session_id="s1",
            key=key,
            cache=cache,
            native_factory=unsupported,
            portable_factory=portable,
        )
    ]
    assert events == [{"type": "finish", "reason": "stop"}]
    assert calls == {"native": 1, "portable": 1}
    assert cache.get("s1", key).status == "unsupported"

    calls = {"native": 0, "portable": 0}

    async def partial_then_fails():
        calls["native"] += 1
        yield {"type": "text_delta", "text": "started"}
        raise NativeFeatureUnsupported("late failure")

    with pytest.raises(NativeFeatureUnsupported, match="late failure"):
        _ = [
            event
            async for event in stream_with_native_fallback(
                session_id="s2",
                key=key,
                cache=cache,
                native_factory=partial_then_fails,
                portable_factory=portable,
            )
        ]
    assert calls == {"native": 1, "portable": 0}


@pytest.mark.asyncio
async def test_real_responses_adapter_wire_stream_and_usage_contract(monkeypatch):
    _catalog, native_plan = _native_plan(deferred=("read",))
    deferred = _deferred_definition(native_plan, "read")
    completed = {
        "type": "response.completed",
        "response": {
            "id": "resp-native-1",
            "usage": {
                "input_tokens": 21,
                "output_tokens": 4,
                "total_tokens": 25,
            },
            "output": [
                {
                    "id": "tsc_1",
                    "type": "tool_search_call",
                    "execution": "server",
                    "status": "completed",
                    "arguments": {"query": "read"},
                },
                {
                    "id": "tso_1",
                    "type": "tool_search_output",
                    "execution": "server",
                    "status": "completed",
                    "tools": [deferred],
                },
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "call_id": "call_native_1",
                    "name": "read",
                    "arguments": '{"marker":"ok"}',
                },
            ],
        },
    }
    responses = [
        _FakeResponse(
            200,
            lines=(
                'data: {"type":"response.created","response":{"id":"resp-native-1"}}',
                f"data: {__import__('json').dumps(completed, separators=(',', ':'))}",
                "data: [DONE]",
            ),
        )
    ]
    captures = []
    monkeypatch.setattr(
        LLM,
        "_get_provider_kwargs",
        lambda _model: {
            "api_key": "secret-not-in-assertions",
            "api_base": "https://api.openai.com/v1",
            "extra_headers": {
                "OpenAI-Beta": "tools-v2",
                "Authorization": "Bearer attacker",
                "X-Api-Key": "attacker-secret",
            },
        },
    )
    import httpx

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeAsyncClient(responses, captures),
    )
    # Keep this contract test independent of LiteLLM's optional provider import
    # graph; only the Responses wire/usage mapping is under test here.
    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(completion_cost=lambda **_kwargs: 0.0),
    )
    capability_results = []

    async def record(status, reason=""):
        capability_results.append((status, reason))
        raise RuntimeError("durable capability state unavailable")

    events = [
        event
        async for event in LLM._stream_responses_api(
            "openai/gpt-5.4",
            ["native instructions"],
            [{"role": "user", "content": "read"}],
            {"bash": _tool("bash")},
            native_plan=native_plan,
            native_discovery_state=_budget(),
            native_record_capability=record,
        )
    ]
    payload = captures[0][2]["json"]
    sent_headers = captures[0][2]["headers"]
    assert len(captures) == 1
    assert any(tool.get("type") == "tool_search" for tool in payload["tools"])
    assert not any(tool.get("name") == "capability_search" for tool in payload["tools"])
    assert next(tool for tool in payload["tools"] if tool.get("name") == "read")["defer_loading"] is True
    assert [event["type"] for event in events if event["type"].startswith("native_")] == [
        "native_search_started",
        "native_search_result",
        "native_tool_revealed",
    ]
    assert not any(
        event["type"] in {"tool_call_start", "tool_call_args_delta"}
        for event in events
    )
    call = next(event for event in events if event["type"] == "tool_call")
    assert call["call_id"] == "call_native_1"
    assert call["native_same_response_executable"] is True
    assert next(event for event in events if event["type"] == "finish")["usage"]["total"] == 25
    assert capability_results == [("supported", "request_completed")]
    assert sent_headers["OpenAI-Beta"] == "tools-v2"
    assert sent_headers["Authorization"] == "Bearer secret-not-in-assertions"
    assert "X-Api-Key" not in sent_headers


@pytest.mark.asyncio
async def test_real_native_stream_waits_for_reveal_before_public_tool_call(monkeypatch):
    _catalog, native_plan = _native_plan(deferred=("read",))
    deferred = _deferred_definition(native_plan, "read")
    import json

    raw_events = (
        {"type": "response.created", "response": {"id": "resp-stream-1"}},
        {
            "type": "response.output_item.done",
            "item": {
                "id": "tsc_stream",
                "type": "tool_search_call",
                "execution": "server",
                "status": "completed",
                "arguments": {"query": "read"},
            },
        },
        {
            "type": "response.output_item.done",
            "item": {
                "id": "tso_stream",
                "type": "tool_search_output",
                "execution": "server",
                "status": "completed",
                "tools": [deferred],
            },
        },
        {
            "type": "response.output_item.added",
            "item": {
                "id": "fc_stream",
                "type": "function_call",
                "call_id": "call_stream",
                "name": "read",
                "arguments": "",
            },
        },
        {
            "type": "response.output_item.done",
            "item": {
                "id": "fc_stream",
                "type": "function_call",
                "call_id": "call_stream",
                "name": "read",
                "arguments": '{"marker":"ok"}',
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp-stream-1",
                "usage": {},
                "output": [],
            },
        },
    )
    responses = [
        _FakeResponse(
            200,
            lines=tuple(
                f"data: {json.dumps(event, separators=(',', ':'))}"
                for event in raw_events
            )
            + ("data: [DONE]",),
        )
    ]
    captures = []
    monkeypatch.setattr(
        LLM,
        "_get_provider_kwargs",
        lambda _model: {
            "api_key": "secret-not-in-assertions",
            "api_base": "https://api.openai.com/v1",
        },
    )
    import httpx

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeAsyncClient(responses, captures),
    )

    events = [
        event
        async for event in LLM._stream_responses_api(
            "openai/gpt-5.4",
            [],
            [{"role": "user", "content": "read"}],
            {"bash": _tool("bash")},
            native_plan=native_plan,
            native_discovery_state=_budget(),
        )
    ]

    assert [event["type"] for event in events] == [
        "native_search_started",
        "native_search_result",
        "native_tool_revealed",
        "tool_call",
        "finish",
    ]
    assert events[3]["args"] == {"marker": "ok"}
    assert events[3]["stream_seq"] == 3


@pytest.mark.asyncio
async def test_real_responses_adapter_pre_stream_fallback_is_one_portable_request(monkeypatch):
    _catalog, native_plan = _native_plan(deferred=("read",))
    responses = [
        _FakeResponse(400, body=b"defer_loading is not supported for this model"),
        _FakeResponse(
            200,
            lines=(
                'data: {"type":"response.created","response":{"id":"portable-1"}}',
                'data: {"type":"response.completed","response":{"id":"portable-1","usage":{},"output":[]}}',
                "data: [DONE]",
            ),
        ),
    ]
    captures = []
    monkeypatch.setattr(
        LLM,
        "_get_provider_kwargs",
        lambda _model: {
            "api_key": "secret-not-in-assertions",
            "api_base": "https://api.openai.com/v1",
        },
    )
    import httpx

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeAsyncClient(responses, captures),
    )
    capability_results = []

    async def record(status, reason=""):
        capability_results.append((status, reason))
        raise RuntimeError("durable capability state unavailable")

    portable_tools = {
        "bash": _tool("bash"),
        "capability_search": _tool("capability_search"),
    }
    events = [
        event
        async for event in LLM._stream_responses_api(
            "openai/gpt-5.4",
            ["native"],
            [{"_responses_input_items": [{"type": "tool_search_call"}]}, {"role": "user", "content": "x"}],
            {"bash": portable_tools["bash"]},
            native_plan=native_plan,
            native_discovery_state=_budget(),
            native_portable_tools=portable_tools,
            native_portable_system=["portable"],
            native_record_capability=record,
        )
    ]
    assert len(captures) == 2
    native_payload, portable_payload = [capture[2]["json"] for capture in captures]
    assert any(tool.get("type") == "tool_search" for tool in native_payload["tools"])
    assert not any(tool.get("type") == "tool_search" for tool in portable_payload["tools"])
    assert any(tool.get("name") == "capability_search" for tool in portable_payload["tools"])
    assert all(item.get("type") != "tool_search_call" for item in portable_payload["input"])
    assert capability_results == [("unsupported", "pre_stream_http_400")]
    assert [event["type"] for event in events] == ["finish"]


@pytest.mark.asyncio
async def test_real_responses_adapter_never_replays_after_first_sse_event(monkeypatch):
    _catalog, native_plan = _native_plan(deferred=("read",))

    class PartialResponse(_FakeResponse):
        async def aiter_lines(self):
            yield 'data: {"type":"response.created","response":{"id":"partial-1"}}'
            raise NativeFeatureUnsupported("connection failed after first event")

    responses = [PartialResponse(200)]
    captures = []
    monkeypatch.setattr(
        LLM,
        "_get_provider_kwargs",
        lambda _model: {
            "api_key": "secret-not-in-assertions",
            "api_base": "https://api.openai.com/v1",
        },
    )
    import httpx

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeAsyncClient(responses, captures),
    )
    events = [
        event
        async for event in LLM._stream_responses_api(
            "openai/gpt-5.4",
            [],
            [{"role": "user", "content": "x"}],
            {"bash": _tool("bash")},
            native_plan=native_plan,
            native_discovery_state=_budget(),
            native_portable_tools={"capability_search": _tool("capability_search")},
        )
    ]
    assert len(captures) == 1
    assert [event["type"] for event in events] == ["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lines", "error_text"),
    [
        (
            (
                'data: {"type":"response.created","response":{"id":"resp-a"}}',
                "data: [DONE]",
            ),
            "before response.completed",
        ),
        (
            (
                'data: {"type":"response.created","response":{"id":"resp-a"}}',
                'data: {"type":"response.completed","response":{"id":"resp-b","output":[]}}',
                "data: [DONE]",
            ),
            "changed response id",
        ),
        (
            (
                'data: {"type":"response.created","response":{"id":"resp-a"}}',
                "data: {malformed-json",
                'data: {"type":"response.completed","response":{"id":"resp-a","output":[]}}',
                "data: [DONE]",
            ),
            "malformed JSON",
        ),
    ],
)
async def test_real_native_stream_fails_closed_without_one_matching_completed(
    monkeypatch,
    lines,
    error_text,
):
    _catalog, native_plan = _native_plan(deferred=("read",))
    responses = [_FakeResponse(200, lines=lines)]
    captures = []
    monkeypatch.setattr(
        LLM,
        "_get_provider_kwargs",
        lambda _model: {
            "api_key": "secret-not-in-assertions",
            "api_base": "https://api.openai.com/v1",
        },
    )
    import httpx

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeAsyncClient(responses, captures),
    )
    capability_results = []

    async def record(status, reason=""):
        capability_results.append((status, reason))

    events = [
        event
        async for event in LLM._stream_responses_api(
            "openai/gpt-5.4",
            [],
            [{"role": "user", "content": "read"}],
            {"bash": _tool("bash")},
            native_plan=native_plan,
            native_discovery_state=_budget(),
            native_record_capability=record,
        )
    ]

    assert len(captures) == 1
    assert [event["type"] for event in events] == ["error"]
    assert error_text in str(events[0]["error"])
    assert not any(status == "supported" for status, _reason in capability_results)
