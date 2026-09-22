"""Builtin tool entry points for the discovery domain."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool.tool import ToolInfo


def load_tools() -> tuple[ToolInfo, ...]:
    """Load implementations only when the builtin catalogue is requested."""
    from tool.discovery.capability_search import capability_search_tool
    from tool.discovery.invalid import invalid_tool

    return (
        capability_search_tool,
        invalid_tool,
    )
