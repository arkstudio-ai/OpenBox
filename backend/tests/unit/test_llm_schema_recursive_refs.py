"""Cycle safety and legacy-definition coverage for tool schema inlining."""

import copy

import pytest

from agent.llm import _inline_refs


def test_nested_refs_from_both_definition_keywords_are_fully_inlined():
    schema = {
        "$defs": {
            "Envelope": {
                "type": "object",
                "properties": {
                    "item": {"$ref": "#/definitions/LegacyItem"},
                },
                "required": ["item"],
            },
        },
        "definitions": {
            "LegacyItem": {
                "type": "object",
                "properties": {"count": {"type": "integer", "minimum": 1}},
                "required": ["count"],
            },
        },
        "type": "object",
        "properties": {
            "payload": {
                "$ref": "#/$defs/Envelope",
                "description": "The wrapped item",
            },
        },
        "required": ["payload"],
    }
    original = copy.deepcopy(schema)

    result = _inline_refs(schema)

    assert schema == original
    assert "$defs" not in str(result)
    assert "definitions" not in result
    assert "$ref" not in str(result)
    payload = result["properties"]["payload"]
    assert payload["description"] == "The wrapped item"
    assert payload["properties"]["item"]["properties"]["count"] == {
        "type": "integer",
        "minimum": 1,
    }


def test_a_property_literally_named_ref_is_not_treated_as_a_reference():
    schema = {
        "type": "object",
        "properties": {
            "$ref": {"type": "string"},
        },
        "required": ["$ref"],
    }

    assert _inline_refs(schema) == schema


def test_a_direct_recursive_ref_fails_instead_of_expanding_forever():
    schema = {
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {"child": {"$ref": "#/$defs/Node"}},
            },
        },
        "$ref": "#/$defs/Node",
    }
    original = copy.deepcopy(schema)

    with pytest.raises(ValueError, match="Recursive JSON Schema reference"):
        _inline_refs(schema)

    assert schema == original


def test_mutually_recursive_refs_fail_instead_of_expanding_forever():
    schema = {
        "definitions": {
            "Left": {
                "type": "object",
                "properties": {"right": {"$ref": "#/definitions/Right"}},
            },
            "Right": {
                "type": "object",
                "properties": {"left": {"$ref": "#/definitions/Left"}},
            },
        },
        "$ref": "#/definitions/Left",
    }

    with pytest.raises(
        ValueError,
        match=(
            r"Recursive JSON Schema reference detected: "
            r"#/definitions/Left -> #/definitions/Right -> #/definitions/Left"
        ),
    ):
        _inline_refs(schema)
