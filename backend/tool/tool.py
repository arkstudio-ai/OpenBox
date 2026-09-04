"""Tool base class and define_tool factory."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from pydantic import BaseModel, Field

from core.log import create_logger

log = create_logger("tool")


class ToolResult(BaseModel):
    """Result returned by a tool execution."""
    title: str = ""
    output: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass
class ToolContext:
    """Context passed to tool execution functions."""
    session_id: str = ""
    origin_session_id: str = ""  # Original session for cron jobs (cron tool uses this)
    user_id: str = ""
    workspace_id: str = ""
    project_id: str = ""
    sandbox: Any = None  # SandboxClient
    bus: Any = None
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    message_id: str = ""
    part_id: str = ""  # The tool call's part ID
    # Stable for the complete Agent run, including all tool-result steps and
    # compaction. In-memory budgets must not reset with every assistant message.
    run_id: str = ""
    agent_id: str = ""
    workdir: str = "/workspace"  # Session-specific working directory
    # Tools exposed for this agent turn. Nested dispatchers such as `batch`
    # must not use the global registry to escape the current agent's allowlist.
    available_tools: frozenset[str] | None = None
    # Permission callback installed by the processor for nested tool calls.
    # It returns a ToolResult when execution must be blocked, else None.
    _authorize_tool: Any = None
    _authorized_tool_id: str = ""
    _authorized_tool_args_key: str = ""
    _on_output: Any = None  # Callback: async fn(output: str) for incremental output updates
    # Portable capability discovery. The catalogue has already passed the
    # agent allowlist and whole-tool permission filter. Only the reviewed
    # capability_search implementation invokes the typed commit callback;
    # ordinary ToolResult metadata is never interpreted as a capability grant.
    _capability_catalog: Any = None
    _capability_discovery_ids: frozenset[str] | None = None
    _commit_tool_reveal: Any = None
    _capability_search_calls: int = 0
    _capability_revealed_ids: set[str] = field(default_factory=set)
    _capability_result_chars: int = 0
    # Request-scoped hard limits copied from ToolExposureConfig.  Keeping
    # these on the context makes the discovery tool pure with respect to
    # global config reloads: one step observes one immutable budget snapshot.
    _capability_max_search_calls: int = 2
    _capability_max_reveals: int = 5
    _capability_max_result_chars: int = 2_000
    # Provider-native discovery is populated only after endpoint/model/account
    # binding and catalogue gates pass.  The portable slot remains resident in
    # the logical catalogue but is removed from the native provider wire.
    _native_tool_plan: Any = None
    _native_portable_tools: Any = None
    _native_portable_system: Any = None
    _native_binding: Any = None
    _native_capability_key: Any = None
    _native_record_capability: Any = None
    _native_reveal_ttl_seconds: int = 1_800
    _native_max_persisted_reveals: int = 8

    async def update_output(self, output: str) -> None:
        """Push incremental output update (for real-time tool display)."""
        if self._on_output:
            await self._on_output(output)


@dataclass
class ToolInfo:
    """Definition of a tool."""
    id: str
    description: str
    parameters: type[BaseModel]
    execute: Callable[..., Awaitable[ToolResult]]
    # Metadata
    sandbox_required: bool = True  # Whether this tool needs sandbox access
    never_prune: bool = False  # Whether output should never be pruned
    # False for tools that mutate one shared state machine and therefore must
    # never be launched by the generic parallel batch dispatcher.
    parallel_safe: bool = True
    # A JSON Schema to advertise verbatim instead of deriving one from
    # `parameters`. Needed for tools whose shape is only known at runtime —
    # structured output builds its schema from what the caller asked for.
    raw_schema: dict | None = None
    # Exposure identity is platform-owned metadata.  It is intentionally
    # separate from descriptions/SKILL content so untrusted text cannot grant
    # a tool a pack, a trust plane, or same-response execution authority.
    source: str = "builtin"  # builtin | custom | mcp | synthetic
    plane: str = "platform"  # platform | sandbox
    canonical_id: str | None = None
    provider_name: str | None = None
    discovery_hint: str = ""
    pack: str | None = None
    same_response_safe: bool = False


def define_tool(
    tool_id: str,
    *,
    description: str,
    parameters: type[BaseModel],
    execute: Callable[..., Awaitable[ToolResult]],
    sandbox_required: bool = True,
    never_prune: bool = False,
    parallel_safe: bool = True,
    raw_schema: dict | None = None,
    source: str = "builtin",
    plane: str = "platform",
    canonical_id: str | None = None,
    provider_name: str | None = None,
    discovery_hint: str = "",
    pack: str | None = None,
    same_response_safe: bool = False,
) -> ToolInfo:
    """Factory function to create a tool with automatic validation and truncation."""
    from tool.truncation import truncate_output

    async def wrapped_execute(args: dict, ctx: ToolContext) -> ToolResult:
        # Validate input
        try:
            validated = parameters.model_validate(args)
        except Exception as exc:
            # Tool arguments routinely contain prompts, credentials and signed
            # URLs. Keep the useful tool/schema identity without copying the
            # rejected payload into shared service logs.
            log.warning(
                f"Tool {tool_id} validation failed: {type(exc).__name__}"
            )
            return ToolResult(
                title=f"Invalid input for {tool_id}",
                output=f"Parameter validation error: {exc}",
            )

        # Execute
        result = await execute(validated, ctx)

        # Truncate output
        truncated = await truncate_output(result.output)
        return ToolResult(
            title=result.title,
            output=truncated.content,
            metadata={**result.metadata, "truncated": truncated.truncated},
        )

    return ToolInfo(
        id=tool_id,
        description=description,
        parameters=parameters,
        execute=wrapped_execute,
        sandbox_required=sandbox_required,
        never_prune=never_prune,
        parallel_safe=parallel_safe,
        raw_schema=raw_schema,
        source=source,
        plane=plane,
        canonical_id=canonical_id,
        provider_name=provider_name,
        discovery_hint=discovery_hint,
        pack=pack,
        same_response_safe=same_response_safe,
    )
