"""Regression tests for provider-compatible tool schemas."""

import copy
import json
from types import SimpleNamespace

import pytest

import agent.llm as llm
from agent.llm import _inline_refs, _tool_parameters_schema
from tool.registry import get_tool, register_builtin_tools


PERMANENT_MEDIA_TOOLS = (
    "video_project",
    "video_generate",
    "video_transcribe",
    "video_render",
    "video_identity",
    "image_gen",
    "creator_context",
)

EXPECTED_ACTIONS = {
    "video_project": {
        "create", "set_script", "set_segments", "request_approval",
        "revise_segment", "set_segment_feedback", "status",
    },
    "video_generate": {"submit", "status", "wait", "cancel"},
    "video_transcribe": {"submit", "status", "wait", "cancel", "retry"},
    "video_render": {"submit", "status", "wait", "cancel", "retry"},
    "video_identity": {"create", "status", "list", "add_asset"},
    "creator_context": {
        "get_user_context", "write_memory", "propose_memory",
        "search_memories", "list_active_memories",
    },
}


class ParametersThatMustNotBeRead:
    @classmethod
    def model_json_schema(cls):
        raise AssertionError("raw_schema must take precedence")


def test_inline_refs_preserves_a_property_literally_named_title():
    schema = {
        "title": "ProjectArgs",
        "type": "object",
        "properties": {
            "title": {"title": "Title", "type": "string"},
            "brief": {"title": "Brief", "type": "string"},
        },
        "required": ["title", "brief"],
    }

    result = _inline_refs(schema)

    assert set(result["properties"]) == {"title", "brief"}
    assert result["properties"]["title"] == {"type": "string"}
    assert result["required"] == ["title", "brief"]
    assert "ProjectArgs" not in str(result)


def test_inline_refs_is_repeatable_and_does_not_mutate_the_source():
    schema = {
        "$defs": {
            "Item": {
                "title": "Item",
                "type": "object",
                "properties": {
                    "kind": {"enum": ["a", "b"]},
                    "count": {"type": "integer", "minimum": 1, "maximum": 3},
                },
                "required": ["kind", "count"],
            }
        },
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "maxItems": 4,
                "items": {"$ref": "#/$defs/Item"},
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    original = {
        "$defs": {name: dict(value) for name, value in schema["$defs"].items()},
        **{key: value for key, value in schema.items() if key != "$defs"},
    }

    first = _inline_refs(schema)
    second = _inline_refs(schema)

    assert first == second
    assert schema == original
    assert "$ref" not in str(first)
    assert first["properties"]["items"]["maxItems"] == 4
    item = first["properties"]["items"]["items"]
    assert item["required"] == ["kind", "count"]
    assert item["properties"]["kind"]["enum"] == ["a", "b"]
    assert item["properties"]["count"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 3,
    }
    assert first["additionalProperties"] is False


def test_empty_raw_schema_is_respected():
    tool = SimpleNamespace(raw_schema={}, parameters=ParametersThatMustNotBeRead)
    assert _tool_parameters_schema(tool) == {}


def test_permanent_media_tool_schemas_fit_the_request_budget():
    register_builtin_tools()
    sizes = {}
    schemas = {}
    for name in PERMANENT_MEDIA_TOOLS:
        tool = get_tool(name)
        schema = _tool_parameters_schema(tool)
        schemas[name] = schema
        payload = {
            "name": name,
            "description": tool.description,
            "parameters": schema,
        }
        sizes[name] = len(json.dumps(payload, ensure_ascii=False))

        assert schema["type"] == "object", name
        assert schema["properties"], name
        assert schema["required"], name
        assert "$ref" not in json.dumps(schema), name

    assert sum(sizes.values()) <= 10_000, sizes

    for name, actions in EXPECTED_ACTIONS.items():
        assert schemas[name]["required"] == ["action"]
        assert set(schemas[name]["properties"]["action"]["enum"]) == actions

    assert schemas["image_gen"]["required"] == ["prompt"]
    assert schemas["image_gen"]["properties"]["n"]["minimum"] == 1
    assert schemas["image_gen"]["properties"]["n"]["maximum"] == 4
    assert schemas["image_gen"]["properties"]["input_images"]["maxItems"] == 16

    project = schemas["video_project"]["properties"]
    assert (project["target_duration_seconds"]["minimum"],
            project["target_duration_seconds"]["maximum"]) == (15, 180)
    assert project["segments"]["maxItems"] == 100

    for name in ("video_generate", "video_transcribe", "video_render"):
        properties = schemas[name]["properties"]
        assert (properties["wait_seconds"]["minimum"],
                properties["wait_seconds"]["maximum"]) == (0.0, 25.0)
        assert (properties["idempotency_key"]["minLength"],
                properties["idempotency_key"]["maxLength"]) == (3, 180)

    render = schemas["video_render"]["properties"]
    assert (render["width"]["minimum"], render["width"]["maximum"]) == (320, 3840)
    assert render["segment_assets"]["maxItems"] == 100

    identity = schemas["video_identity"]["properties"]
    assert (identity["label"]["minLength"], identity["label"]["maxLength"]) == (1, 120)

    context = schemas["creator_context"]["properties"]
    assert (context["limit"]["minimum"], context["limit"]["maximum"]) == (1, 100)
    assert (context["confidence"]["minimum"],
            context["confidence"]["maximum"]) == (0, 100)

    # The budget is not permission to erase the non-obvious safety contract.
    assert "idempotency_key" in get_tool("video_generate").description
    assert "ambiguous" in get_tool("video_generate").description
    assert "real person" in get_tool("video_identity").description
    assert "virtual" in get_tool("video_identity").description
    assert "confirmation card" in get_tool("creator_context").description
    assert "never crosses users" in get_tool("creator_context").description


def _provider_probe_tools():
    raw_schema = {
        "$defs": {
            "Item": {
                "title": "Item",
                "type": "object",
                "properties": {
                    "title": {"title": "Title", "type": "string"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 3},
                },
                "required": ["title", "count"],
            }
        },
        "title": "Probe",
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "maxItems": 4,
                "items": {"$ref": "#/$defs/Item"},
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    tools = {
        "schema_probe": SimpleNamespace(
            description="nested schema probe",
            raw_schema=raw_schema,
            parameters=ParametersThatMustNotBeRead,
        ),
        "empty_probe": SimpleNamespace(
            description="empty schema probe",
            raw_schema={},
            parameters=ParametersThatMustNotBeRead,
        ),
    }
    return raw_schema, tools


def _assert_stable_normalized_provider_tools(captured, raw_schema, nested):
    assert len(captured) == 2
    first = captured[0]["tools"]
    second = captured[1]["tools"]
    assert first == second

    first_by_name = {
        normalized["name"]: normalized
        for normalized in (nested(item) for item in first)
    }
    schema = first_by_name["schema_probe"]["parameters"]
    assert "$defs" not in schema
    assert "$ref" not in json.dumps(schema)
    assert schema["properties"]["items"]["maxItems"] == 4
    item = schema["properties"]["items"]["items"]
    assert item["properties"]["title"] == {"type": "string"}
    assert item["properties"]["count"] == {
        "type": "integer", "minimum": 1, "maximum": 3,
    }
    assert first_by_name["empty_probe"]["parameters"] == {}

    # Both provider calls reused this exact source object.
    assert "$defs" in raw_schema
    assert raw_schema["properties"]["items"]["items"] == {
        "$ref": "#/$defs/Item"
    }


@pytest.mark.asyncio
async def test_responses_payload_uses_stable_normalized_schemas(monkeypatch):
    import httpx

    raw_schema, tools = _provider_probe_tools()
    original = copy.deepcopy(raw_schema)
    captured = []

    class FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            yield "data: [DONE]"

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, _method, _url, **kwargs):
            captured.append(copy.deepcopy(kwargs["json"]))
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        llm, "_get_provider_kwargs",
        lambda _model: {"api_key": "test", "api_base": "https://example.test/v1"},
    )
    monkeypatch.setattr(llm, "_get_variant_kwargs", lambda *_args: {})
    monkeypatch.setattr(llm, "_get_max_output_tokens", lambda _model: 1000)

    for _ in range(2):
        events = [
            event
            async for event in llm._stream_responses_api(
                "openai/gpt-5-test", [], [], tools
            )
        ]
        assert events[-1]["type"] == "finish"

    _assert_stable_normalized_provider_tools(captured, raw_schema, lambda item: item)
    assert raw_schema == original


@pytest.mark.asyncio
async def test_litellm_payload_uses_stable_normalized_schemas(monkeypatch):
    import litellm

    raw_schema, tools = _provider_probe_tools()
    original = copy.deepcopy(raw_schema)
    captured = []

    class EmptyStream:
        _hidden_params = {}
        usage = None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def fake_completion(**kwargs):
        captured.append(copy.deepcopy(kwargs))
        return EmptyStream()

    # _stream_litellm_direct configures these module globals. Register their
    # current values with monkeypatch so this test restores them afterwards.
    for setting in ("modify_params", "drop_params", "reasoning_auto_summary"):
        monkeypatch.setattr(litellm, setting, getattr(litellm, setting))
    monkeypatch.setattr(litellm, "acompletion", fake_completion)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _model: {})
    monkeypatch.setattr(llm, "_get_variant_kwargs", lambda *_args: {})
    monkeypatch.setattr(llm, "_get_max_output_tokens", lambda _model: 1000)
    monkeypatch.setattr(llm, "_detect_provider", lambda _model: "anthropic")

    for _ in range(2):
        events = [
            event
            async for event in llm._stream_litellm_direct(
                "anthropic/test", [], [], tools
            )
        ]
        assert events[-1]["type"] == "finish"

    def nested(item):
        return item["function"]

    _assert_stable_normalized_provider_tools(captured, raw_schema, nested)
    assert raw_schema == original
