"""Builtin tool entry points for the desktop domain."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool.tool import ToolInfo


def load_tools() -> tuple[ToolInfo, ...]:
    """Load implementations only when the builtin catalogue is requested."""
    from tool.desktop.computer import computer_tool
    from tool.desktop.browser_mode import browser_mode_tool
    from tool.desktop.desktop_login import desktop_login_tool
    from tool.desktop.desktop_takeover import desktop_takeover_tool

    return (
        computer_tool,
        browser_mode_tool,
        desktop_login_tool,
        desktop_takeover_tool,
    )
