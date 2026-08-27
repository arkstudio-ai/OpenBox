"""Regression tests for provider-compatible tool schemas."""

from agent.llm import _inline_refs


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
