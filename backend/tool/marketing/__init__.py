"""Builtin tool entry points for the marketing domain."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool.tool import ToolInfo


def load_tools() -> tuple[ToolInfo, ...]:
    """Load implementations only when the builtin catalogue is requested."""
    from tool.marketing.hot_trends import hot_trends_tool
    from tool.marketing.desktop_publish import desktop_publish_tool
    from tool.marketing.douyin_publish import douyin_publish_tool
    from tool.marketing.autopilot_run import autopilot_run_tool

    return (
        hot_trends_tool,
        desktop_publish_tool,
        douyin_publish_tool,
        autopilot_run_tool,
    )
