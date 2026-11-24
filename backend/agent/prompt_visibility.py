"""Prompt fragments derived from the same exposure plan as provider tools."""
from __future__ import annotations

from typing import Iterable


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


def build_tool_visibility_fragment(
    visible_tool_names: Iterable[str],
    *,
    strategy: str,
    deferred_count: int = 0,
) -> str:
    """Describe tool access using the provider payload as the portable frontier."""
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
        return "\n".join([*lines, "</tool_visibility>"])
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
