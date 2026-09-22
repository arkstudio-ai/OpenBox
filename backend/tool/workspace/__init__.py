"""Builtin tool entry points for the workspace domain."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool.tool import ToolInfo


def load_tools() -> tuple[ToolInfo, ...]:
    """Load implementations only when the builtin catalogue is requested."""
    from tool.workspace.bash import bash_tool
    from tool.workspace.read import read_tool
    from tool.workspace.write import write_tool
    from tool.workspace.edit import edit_tool
    from tool.workspace.apply_patch import apply_patch_tool
    from tool.workspace.glob_tool import glob_tool
    from tool.workspace.grep import grep_tool
    from tool.workspace.multiedit import multiedit_tool
    from tool.workspace.view_image import view_image_tool
    from tool.workspace.share_file import share_file_tool

    return (
        bash_tool,
        read_tool,
        write_tool,
        edit_tool,
        apply_patch_tool,
        glob_tool,
        grep_tool,
        multiedit_tool,
        view_image_tool,
        share_file_tool,
    )
