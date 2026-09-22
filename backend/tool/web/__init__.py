"""Builtin tool entry points for the web domain."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool.tool import ToolInfo


def load_tools() -> tuple[ToolInfo, ...]:
    """Load implementations only when the builtin catalogue is requested."""
    from tool.web.web_search import web_search_tool
    from tool.web.web_fetch import web_fetch_tool

    return (
        web_search_tool,
        web_fetch_tool,
    )
