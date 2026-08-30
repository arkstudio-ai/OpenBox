"""Prompt fragments derived from the same exposure plan as provider tools."""
from __future__ import annotations

from typing import Iterable


def build_tool_visibility_fragment(
    visible_tool_names: Iterable[str],
    *,
    strategy: str,
    deferred_count: int = 0,
) -> str:
    """Describe the current execution frontier without leaking hidden tools."""
    visible = tuple(sorted(set(visible_tool_names)))
    if not visible:
        return (
            "<tool_visibility>\n"
            "No tools are materialized for this request. Answer in text only. "
            "Any generic instruction elsewhere that names a tool is conditional and does not apply.\n"
            "</tool_visibility>"
        )

    lines = [
        "<tool_visibility>",
        "The only tools directly callable in this request are: " + ", ".join(visible) + ".",
        (
            "Any generic instruction elsewhere that names a tool outside this list is conditional; "
            "do not call or simulate an unavailable tool."
        ),
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
    if "cron" in visible:
        lines.append("For recurring reminders or monitoring, call cron; do not write crontab/systemd code.")
    if {"web_search", "web_fetch"} & set(visible):
        lines.append("Use the visible web tools for current facts or supplied URLs instead of a custom scraper.")
    if "computer" in visible:
        lines.append("Use computer only for desktop UI outside a semantically controllable web page.")
    if "todo_write" in visible:
        lines.append("Use todo_write for genuinely complex multi-step work, not trivial answers.")
    lines.append("</tool_visibility>")
    return "\n".join(lines)
