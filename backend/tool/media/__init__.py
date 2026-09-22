"""Builtin tool entry points for the media domain."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool.tool import ToolInfo


def load_tools() -> tuple[ToolInfo, ...]:
    """Load implementations only when the builtin catalogue is requested."""
    from tool.media.image_gen import image_gen_tool
    from tool.media.video_production import video_generate_tool, video_transcribe_tool
    from tool.media.video_compose import video_compose_tool
    from tool.media.video_analyze import video_analyze_tool

    return (
        image_gen_tool,
        video_generate_tool,
        video_transcribe_tool,
        video_compose_tool,
        video_analyze_tool,
    )
