"""Tool base class and define_tool factory."""
from __future__ import annotations

import asyncio
import time
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
    workspace_id: str = ""
    timezone: str | None = None
    project_id: str = ""
    sandbox: Any = None  # SandboxClient
    sandbox_error: dict | None = None  # Expected unavailability, not a failed LLM turn
    bus: Any = None
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    message_id: str = ""
    part_id: str = ""  # The tool call's part ID
    # Stable for the complete Agent run, including all tool-result steps and
    # compaction. In-memory budgets must not reset with every assistant message.
    run_id: str = ""
    run_generation: int = 0
    guided_video_render: bool = False  # App submission or the same render resumed by the backend
    production_output_language: str = ""  # Also applies to subsequent edits in this conversation.
    agent_id: str = ""
    # Bound by the loop for attribution, never accepted from tool arguments.
    model_id: str = ""
    # Opaque per-plugin state (agent.extensions.plugin_state). The kernel never
    # reads it; a write replaces the mapping, so a copy never changes its parent.
    _plugin_state: dict[str, Any] = field(default_factory=dict, repr=False)
    # Monotonic time the current call's body was admitted; None until it is.
    _body_started: float | None = None
    # The tool part whose request this context last announced to observers;
    # dedupes tool.requested and binds a permission request to its call.
    _requested_part_id: str | None = None
    workdir: str = "/workspace"  # Session-specific working directory
    # Tools exposed for this agent turn. Nested dispatchers such as `batch`
    # must not use the global registry to escape the current agent's allowlist.
    available_tools: frozenset[str] | None = None
    # Exact ToolInfo objects selected for this provider step. Composite tools
    # execute through this immutable lookup instead of re-reading the global
    # registry after a plugin/catalogue generation has changed.
    _tool_execution_lookup: Any = None
    # Ephemeral source projection; only the main loop binds it into the exact
    # model.requested checkpoint. Never populated from model/tool metadata.
    _assistant_context: dict[str, Any] | None = None
    # Bound by the loop to the original user turn, never by tool arguments.
    memory_turn: Any = None
    memory_read_budget: Any = None
    # Actual image bytes retained in this provider request, independent of
    # optional trajectory recording. Never supplied by the model or tool args.
    _resource_image_inputs: dict[str, dict] | None = None
    # Inline bytes actually sent in this request, kept only in memory for a
    # bounded multimodal review. Never persisted as tool metadata or history.
    _image_input_uris: dict[str, str] | None = field(default=None, repr=False)
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
    # Optional combined callback: it MUST check both the question execution
    # and exact Agent Driver lease from fresh SQL, never a cached decision.
    _assert_all_current: Any = None
    # Portable capability discovery. The catalogue has already passed the
    # agent allowlist and whole-tool permission filter. Only the reviewed
    # capability_search implementation invokes the typed commit callback;
    # ordinary ToolResult metadata is never interpreted as a capability grant.
    _capability_catalog: Any = None
    _capability_discovery_ids: frozenset[str] | None = None
    _commit_tool_reveal: Any = None
    # Resolves a previously deferred directory only inside a real discovery
    # call. It may update discovery, never this response's executable tools.
    _activate_tool_catalog: Any = None
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
    # Directors explicitly mark constituent media that are only inputs to a later deliverable.
    transient_output: bool = False
    # The projected Messages the provider received in this step (after the
    # compaction replacement, memory read policy and assistant projection).
    # Bound by the loop, read-only for tools: the Skill loader uses it to tell
    # whether an unchanged body is still in context. None outside the loop.
    _history_view: Any = field(default=None, repr=False)

    async def update_output(self, output: str) -> None:
        """Push incremental output update (for real-time tool display)."""
        if self._on_output:
            await self._on_output(output)

    async def assert_run_current(self) -> None:
        """Fence a provider/tool commit when this context belongs to a run."""
        if self._assert_all_current is not None:
            await self._assert_all_current()
            return
        from question.runtime import assert_current
        await assert_current("tool")
        if self._assert_current is not None:
            await self._assert_current()

    async def assert_dispatch_allowed(self) -> None:
        from assistant.scheduling import require_runnable
        await require_runnable(self.session_id, self.user_id, abort=self.abort)
        from assistant.budget import check_tool
        check_tool(self)

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

        Desktops are assigned per user. Absolute workspace paths keep their
        existing meaning (in particular /workspace/uploads attachments), while
        relative paths resolve inside the current project.

        The existing per-user sandbox owns filesystem access. Keep main's
        absolute paths (including /tmp and installed Skill directories); a
        project directory is a working directory, not a tenant boundary.
        ``allow_user_scope`` remains accepted for existing tool callers.
        """
        value = str(raw_path or "")
        if not value or "\x00" in value:
            raise ValueError("file path is empty or invalid")

        workspace_root = "/workspace"
        workdir = posixpath.normpath(str(self.workdir or workspace_root))
        if not posixpath.isabs(workdir):
            raise ValueError("Session workdir is not absolute")
        normalized_input = posixpath.normpath(value)
        if value.startswith("/"):
            candidate = normalized_input
        else:
            candidate = posixpath.join(workdir, value)
        candidate = posixpath.normpath(candidate)

        return candidate


SandboxRequirement = bool | Callable[[dict], bool]


def needs_sandbox(requirement: SandboxRequirement, arguments) -> bool:
    if callable(requirement):
        return requirement(arguments) if isinstance(arguments, dict) else True
    return requirement


@dataclass
class ToolInfo:
    """Definition of a tool."""
    id: str
    description: str
    parameters: type[BaseModel]
    execute: Callable[..., Awaitable[ToolResult]]
    # Metadata
    sandbox_required: SandboxRequirement = True
    # Whether output should never be pruned. Read by the pruner only while
    # agent_tuning.prune_stale_skill_copies is on (agent/compaction.py).
    never_prune: bool = False
    # Input argument naming what a call loads (``skill`` for the Skill tool).
    # A later completed call with the same value supersedes an earlier one:
    # the pruner keeps the newest output and may clear older ones, even when
    # ``never_prune`` is set. Same switch as above.
    supersede_key: str | None = None
    # Optional coroutine ``(ruleset, workspace_id) -> str | None`` giving the
    # description to show while the tool is deferred in portable mode (the
    # Skill tool's resident catalogue, A4/A7). The kernel calls it instead of
    # importing the tool's package; None keeps ``description``.
    deferred_description: Callable[[list | None, str | None], Awaitable[str | None]] | None = None
    # False for tools that mutate one shared state machine and therefore must
    # never be launched by the generic parallel batch dispatcher.
    # None preserves legacy Batch availability, but runs the tool serially.
    # Only an explicit True enables the new scheduler's parallel execution.
    parallel_safe: bool | None = None
    # Explicit permission for independent, serial children in a composite.
    # This does not grant permission to overlap their paid side effects.
    batch_serial_safe: bool = False
    body_timeout: Callable[[dict, ToolContext], float] | None = None
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
    sandbox_required: SandboxRequirement = True,
    never_prune: bool = False,
    supersede_key: str | None = None,
    deferred_description: Callable[[list | None, str | None], Awaitable[str | None]] | None = None,
    parallel_safe: bool | None = None,
    batch_serial_safe: bool = False,
    body_timeout: Callable[[dict, ToolContext], float] | None = None,
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
        ctx = ctx or ToolContext()
        if getattr(ctx, "sandbox_error", None) and needs_sandbox(sandbox_required, args):
            return ToolResult(
                title="Sandbox unavailable",
                output=ctx.sandbox_error["detail"],
                metadata={"error": True, **ctx.sandbox_error},
            )
        # Validate input (a JSON object sent as a string is decoded once, tool/argument_repair.py)
        try:
            from tool.argument_repair import validate
            validated = validate(parameters, args, tool_id=tool_id)
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
                metadata={"error": True, "error_code": "invalid_arguments"},
            )

        from agent import recording
        trace = recording.context_of(ctx) or recording.current()
        if trace is not None:
            await recording.record("tool.started", {"tool": tool_id,
                "effective_arguments": validated.model_dump(mode="json"),
                "timing_source": "producer_monotonic"}, context=trace)
        # The hooks' output stream when they run this very call; a call made
        # without them records into its own.
        stream = recording.tool_body(ctx, trace, tool=tool_id, owner=wrapped_execute)
        # Execute
        await ctx.assert_dispatch_allowed()
        from assistant.budget import admit_tool, run_tool_body
        await admit_tool(ctx, ctx.part_id)
        ctx._body_started = time.monotonic()
        recording.retain_full_output(ctx, None)
        from sandbox.resource_operation import run_tool_resource_scope
        result = await run_tool_resource_scope(ctx, tool_id, args,
            lambda: run_tool_body(ctx, execute, validated))
        duration = time.monotonic() - ctx._body_started

        # Preserve the execution result before the model-facing presentation
        # applies its length budget. A later tool.finished stores that view.
        if trace is not None:
            retained = recording.retained_output(ctx, result.output)
            # The call's last output: a degraded budget keeps it (NOTES decision 7).
            await stream.finish(retained, title=result.title, metadata=result.metadata,
                                duration_ms=round(duration * 1000, 3))

        # Truncate output
        truncated = await truncate_output(result.output)
        return ToolResult(
            title=result.title,
            output=truncated.content,
            metadata={**result.metadata, "truncated": truncated.truncated, "duration": duration},
        )

    # The shared hooks defer the dispatch boundary until validation succeeds.
    wrapped_execute._validates_arguments = True
    return ToolInfo(
        id=tool_id,
        description=description,
        parameters=parameters,
        execute=wrapped_execute,
        sandbox_required=sandbox_required,
        never_prune=never_prune,
        supersede_key=supersede_key,
        deferred_description=deferred_description,
        parallel_safe=parallel_safe,
        batch_serial_safe=batch_serial_safe,
        body_timeout=body_timeout,
        raw_schema=raw_schema,
        source=source,
        plane=plane,
        canonical_id=canonical_id,
        provider_name=provider_name,
        discovery_hint=discovery_hint,
        pack=pack,
        same_response_safe=same_response_safe,
    )


def tool_body_timeout(tool: ToolInfo | None, args: dict, ctx: ToolContext) -> float:
    """Resolve a trusted tool definition's deadline, never a model argument."""
    import math
    budget = getattr(tool, "body_timeout", None)
    value = budget(args, ctx) if budget else 600.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("tool body timeout must be finite and positive")
    return float(value)
