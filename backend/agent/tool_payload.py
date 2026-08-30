"""Canonical tool-definition serialization and payload measurement.

Every provider path and every budget test must use these builders.  Keeping
the byte-counting beside the production serializer prevents a test from
measuring a Pydantic intermediate that is never sent on the wire.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from typing import Literal, Mapping

from core.token import token_estimate
from tool.tool import ToolInfo


ToolDialect = Literal["responses", "litellm"]


@dataclass(frozen=True)
class ToolDefinitionSize:
    """Size breakdown for one serialized tool definition."""

    tool_id: str
    definition_chars: int
    description_chars: int
    schema_chars: int


@dataclass(frozen=True)
class ToolPayloadMetrics:
    """The three distinct definition budgets used by exposure planning."""

    dialect: ToolDialect
    tool_count: int
    catalogue_wire_definition_chars: int
    initial_model_visible_definition_chars: int
    revealed_model_visible_definition_chars: int
    catalogue_wire_proxy_tokens: int
    initial_model_visible_proxy_tokens: int
    revealed_model_visible_proxy_tokens: int
    items: tuple[ToolDefinitionSize, ...]
    source_counts: tuple[tuple[str, int], ...]

    @property
    def largest_items(self) -> tuple[ToolDefinitionSize, ...]:
        return tuple(
            sorted(self.items, key=lambda item: (-item.definition_chars, item.tool_id))[:5]
        )


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@lru_cache(maxsize=1)
def _proxy_encoding():
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


def proxy_token_count(text: str) -> int:
    """Stable CI proxy; provider usage remains the production authority."""

    encoding = _proxy_encoding()
    if encoding is None:
        return token_estimate(text)
    try:
        return len(encoding.encode(text))
    except Exception:
        # Metrics are observability, not permission to reject a working LLM
        # request because a local proxy tokenizer changed underneath us.
        return token_estimate(text)


_NOOP_FUNCTION = {
    "name": "_noop",
    "description": "Placeholder for proxy compatibility",
    "parameters": {"type": "object", "properties": {}},
}


def _parameters_schema(tool: ToolInfo) -> dict:
    # Local import avoids making schema normalization depend on this module
    # while agent.llm keeps the compatibility exports used by existing tests.
    from agent.llm import _tool_parameters_schema

    return _tool_parameters_schema(tool)


def build_tool_definitions(
    tools: Mapping[str, ToolInfo],
    dialect: ToolDialect,
    *,
    include_noop: bool = False,
) -> list[dict]:
    """Build the exact provider definitions in deterministic mapping order."""

    definitions: list[dict] = []
    if include_noop:
        if dialect != "litellm":
            raise ValueError("_noop compatibility definition is LiteLLM-only")
        definitions.append({"type": "function", "function": dict(_NOOP_FUNCTION)})
    for tool_id, tool in tools.items():
        function = {
            "name": tool_id,
            "description": tool.description,
            "parameters": _parameters_schema(tool),
        }
        if dialect == "responses":
            definitions.append({"type": "function", **function})
        elif dialect == "litellm":
            definitions.append({"type": "function", "function": function})
        else:  # pragma: no cover - Literal catches typed callers
            raise ValueError(f"Unsupported tool dialect: {dialect}")
    return definitions


def _definition_id(definition: dict, dialect: ToolDialect) -> str:
    if dialect == "responses":
        return str(definition.get("name", ""))
    return str((definition.get("function") or {}).get("name", ""))


def _function_payload(definition: dict, dialect: ToolDialect) -> dict:
    return definition if dialect == "responses" else definition["function"]


def _serialized_chars(definitions: list[dict]) -> int:
    # An omitted tools field costs zero bytes; an empty JSON array is not sent
    # by either provider adapter.
    return len(_compact(definitions)) if definitions else 0


def measure_tool_definitions(
    tools: Mapping[str, ToolInfo],
    dialect: ToolDialect,
    *,
    direct_ids: frozenset[str] | set[str] | None = None,
    revealed_ids: frozenset[str] | set[str] | None = None,
    sources: Mapping[str, str] | None = None,
    include_noop: bool = False,
) -> ToolPayloadMetrics:
    """Serialize once and report wire, initial-visible, and revealed budgets.

    ``direct_ids=None`` is the legacy-eager baseline: every supplied tool is
    initially visible.  Revealed definitions are a separate delta and default
    to an empty set.
    """

    definitions = build_tool_definitions(tools, dialect, include_noop=include_noop)
    direct = set(tools) if direct_ids is None else set(direct_ids)
    if include_noop:
        direct.add("_noop")
    revealed = set(revealed_ids or ())
    initial_definitions = [
        definition
        for definition in definitions
        if _definition_id(definition, dialect) in direct
    ]
    revealed_definitions = [
        definition
        for definition in definitions
        if _definition_id(definition, dialect) in revealed
    ]

    wire_text = _compact(definitions) if definitions else ""
    initial_text = _compact(initial_definitions) if initial_definitions else ""
    revealed_text = _compact(revealed_definitions) if revealed_definitions else ""
    items = []
    last_index = len(definitions) - 1
    for index, definition in enumerate(definitions):
        function = _function_payload(definition, dialect)
        # Attribute the array delimiters and commas to items so a breakdown
        # sums exactly to the measured wire payload instead of being off by
        # punctuation.  This also makes CI deltas auditable without a hidden
        # global overhead bucket.
        punctuation_chars = 1 + (1 if index == last_index else 0)
        items.append(
            ToolDefinitionSize(
                tool_id=_definition_id(definition, dialect),
                definition_chars=len(_compact(definition)) + punctuation_chars,
                description_chars=len(_compact(function.get("description", ""))),
                schema_chars=len(_compact(function.get("parameters", {}))),
            )
        )

    source_totals: dict[str, int] = {}
    for definition in definitions:
        tool_id = _definition_id(definition, dialect)
        source = "synthetic" if tool_id == "_noop" else (sources or {}).get(tool_id, "unknown")
        source_totals[source] = source_totals.get(source, 0) + 1

    return ToolPayloadMetrics(
        dialect=dialect,
        tool_count=len(definitions),
        catalogue_wire_definition_chars=_serialized_chars(definitions),
        initial_model_visible_definition_chars=_serialized_chars(initial_definitions),
        revealed_model_visible_definition_chars=_serialized_chars(revealed_definitions),
        catalogue_wire_proxy_tokens=proxy_token_count(wire_text),
        initial_model_visible_proxy_tokens=proxy_token_count(initial_text),
        revealed_model_visible_proxy_tokens=proxy_token_count(revealed_text),
        items=tuple(items),
        source_counts=tuple(sorted(source_totals.items())),
    )
