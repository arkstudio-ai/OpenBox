"""Builtin tool entry points for the planning domain."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool.tool import ToolInfo


def load_tools() -> tuple[ToolInfo, ...]:
    """Load implementations only when the builtin catalogue is requested."""
    from tool.planning.todo_tool import todo_read_tool, todo_write_tool
    from tool.planning.plan import plan_enter_tool, plan_exit_tool

    return (
        todo_read_tool,
        todo_write_tool,
        plan_enter_tool,
        plan_exit_tool,
    )
