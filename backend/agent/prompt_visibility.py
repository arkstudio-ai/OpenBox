"""Prompt fragments derived from the same exposure plan as provider tools."""
from __future__ import annotations

import re
from typing import Iterable, Mapping

from agent.tool_exposure import AGENT_RESIDENT_IDS, _infer_identity, preferred_editor_id
from core.config import agent_tuning_enabled


_PORTABLE_POLICY = (
    "The tool definitions supplied with this request determine which tools are directly callable. "
    "Treat instructions about tools as conditional on those definitions. "
    "Do not call or simulate unavailable tools.",
    "For recurring reminders and monitoring, use the scheduling tool when available. "
    "Do not implement scheduling with crontab or systemd.",
    "Use the available web tools for current facts or supplied URLs instead of a custom scraper.",
    "Use desktop controls only for UI outside a semantically controllable web page.",
    "For browser interactions, prefer the combined browser action tool when its shell and image tools "
    "are also available: it returns page state and a screenshot together. "
    "Group known dependent actions and inspect once before deciding the next action.",
    "Use the task-list tool when available for genuinely complex multi-step work.",
)


# --- A3: tool names visible, schemas deferred (switch ``tool_name_listing``)
# docs/KERNEL_DSH_COMPARISON_20261128.md section 21 "关于 A3". In portable
# mode the eligible platform built-ins that are deferred by default are
# listed by name with a one-line purpose; their schemas stay deferred and
# are fetched with capability_search. The rules of
# docs/TOOL_SCHEMA_DEFERRED_LOADING_PLAN.md still hold:
# - the input is the static whitelist after permission filtering, so denied
#   tools never appear and per-step narrowing never changes the text;
# - only platform-plane built-ins are listed; sandbox MCP tools and the
#   knowledge loaders (whose text can carry sandbox Skill names) never are;
# - the listing's characters count against the 24K resident budget.

#: Appended to the error for a call to a tool whose schema is not
#: materialized. Generic on purpose: the same words for a hidden, a denied
#: and an unknown name, so it never confirms that a tool exists, and nothing
#: is fetched on the model's behalf (deferred-loading plan 2.3 and 2.4).
UNMATERIALIZED_TOOL_HINT = (
    "To use a tool that is not listed, request it by exact name with capability_search; "
    "if it is available to you, its definition is supplied on the next step."
)

_LISTING_HEADER = (
    "Built-in tools you can load by name (name: purpose). A tool below may have no definition in "
    "this request: request it by exact name with capability_search, then call it on the next step, "
    "once its definition is supplied. Never call a tool whose definition is not supplied."
)

#: Logical slots portable_plan always materializes when eligible
#: (agent/tool_exposure.py). A test keeps this in step with the planner.
_ALWAYS_DIRECT_IDS = frozenset({
    "capability_search", "skill_search", "memory_prepare", "memory_search",
    "memory_read_sources", "memory_read_more", "current_task_state",
})
#: Never listed: the discovery tool itself, and the Skill loaders, whose
#: descriptions can carry sandbox Skill names.
_NEVER_LISTED_IDS = frozenset({"capability_search", "skill", "skill_search"})

LISTING_HINT_CHARS = 120
#: Above this the listing falls back to names only (deterministic, so the
#: text stays stable). Today's build listing is far below it.
LISTING_MAX_CHARS = 6_000

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SENTENCE_END = re.compile(r"(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)[.!?](?=\s|$)|[。！？]")


def _one_line(tool) -> str:
    """First sentence of the tool's hint or description, at most LISTING_HINT_CHARS."""
    raw = str(getattr(tool, "discovery_hint", "") or getattr(tool, "description", "") or "").strip()
    first_line = raw.splitlines()[0] if raw else ""
    text = " ".join(_CONTROL.sub(" ", first_line).split())
    end = _SENTENCE_END.search(text)
    if end:
        text = text[:end.end()]
    if len(text) <= LISTING_HINT_CHARS:
        return text
    for separator in (": ", "; ", " — "):
        head = text.split(separator, 1)[0]
        if 24 <= len(head) <= LISTING_HINT_CHARS:
            return head
    cut = text.rfind(" ", 0, LISTING_HINT_CHARS - 1)
    return text[:cut if cut > 0 else LISTING_HINT_CHARS - 1].rstrip(",;: ") + "…"


def deferred_builtin_listing(
    tools: Mapping[str, object],
    *,
    agent_name: str,
    editor_id: str | None,
) -> tuple[tuple[str, str], ...]:
    """(name, one-line purpose) for each eligible platform built-in deferred by default.

    ``tools`` must be the agent's static whitelist after permission
    filtering (never one step's narrowed set). Resident tools, the model's
    editor and the always-direct discovery slots are already visible and are
    left out. Nothing is listed when capability_search is not eligible: the
    names would then point at tools the model cannot fetch.
    """
    if "capability_search" not in tools:
        return ()
    direct = AGENT_RESIDENT_IDS.get(agent_name, frozenset()) | _ALWAYS_DIRECT_IDS
    if editor_id:
        direct = direct | {editor_id}
    rows = []
    for name, tool in tools.items():
        canonical, provider_name, source, plane = _infer_identity(name, tool)
        if source != "builtin" or plane != "platform":
            continue
        if canonical in direct or canonical in _NEVER_LISTED_IDS:
            continue
        rows.append((provider_name, _one_line(tool)))
    return tuple(sorted(rows))


def tool_name_listing(
    tools: Mapping[str, object],
    *,
    agent_name: str,
    model_id: str,
    workspace_id: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """The A3 listing for ``tools``, or () when ``tool_name_listing`` is off."""
    if not agent_tuning_enabled("tool_name_listing", workspace_id):
        return ()
    return deferred_builtin_listing(
        tools, agent_name=agent_name, editor_id=preferred_editor_id(model_id, tools),
    )


def render_tool_listing(listing: Iterable[tuple[str, str]]) -> list[str]:
    """Lines of the listing block; empty when there is nothing to list."""
    rows = tuple(listing)
    if not rows:
        return []
    lines = [_LISTING_HEADER, *(f"- {name}: {hint}" if hint else f"- {name}" for name, hint in rows)]
    if len("\n".join(lines)) > LISTING_MAX_CHARS:
        lines = [_LISTING_HEADER, *(f"- {name}" for name, _hint in rows)]
    return lines


def build_tool_visibility_fragment(
    visible_tool_names: Iterable[str],
    *,
    strategy: str,
    deferred_count: int = 0,
    listing: Iterable[tuple[str, str]] = (),
) -> str:
    """Describe tool access using the provider payload as the portable frontier.

    ``listing`` (A3) is rendered in portable mode only; pass the output of
    tool_name_listing, which is empty while its switch is off.
    """
    visible = frozenset(visible_tool_names)
    if not visible:
        return (
            "<tool_visibility>\n"
            "No tools are materialized for this request. Answer in text only. "
            "Any generic instruction elsewhere that names a tool is conditional and does not apply.\n"
            "</tool_visibility>"
        )

    if strategy == "portable":
        # Reprinting the active schema names here rewrote the system message
        # every time the portable planner added or removed an optional tool.
        lines = ["<tool_visibility>", *_PORTABLE_POLICY]
    else:
        lines = [
            "<tool_visibility>",
            "The only tools directly callable in this request are: " + ", ".join(sorted(visible)) + ".",
            "Any generic instruction elsewhere that names a tool outside this list is conditional; "
            "do not call or simulate an unavailable tool.",
        ]
    if deferred_count and "capability_search" in visible:
        lines.append(
            "Other eligible capabilities may exist. Use capability_search with one concise task "
            "phrase or exact names; returned typed schemas become callable on the next step."
        )
    elif deferred_count and strategy.startswith("native"):
        lines.append(
            "Other eligible capabilities are deferred. Use the provider's native tool search; "
            "never guess an unreferenced tool name."
        )
    if strategy == "portable":
        # The listing is a stable directory of names, not this step's
        # frontier: it does not depend on ``visible`` or ``deferred_count``.
        return "\n".join([*lines, *render_tool_listing(listing), "</tool_visibility>"])
    if "cron" in visible:
        lines.append("For recurring reminders or monitoring, call cron; do not write crontab/systemd code.")
    if {"web_search", "web_fetch"} & visible:
        lines.append("Use the visible web tools for current facts or supplied URLs instead of a custom scraper.")
    if "computer" in visible:
        lines.append("Use computer only for desktop UI outside a semantically controllable web page.")
    if {"browser_action", "bash", "view_image"} <= visible:
        lines.append("For browser interactions, prefer browser_action: it returns page state and a screenshot "
                     "together. Group known dependent actions; inspect once before deciding the next action.")
    if "todo_write" in visible:
        lines.append("Use todo_write for genuinely complex multi-step work, not trivial answers.")
    lines.append("</tool_visibility>")
    return "\n".join(lines)
