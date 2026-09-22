"""Builtin tool entry points for the collaboration domain."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool.tool import ToolInfo


def load_tools() -> tuple[ToolInfo, ...]:
    """Load implementations only when the builtin catalogue is requested."""
    from tool.collaboration.task import task_tool
    from tool.collaboration.batch import batch_tool
    from tool.collaboration.agent_manage import agent_manage_tool
    from tool.collaboration.team_tools import team_tools

    return (
        task_tool,
        batch_tool,
        agent_manage_tool,
        *team_tools,
    )
