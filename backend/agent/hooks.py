"""Tool execution hooks: permission checks, doom loop detection, SSE events."""
import asyncio
import json
import time
from typing import Any
from contextlib import contextmanager
from contextvars import ContextVar

from agent.doom_loop import DOOM_LOOP_THRESHOLD, is_repeatable_poll
from bus import bus
from bus.events import TOOL_RUNNING, TOOL_COMPLETED, TOOL_ERROR
from permission import permission as perm_mod
from tool.tool import ToolResult, ToolContext
from core.log import create_logger

log = create_logger("agent.hooks")

_execution_context: ContextVar[ToolContext | None] = ContextVar("tool_execution_context", default=None)


def current_tool_context() -> ToolContext | None:
    """The isolated executor identity, also available when recording is disabled."""
    return _execution_context.get()


@contextmanager
def _bind_tool_context(context: ToolContext):
    token = _execution_context.set(context)
    try:
        yield context
    finally:
        _execution_context.reset(token)


class ToolHooks:
    """Wraps tool execution with permission checks, doom loop detection, and SSE events."""

    def __init__(self, session_id: str, user_id: str = "default", config_rules: list | None = None, agent_rules: list | None = None):
        self.session_id = session_id
        self.user_id = user_id
        self.config_rules = config_rules or []
        self.agent_rules = self._parse_agent_rules(agent_rules or [])
        self.call_history: list[tuple[str, str]] = []  # (tool_name, args_json)

    @staticmethod
    def _parse_agent_rules(raw_rules: list[dict]) -> list:
        """Convert agent permission dicts to Rule objects."""
        from permission.permission import Rule
        rules = []
        for r in raw_rules:
            if isinstance(r, dict):
                rules.append(Rule(
                    permission=r.get("permission", "*"),
                    pattern=r.get("pattern", "*"),
                    action=r.get("action", "ask"),
                ))
        return rules

    async def wrap_execute(
        self, tool_id: str, execute_fn: Any, args: dict, ctx: ToolContext,
        part_id: str = "", *, tool_info=None, provider_call_id: str | None = None,
        arguments_raw: str | None = None, requested_recorded: bool = False,
    ) -> ToolResult:
        from agent.trajectory import context_for_tool, public_value, requested_tool_schema
        from trajectory import bind, record
        from core.identifier import ascending
        context = await context_for_tool(ctx)
        if context is not None:
            context = context.derive(
                call_id=part_id or ascending("call"), part_id=part_id or None,
                message_id=ctx.message_id or context.message_id,
            )
            ctx.trace_context = context
        ctx._trajectory_execute_started = None
        ctx._trajectory_full_tool_output = None
        from trajectory.stream_redaction import StreamTextRedactor
        ctx._trajectory_output_redactor = StreamTextRedactor()
        started = time.monotonic()
        with bind(context), _bind_tool_context(ctx):
            if not requested_recorded:
                schema, schema_source = requested_tool_schema(ctx, tool_id, tool_info)
                await record("tool.requested", {
                    "tool": tool_id, "provider_call_id": provider_call_id,
                    "arguments_raw": arguments_raw, "requested_arguments": public_value(args),
                    "schema": schema, "schema_source": schema_source,
                }, context=context)
            try:
                result = await self._wrap_execute_impl(tool_id, execute_fn, args, ctx, part_id)
            except BaseException as exc:
                from trajectory.types import TrajectoryError
                if isinstance(exc, TrajectoryError):
                    raise
                from question.question import QuestionSuspended
                status = "waiting" if isinstance(exc, QuestionSuspended) else (
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
                await record("tool.finished", {
                    "tool": tool_id, "status": status,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                    "total_duration_ms": (time.monotonic() - started) * 1000,
                    "duration_ms": (time.monotonic() - ctx._trajectory_execute_started) * 1000 if ctx._trajectory_execute_started is not None else None,
                    "result_availability": "pending" if status == "waiting" else "unknown",
                    "timing_source": "producer_monotonic",
                }, context=context)
                raise
            execution_duration = result.metadata.get("duration")
            if execution_duration is None and ctx._trajectory_execute_started is not None:
                execution_duration = time.monotonic() - ctx._trajectory_execute_started
            status = "denied" if result.metadata.get("blocked") or result.metadata.get("rejected") else (
                "failed" if result.metadata.get("error") else "completed")
            recorded_model_output = ctx._trajectory_output_redactor.redact(
                result.output, mode="replace", final=True)
            await record("tool.finished", {
                "tool": tool_id, "status": status, "title": result.title,
                "model_output": recorded_model_output["output"], "metadata": public_value(result.metadata),
                "model_output_redaction": recorded_model_output.get("redaction"),
                "duration_ms": execution_duration * 1000 if execution_duration is not None else None,
                "total_duration_ms": (time.monotonic() - started) * 1000,
                "timing_source": "producer_monotonic",
            }, context=context)
            return result

    async def _wrap_execute_impl(
        self,
        tool_id: str,
        execute_fn: Any,
        args: dict,
        ctx: ToolContext,
        part_id: str = "",
    ) -> ToolResult:
        """Wrap a tool execution with hooks."""
        from question.runtime import still_current
        if not await still_current():
            return ToolResult(title="Superseded", output="This run was replaced by a new user message.", metadata={"blocked": True})
        start_time = None
        blocked = await self.authorize_tool(tool_id, args)
        if blocked is not None:
            return blocked

        previous_authorized_id = ctx._authorized_tool_id
        previous_authorized_args = ctx._authorized_tool_args_key
        ctx._authorized_tool_id = tool_id
        ctx._authorized_tool_args_key = json.dumps(
            args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

        # Set part_id on context so tools can reference their own tool call
        ctx.part_id = part_id

        # Publish tool.running event
        bus.publish(TOOL_RUNNING, {
            "userId": self.user_id,
            "sessionId": self.session_id,
            "partId": part_id,
            "tool": tool_id,
            "input": args,
        })

        # Set up incremental output callback for real-time streaming
        from bus.events import PART_UPDATED
        _last_output = {"text": ""}

        async def _on_output(output: str) -> None:
            """Push incremental tool output to frontend via part.updated."""
            from trajectory import record
            recorded_output = ctx._trajectory_output_redactor.redact(output, mode="replace")
            await record("tool.output", {"tool": tool_id, **recorded_output,
                "stage": "executor_stream", "chunk_index": _last_output.get("index", 0)},
                context=getattr(ctx, "trace_context", None))
            _last_output["index"] = _last_output.get("index", 0) + 1
            if output == _last_output["text"]:
                return
            _last_output["text"] = output
            bus.publish(PART_UPDATED, {
                "userId": self.user_id,
                "sessionId": self.session_id,
                "messageId": ctx.message_id,
                "part": {
                    "type": "tool",
                    "id": part_id,
                    "tool": tool_id,
                    "status": "running",
                    "output": output[-2000:] if len(output) > 2000 else output,
                    "input": args,
                },
            })

        ctx._on_output = _on_output

        # Execute. A capable sandbox adds end-to-end trace headers here.
        try:
            if not await still_current(progress=True):
                return ToolResult(title="Superseded", output="This run was replaced by a new user message.", metadata={"blocked": True})
            from trajectory import record
            from agent.trajectory import public_value
            if not getattr(execute_fn, "_trajectory_validates", False):
                await record("tool.started", {"tool": tool_id, "effective_arguments": public_value(args),
                             "timing_source": "producer_monotonic"}, context=getattr(ctx, "trace_context", None))
            start_time = time.monotonic()
            if not getattr(execute_fn, "_trajectory_validates", False):
                ctx._trajectory_execute_started = start_time
            request_context = getattr(ctx.sandbox, "request_context", None)
            if request_context is not None:
                async with request_context(
                    session_id=self.session_id,
                    tool_call_id=part_id,
                    operation=tool_id,
                ):
                    result = await execute_fn(args, ctx)
            else:
                result = await execute_fn(args, ctx)
        except Exception as e:
            from trajectory.types import TrajectoryError
            if isinstance(e, TrajectoryError):
                raise
            from question.question import QuestionSuspended
            if isinstance(e, QuestionSuspended):
                raise
            from sandbox.entitlement import SandboxSubscriptionRequired
            if isinstance(e, SandboxSubscriptionRequired):
                return ToolResult(title="Sandbox unavailable", output=e.detail,
                    metadata={"error": True, **e.payload})
            # Handle plan mode rejection gracefully (not a real error)
            from tool.plan import PlanRejectedError
            from question.question import QuestionRejectedError
            if isinstance(e, (PlanRejectedError, QuestionRejectedError)):
                return ToolResult(
                    title="Rejected",
                    output=str(e),
                    metadata={"rejected": True},
                )

            # Handle container connection errors with a clear message
            import httpx as _httpx
            if isinstance(e, (_httpx.ConnectError, _httpx.ReadError, _httpx.RemoteProtocolError, ConnectionError, OSError)):
                log.warning(f"Container connection error during {tool_id}: {e}")
                bus.publish(TOOL_ERROR, {
                    "userId": self.user_id,
                    "sessionId": self.session_id,
                    "partId": part_id,
                    "error": f"Container unavailable: {e}",
                })
                return ToolResult(
                    title="Container Error",
                    output=f"The sandbox container is not available: {e}. The container will be recreated automatically on the next attempt.",
                    metadata={"error": True, "container_error": True},
                )

            bus.publish(TOOL_ERROR, {
                "userId": self.user_id,
                "sessionId": self.session_id,
                "partId": part_id,
                "error": str(e),
            })
            return ToolResult(
                title=f"Error in {tool_id}",
                output=str(e),
                metadata={"error": True},
            )
        finally:
            ctx._on_output = None  # Clean up callback
            ctx._authorized_tool_id = previous_authorized_id
            ctx._authorized_tool_args_key = previous_authorized_args

        duration = time.monotonic() - start_time if start_time is not None else None
        bus.publish(TOOL_COMPLETED, {
            "userId": self.user_id,
            "sessionId": self.session_id,
            "partId": part_id,
            "output": result.output[:2000] if result.output else "",
            "title": result.title,
        })

        if not getattr(execute_fn, "_trajectory_validates", False):
            result.metadata["duration"] = duration
            recorded_output = ctx._trajectory_output_redactor.redact(
                ctx._trajectory_full_tool_output if ctx._trajectory_full_tool_output is not None else result.output,
                mode="replace", final=True)
            await record("tool.output", {"tool": tool_id, **recorded_output, "title": result.title,
                "metadata": public_value(result.metadata), "stage": "executor_result",
                "duration_ms": duration * 1000 if duration is not None else None},
                context=getattr(ctx, "trace_context", None))
        return result

    async def authorize_tool(self, tool_id: str, args: dict) -> ToolResult | None:
        """Apply doom-loop and permission policy to direct and nested calls."""
        # Doom loop detection (check BEFORE normal permission)
        is_doom = self._check_doom_loop(tool_id, args)
        if is_doom:
            try:
                await perm_mod.ask(
                    session_id=self.session_id,
                    permission="doom_loop",
                    patterns=[tool_id],
                    input_data=args,
                    metadata={"tool": tool_id, "input": args},
                    config_rules=self.config_rules,
                    is_doom_loop=True,
                    user_id=self.user_id,
                )
            except (perm_mod.PermissionDeniedError, perm_mod.PermissionRejectedError):
                return ToolResult(
                    title="Doom loop detected",
                    output=f"The same tool call ({tool_id}) was repeated {DOOM_LOOP_THRESHOLD} times with identical arguments. Execution was blocked.",
                    metadata={"blocked": True, "doom_loop": True},
                )

        # Agent-level permission check (takes precedence over config rules)
        # Agent rules are evaluated first; if they produce a deny, block immediately.
        # Map edit-family tools to the "edit" permission (matching opencode's EDIT_TOOLS)
        perm_name = "edit" if tool_id in perm_mod.EDIT_TOOLS else tool_id
        patterns = self._extract_patterns(tool_id, args)

        # In Docker sandbox, "always allow" grants blanket permission for the tool
        always_patterns = ["*"]

        merged_rules = (self.config_rules + self.agent_rules) if self.agent_rules else self.config_rules
        try:
            await perm_mod.ask(
                session_id=self.session_id,
                permission=perm_name,
                patterns=patterns,
                input_data=args,
                config_rules=merged_rules,
                always=always_patterns,
                user_id=self.user_id,
            )
        except perm_mod.PermissionDeniedError:
            return ToolResult(
                title="Permission denied",
                output=f"Permission denied for tool '{tool_id}'. This tool is restricted in the current agent mode.",
                metadata={"blocked": True},
            )
        except perm_mod.PermissionCorrectedError as e:
            return ToolResult(
                title="Permission rejected with feedback",
                output=f"The user rejected with feedback: {e.feedback}",
                metadata={"blocked": True},
            )
        except perm_mod.PermissionRejectedError:
            return ToolResult(
                title="Permission rejected",
                output="The user rejected permission to use this tool.",
                metadata={"blocked": True},
            )

        # Record call for doom loop detection
        call_sig = json.dumps(args, sort_keys=True)
        self.call_history.append((tool_id, call_sig))
        return None

    def _extract_patterns(self, tool_id: str, args: dict) -> list[str]:
        """The subject a permission rule matches against, taken from the args.

        Falling back to "*" is not a safe default — it is the opposite. A rule
        like skill/secret-* => deny is evaluated against the pattern passed in,
        and "*" does not match "secret-*", so the rule never fires and the call
        is allowed. Any tool whose rules are written per-target has to name that
        target here or its deny rules are decorative.
        """
        if tool_id == "bash":
            return [args.get("command", "")]
        elif tool_id in ("read", "write", "edit", "multiedit", "apply_patch"):
            return [args.get("file_path", "")]
        elif tool_id == "glob":
            return [args.get("pattern", "")]
        elif tool_id == "grep":
            return [args.get("pattern", "")]
        elif tool_id == "skill":
            return [args.get("skill", "")]
        elif tool_id == "mcp_read_resource":
            # Resource bodies are not catalogue data. Authorize the exact raw
            # server/URI tuple through a fixed-size, unambiguous subject before
            # the executor can fetch any body bytes. Existing rule evaluation
            # remains last-match-wins.
            from tool.mcp_tool import _canonical_resource_id

            return [_canonical_resource_id(args.get("server"), args.get("uri"))]
        elif tool_id == "web_fetch":
            return [args.get("url", "")]
        return ["*"]

    def _check_doom_loop(self, tool_id: str, args: dict) -> bool:
        """Check if we're in a doom loop (same call repeated N times)."""
        if is_repeatable_poll(tool_id, args):
            return False
        if len(self.call_history) < DOOM_LOOP_THRESHOLD:
            return False

        call_sig = json.dumps(args, sort_keys=True)
        recent = self.call_history[-DOOM_LOOP_THRESHOLD:]
        return all(
            name == tool_id and sig == call_sig
            for name, sig in recent
        )
