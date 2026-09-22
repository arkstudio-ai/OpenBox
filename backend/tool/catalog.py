"""Functional categories for builtin tools, independent of execution authority.

Each domain owns its lazy loader. This catalogue only composes those loaders;
it does not enable a tool for an Agent, grant permissions, or probe services.
MCP/plugin tools and synthetic output tools retain their own lifecycles.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tool.tool import ToolInfo


@dataclass(frozen=True)
class ToolGroup:
    id: str
    title: str
    description: str

    @property
    def module(self) -> str:
        return f"tool.{self.id}"

    def load(self) -> tuple[ToolInfo, ...]:
        return import_module(self.module).load_tools()


BUILTIN_GROUPS = (
    ToolGroup("workspace", "Files and execution", "Read, search, edit, execute commands, inspect images and deliver files."),
    ToolGroup("web", "Web access", "Search the internet and fetch web pages."),
    ToolGroup("desktop", "Desktop and browser", "Operate the desktop, select browser mode, manage login and handover."),
    ToolGroup("knowledge", "Skills and memory", "Discover and load skills, manage skill packages and access creator context."),
    ToolGroup("planning", "Planning and todos", "Maintain task lists and enter or leave planning mode."),
    ToolGroup("collaboration", "Agents and teams", "Delegate work, manage reusable Agents and coordinate durable teams."),
    ToolGroup("interaction", "User interaction", "Ask questions and collect user answers."),
    ToolGroup("automation", "Scheduled work", "Create and manage scheduled Agent runs."),
    ToolGroup("media", "Image and video", "Generate images and generate, transcribe, compose or analyze video."),
    ToolGroup("marketing", "Publishing and marketing", "Research trends, publish content and record marketing runs."),
    ToolGroup("discovery", "Tool discovery and protocol", "Discover eligible tools and report invalid tool calls."),
)


def load_builtin_groups() -> tuple[tuple[ToolGroup, tuple[ToolInfo, ...]], ...]:
    """Load and validate all groups before a caller changes the live registry."""
    groups = []
    group_ids: set[str] = set()
    tool_ids: set[str] = set()
    for group in BUILTIN_GROUPS:
        if group.id in group_ids:
            raise ValueError(f"Duplicate builtin tool group: {group.id}")
        group_ids.add(group.id)
        tools = group.load()
        for tool in tools:
            if tool.id in tool_ids:
                raise ValueError(f"Duplicate builtin tool: {tool.id}")
            tool_ids.add(tool.id)
        groups.append((group, tools))
    return tuple(groups)


def describe_builtin_groups() -> list[dict]:
    """Describe builtin membership without registering or activating tools.

    This is an implementation inventory, not a session's authorized tool list
    or a health report. Runtime selection stays in agent/tool_resolution.py;
    team delegation stays in team/policy.py.
    """
    return [
        {
            "id": group.id,
            "title": group.title,
            "description": group.description,
            "module": group.module,
            "tools": [tool.id for tool in tools],
        }
        for group, tools in load_builtin_groups()
    ]


if __name__ == "__main__":
    import json

    print(json.dumps(describe_builtin_groups(), ensure_ascii=False, indent=2))
