"""Tool base class and define_tool factory."""
from __future__ import annotations

import asyncio
import posixpath
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
    project_id: str = ""
    sandbox: Any = None  # SandboxClient
    bus: Any = None
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    message_id: str = ""
    part_id: str = ""  # The tool call's part ID
    # Stable for the complete Agent run, including all tool-result steps and
    # compaction. In-memory budgets must not reset with every assistant message.
    run_id: str = ""
    run_generation: int = 0
    agent_id: str = ""
    workdir: str = "/workspace"  # Session-specific working directory
    # Tools exposed for this agent turn. Nested dispatchers such as `batch`
    # must not use the global registry to escape the current agent's allowlist.
    available_tools: frozenset[str] | None = None
    # Exact ToolInfo objects selected for this provider step. Composite tools
    # execute through this immutable lookup instead of re-reading the global
    # registry after a plugin/catalogue generation has changed.
    _tool_execution_lookup: Any = None
    # Permission callback installed by the processor for nested tool calls.
    # It returns a ToolResult when execution must be blocked, else None.
    _authorize_tool: Any = None
    # Staged nested-tool lifecycle installed by ToolHooks for composite tools.
    # Production ``batch`` must use this instead of calling registry executors
    # directly; direct tool unit tests may omit it.
    _nested_tool_runtime: Any = None
    _authorized_tool_id: str = ""
    _authorized_tool_args_key: str = ""
    _on_output: Any = None  # Callback: async fn(output: str) for incremental output updates
    # Exact Agent ownership callback. Agent-loop contexts install the current
    # RunLease method; Cron/compaction/direct tool contexts intentionally leave
    # it unset because they are not owned by an Agent Driver generation.
    _assert_current: Any = None
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
    # Versioned, JSON-safe effective authority that Task persists on a child
    # descriptor.  It is built by the loop after tool resolution and includes
    # every inherited must-pass permission plane.
    _subagent_authority_snapshot: dict[str, Any] | None = None
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

    async def assert_run_current(self) -> None:
        """Fence a provider/tool commit when this context belongs to a run."""
        if self._assert_current is not None:
            await self._assert_current()

    @property
    def run_fence(self) -> tuple[str, str, int] | None:
        if not self.session_id or not self.run_id or self.run_generation <= 0:
            return None
        return self.session_id, self.run_id, self.run_generation

    def resolve_file_path(
        self,
        raw_path: str,
        *,
        allow_user_scope: bool = False,
    ) -> str:
        """Resolve one model path against the Session project directory.

        Models historically learned that ``/workspace`` was their project
        root.  On a shared WUYING desktop the real root is now a stable
        user/project namespace, so both relative paths and that legacy facade
        must be rebased before a file request crosses the execution boundary.
        Public tool payloads stay project-relative; only the private request
        carries the canonical path.

        Mutating tools are confined to ``workdir``.  Read-like callers may opt
        into the same user's attachment/internal roots and scoped Skill root,
        but never another tenant's namespace.
        """
        value = str(raw_path or "").strip()
        if not value or "\x00" in value:
            raise ValueError("file path is empty or invalid")

        workspace_root = "/workspace"
        workdir = posixpath.normpath(str(self.workdir or workspace_root))
        if not posixpath.isabs(workdir):
            raise ValueError("Session workdir is not absolute")
        workspace_backed = workdir == workspace_root or workdir.startswith(
            f"{workspace_root}/"
        )

        normalized_input = posixpath.normpath(value)
        if value == workspace_root and workspace_backed:
            candidate = workdir
        elif value.startswith(f"{workspace_root}/") and workspace_backed:
            if normalized_input == workdir or normalized_input.startswith(
                f"{workdir}/"
            ):
                candidate = normalized_input
            elif normalized_input.startswith(f"{workspace_root}/openbox/users/"):
                candidate = normalized_input
            else:
                candidate = posixpath.join(
                    workdir,
                    normalized_input[len(workspace_root) + 1 :],
                )
        elif value.startswith("/"):
            candidate = normalized_input
        else:
            candidate = posixpath.join(workdir, value)
        candidate = posixpath.normpath(candidate)

        if candidate == workdir or candidate.startswith(f"{workdir}/"):
            return candidate
        if allow_user_scope:
            from project.workspace import user_directory, user_scope_for_identity

            user_root = posixpath.normpath(user_directory(self.user_id or "default"))
            skill_root = f"/data/skills/{user_scope_for_identity(self.user_id or 'default')}"
            if (
                candidate == user_root
                or candidate.startswith(f"{user_root}/")
                or candidate == skill_root
                or candidate.startswith(f"{skill_root}/")
            ):
                return candidate
        raise ValueError("file path escapes the current project")


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
    parallel_safe: bool = False
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
    parallel_safe: bool = False,
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
