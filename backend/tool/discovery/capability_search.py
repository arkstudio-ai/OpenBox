"""Bounded local discovery for eligible but non-materialized tools."""
from __future__ import annotations

import inspect
import re

from pydantic import BaseModel, Field, field_validator

from agent.tool_exposure import EligibleCatalog
from tool.tool import ToolContext, ToolResult, define_tool


MAX_SEARCH_CALLS_PER_STEP = 2
MAX_REVEALS_PER_STEP = 5
MAX_RESULT_CHARS_PER_STEP = 2000

_WORDS = re.compile(r"[\w.-]+", re.UNICODE)


class CapabilitySearchArgs(BaseModel):
    query: str = Field(
        default="",
        max_length=500,
        description="Short description of the capability needed. Use exact names when known.",
    )
    names: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Optional exact canonical IDs or tool names to reveal together.",
    )

    @field_validator("names")
    @classmethod
    def _bounded_names(cls, value: list[str]) -> list[str]:
        cleaned = []
        for name in value:
            candidate = str(name).strip()
            if not candidate or len(candidate) > 128:
                continue
            if candidate not in cleaned:
                cleaned.append(candidate)
        return cleaned[:5]


def _rank(
    catalogue: EligibleCatalog,
    query: str,
    names: list[str],
    allowed_ids: frozenset[str] | None = None,
) -> list[str]:
    entries = catalogue.entries
    allowed = set(entries) if allowed_ids is None else set(allowed_ids) & set(entries)
    if names:
        by_provider = {
            entry.provider_name: tool_id
            for tool_id, entry in entries.items()
            if tool_id in allowed
        }
        exact = []
        for name in names:
            tool_id = name if name in allowed else by_provider.get(name)
            if tool_id and tool_id not in exact:
                exact.append(tool_id)
        return exact

    terms = [token.lower() for token in _WORDS.findall(query) if len(token) > 1]
    if not terms:
        return []
    ranked: list[tuple[int, str]] = []
    for tool_id, entry in entries.items():
        if tool_id not in allowed:
            continue
        haystack = " ".join((
            tool_id,
            entry.provider_name,
            entry.discovery_hint,
            " ".join(entry.parameter_names),
            entry.pack or "",
        )).lower()
        score = 0
        for term in terms:
            if term == tool_id.lower() or term == entry.provider_name.lower():
                score += 20
            elif tool_id.lower().startswith(term) or entry.provider_name.lower().startswith(term):
                score += 8
            elif term in haystack:
                score += 2
        if score:
            ranked.append((score, tool_id))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [tool_id for _score, tool_id in ranked]


async def execute_capability_search(
    args: CapabilitySearchArgs,
    ctx: ToolContext,
) -> ToolResult:
    catalogue = ctx._capability_catalog
    if not isinstance(catalogue, EligibleCatalog):
        return ToolResult(
            title="Capability discovery unavailable",
            output="The local capability catalogue is unavailable for this step.",
            metadata={"blocked": True},
        )
    max_search_calls = max(1, int(ctx._capability_max_search_calls))
    max_reveals = max(1, int(ctx._capability_max_reveals))
    max_result_chars = max(100, int(ctx._capability_max_result_chars))
    if ctx._capability_search_calls >= max_search_calls:
        return ToolResult(
            title="Capability search limit reached",
            output="This step already used the capability search limit. Continue with the returned tools.",
            metadata={"blocked": True},
        )
    ctx._capability_search_calls += 1

    remaining_ids = max_reveals - len(ctx._capability_revealed_ids)
    remaining_chars = max_result_chars - ctx._capability_result_chars
    if remaining_ids <= 0 or remaining_chars <= 0:
        return ToolResult(
            title="Capability reveal limit reached",
            output="The bounded capability result budget for this step is exhausted.",
            metadata={"blocked": True},
        )

    ranked = _rank(
        catalogue,
        args.query,
        args.names,
        ctx._capability_discovery_ids,
    )
    selected: list[str] = []
    blocks: list[str] = []
    used_chars = 0
    for tool_id in ranked:
        if tool_id in ctx._capability_revealed_ids:
            continue
        entry = catalogue.entries[tool_id]
        block = (
            f"id: {entry.id}\n"
            f"name: {entry.provider_name}\n"
            f"when: {entry.discovery_hint}\n"
            f"parameters: {', '.join(entry.parameter_names) or '(none)'}\n"
            "status: typed schema will be available on the next step"
        )
        separator = 5 if blocks else 0
        if len(block) + separator + used_chars > remaining_chars:
            continue
        selected.append(tool_id)
        blocks.append(block)
        used_chars += len(block) + separator
        if len(selected) >= remaining_ids:
            break

    if not selected:
        return ToolResult(
            title="No matching capabilities",
            output="No eligible capabilities matched this query. Try an exact tool name or a narrower task phrase.",
            metadata={"count": 0},
        )

    commit = ctx._commit_tool_reveal
    if commit is None:
        return ToolResult(
            title="Capability reveal unavailable",
            output="Matches were found, but reveal state could not be committed safely.",
            metadata={"blocked": True},
        )
    outcome = commit(
        tuple(selected),
        catalogue.generation,
        {tool_id: catalogue.entries[tool_id].schema_digest for tool_id in selected},
    )
    if inspect.isawaitable(outcome):
        await outcome

    ctx._capability_revealed_ids.update(selected)
    ctx._capability_result_chars += used_chars
    return ToolResult(
        title=f"Found {len(selected)} capabilities",
        output="\n---\n".join(blocks),
        # IDs deliberately do not travel through ordinary metadata. The typed
        # private callback above is the sole state transition.
        metadata={"count": len(selected)},
    )


capability_search_tool = define_tool(
    "capability_search",
    description=(
        "Find eligible OpenBox capabilities whose full schemas are not visible yet. "
        "Search once with a concise task phrase or exact names; returned typed tools appear next step."
    ),
    parameters=CapabilitySearchArgs,
    execute=execute_capability_search,
    sandbox_required=False,
    parallel_safe=False,
    discovery_hint="Find a registered capability not currently shown.",
)
