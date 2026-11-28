"""Main-chat structured output is validated against the caller's schema.

The synthetic tool's parameters are a pass-through model, so before this
anything the model sent was captured as the answer. It now uses the subtask
path's validator: a violation goes back to the model and nothing is captured.
"""
import asyncio

from agent.structured_output import create_structured_output_tool

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["name"],
    "additionalProperties": False,
}


def _run(schema, payload):
    captured = []
    tool = create_structured_output_tool(schema, captured.append)
    result = asyncio.run(tool.execute(payload, None))
    return captured, result


def test_valid_answer_is_captured():
    captured, result = _run(SCHEMA, {"name": "openbox", "count": 3})
    assert captured == [{"name": "openbox", "count": 3}]
    assert result.metadata.get("structured") is True


def test_wrong_type_is_rejected_and_not_captured():
    captured, result = _run(SCHEMA, {"name": "openbox", "count": "three"})
    assert captured == []
    assert result.metadata.get("error") is True
    assert result.metadata.get("structured") is False
    assert "count" in result.output and "StructuredOutput again" in result.output
    assert "subagent" not in result.output


def test_missing_required_field_is_rejected():
    captured, result = _run(SCHEMA, {"count": 1})
    assert captured == []
    assert "name" in result.output


def test_extra_field_is_rejected_when_schema_forbids_it():
    captured, _ = _run(SCHEMA, {"name": "x", "extra": True})
    assert captured == []


def test_schema_outside_the_validator_bounds_keeps_old_behaviour():
    # Not an object schema with properties: captured unvalidated, as before.
    schema = {"type": "object", "additionalProperties": {"type": "string"}}
    captured, result = _run(schema, {"anything": 1})
    assert captured == [{"anything": 1}]
    assert result.metadata.get("structured") is True
