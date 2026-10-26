"""Directory discovery that does no remote work for ordinary conversation."""
from __future__ import annotations

import asyncio
from dataclasses import replace

from tool.tool import ToolResult


def deferred_tools(tools, load):
    """Keep built-ins; bind discovery/Skill calls to one scoped lazy snapshot.

    The provider response keeps its original execution allowlist. A newly
    discovered tool must still be revealed and sent on a later model step.
    No background request outlives the originating tool's ownership window.
    """
    lock = asyncio.Lock()
    resolved = None

    def wrap(tool_id, info):
        async def execute(args, ctx):
            nonlocal resolved
            if ctx._activate_tool_catalog is None:
                return ToolResult(title="Capability discovery unavailable",
                    output="The current step cannot safely activate its directory.",
                    metadata={"blocked": True})
            await ctx.assert_run_current()
            async with lock:
                if resolved is None:
                    resolved = await load()
            await ctx.assert_run_current()
            if resolved.catalogue_availability == "unavailable":
                return ToolResult(title="Capability directory unavailable",
                    output="The capability directory could not be read. This does not mean the requested capability is absent.",
                    metadata={"blocked": True, "error_code": "catalogue_unavailable"})
            await ctx._activate_tool_catalog(resolved, ctx)
            selected = resolved.tools.get(tool_id)
            if selected is None:
                return ToolResult(title="Capability unavailable",
                    output="This capability is not available in the current permitted directory.",
                    metadata={"blocked": True})
            return await selected.execute(args, ctx)

        description = info.description
        if tool_id == "skill":
            from tool.skill_tool import COMPACT_SKILL_DESCRIPTION
            description = COMPACT_SKILL_DESCRIPTION
        elif tool_id == "skill_search":
            description = ("Discover permitted specialized workflows using a short task description "
                           "or an exact name. Then use skill to load the selected instructions.")
        return replace(info, execute=execute, description=description)

    return {name: wrap(name, info) if name in {"capability_search", "skill_search", "skill"}
            else info for name, info in tools.items()}
