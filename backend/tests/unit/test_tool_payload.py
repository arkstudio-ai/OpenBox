"""Production tool serialization and definition budget measurement."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import agent.tool_payload as payload
from agent.tool_payload import build_tool_definitions, measure_tool_definitions


class ParametersThatMustNotBeRead:
    @classmethod
    def model_json_schema(cls):
        raise AssertionError("raw_schema must take precedence")


def _tools():
    schema = {
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            }
        },
        "type": "object",
        "properties": {"item": {"$ref": "#/$defs/Item"}},
        "required": ["item"],
    }
    return schema, {
        "alpha": SimpleNamespace(
            description="first definition",
            raw_schema=schema,
            parameters=ParametersThatMustNotBeRead,
        ),
        "beta": SimpleNamespace(
            description="second definition",
            raw_schema={"type": "object", "properties": {}},
            parameters=ParametersThatMustNotBeRead,
        ),
    }


def _compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def test_responses_builder_and_meter_share_the_exact_serialization():
    raw_schema, tools = _tools()
    original = copy.deepcopy(raw_schema)

    definitions = build_tool_definitions(tools, "responses")
    metrics = measure_tool_definitions(
        tools,
        "responses",
        direct_ids={"alpha"},
        revealed_ids={"beta"},
        sources={"alpha": "platform", "beta": "sandbox"},
    )

    assert [item["name"] for item in definitions] == ["alpha", "beta"]
    assert metrics.catalogue_wire_definition_chars == len(_compact(definitions))
    assert metrics.initial_model_visible_definition_chars == len(_compact([definitions[0]]))
    assert metrics.revealed_model_visible_definition_chars == len(_compact([definitions[1]]))
    assert {item.tool_id for item in metrics.items} == {"alpha", "beta"}
    assert sum(item.definition_chars for item in metrics.items) == metrics.catalogue_wire_definition_chars
    assert dict(metrics.source_counts) == {"platform": 1, "sandbox": 1}
    assert raw_schema == original
    assert "$ref" not in _compact(definitions)


def test_litellm_builder_has_the_real_function_wrapper():
    _schema, tools = _tools()
    definitions = build_tool_definitions(tools, "litellm")
    metrics = measure_tool_definitions(tools, "litellm")

    assert definitions[0] == {
        "type": "function",
        "function": {
            "name": "alpha",
            "description": "first definition",
            "parameters": {
                "type": "object",
                "properties": {
                    "item": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    }
                },
                "required": ["item"],
            },
        },
    }
    assert metrics.catalogue_wire_definition_chars == len(_compact(definitions))
    assert metrics.initial_model_visible_definition_chars == metrics.catalogue_wire_definition_chars
    assert metrics.revealed_model_visible_definition_chars == 0


def test_empty_tool_payload_costs_zero_not_an_unsent_empty_array():
    metrics = measure_tool_definitions({}, "responses")
    assert metrics.tool_count == 0
    assert metrics.catalogue_wire_definition_chars == 0
    assert metrics.initial_model_visible_definition_chars == 0
    assert metrics.revealed_model_visible_definition_chars == 0


def test_litellm_noop_is_built_and_measured_by_the_same_serializer():
    definitions = build_tool_definitions({}, "litellm", include_noop=True)
    metrics = measure_tool_definitions({}, "litellm", include_noop=True)

    assert definitions[0]["function"]["name"] == "_noop"
    assert metrics.tool_count == 1
    assert metrics.catalogue_wire_definition_chars == len(_compact(definitions))
    assert metrics.initial_model_visible_definition_chars == metrics.catalogue_wire_definition_chars
    assert dict(metrics.source_counts) == {"synthetic": 1}


def test_proxy_tokenizer_failure_falls_back_without_blocking(monkeypatch):
    class BrokenEncoding:
        def encode(self, _text):
            raise RuntimeError("tokenizer unavailable")

    monkeypatch.setattr(payload, "_proxy_encoding", lambda: BrokenEncoding())
    _schema, tools = _tools()
    metrics = measure_tool_definitions(tools, "responses")
    assert metrics.catalogue_wire_proxy_tokens > 0
