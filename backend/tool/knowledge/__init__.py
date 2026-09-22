"""Builtin tool entry points for the knowledge domain."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool.tool import ToolInfo


def load_tools() -> tuple[ToolInfo, ...]:
    """Load implementations only when the builtin catalogue is requested."""
    from tool.knowledge.skill_tool import skill_tool, skill_search_tool
    from tool.knowledge.skill_manage import skill_manage_tool
    from tool.knowledge.creator_context import creator_context_tool

    return (
        skill_tool,
        skill_search_tool,
        skill_manage_tool,
        creator_context_tool,
    )
