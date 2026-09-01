"""Tool execution hooks: permission checks, doom loop detection, SSE events."""
import copy
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from agent.doom_loop import DOOM_LOOP_THRESHOLD, is_repeatable_poll
from bus import bus
from bus.events import TOOL_RUNNING, TOOL_COMPLETED, TOOL_ERROR
from permission import permission as perm_mod
from tool.tool import ToolResult, ToolContext
from core.log import create_logger

log = create_logger("agent.hooks")


def _httpx_targets_sandbox(exc: Exception, ctx: ToolContext) -> bool:
    """Classify transport failures by the actual request origin.

    A provider/OSS outage must not be diagnosed as a dead cloud desktop merely
    because the same tool also has a sandbox context.
    """
    request = getattr(exc, "request", None)
    request_url = str(getattr(request, "url", "") or "")
    sandbox_url = str(getattr(getattr(ctx, "sandbox", None), "base_url", "") or "")
    if not request_url or not sandbox_url:
        return False
    def normalized_origin(raw: str) -> tuple[str, str, int | None] | None:
        try:
            parsed = urlsplit(raw)
            scheme = parsed.scheme.casefold()
            hostname = (parsed.hostname or "").casefold()
            if not scheme or not hostname:
                return None
            port = parsed.port
        except ValueError:
            # A malformed request URL must never be allowed to impersonate the
            # platform-owned sandbox origin.
            return None
        if port is None:
            port = {"http": 80, "https": 443}.get(scheme)
        return scheme, hostname, port

    request_origin = normalized_origin(request_url)
    sandbox_origin = normalized_origin(sandbox_url)
    return request_origin is not None and request_origin == sandbox_origin


def _generation_payload(ctx: ToolContext, payload: dict) -> dict:
    generation = int(getattr(ctx, "run_generation", 0) or 0)
    if generation > 0:
        payload["generation"] = generation
    return payload


@dataclass
class PreparedToolExecution:
    """Permission-approved execution state owned by one scheduler slot."""

    tool_id: str
    execute_fn: Any
    args: dict
    shared_ctx: ToolContext
    run_ctx: ToolContext
    part_id: str
    start_time: float
    isolated: bool
    previous_authorized_id: str = ""
    previous_authorized_args: str = ""
    previous_output_callback: Any = None
    previous_authorize_callback: Any = None
    previous_nested_runtime: Any = None
    baseline_search_calls: int = 0
    baseline_result_chars: int = 0
    baseline_revealed_ids: frozenset[str] = frozenset()
    blocked_result: ToolResult | None = None
    cleaned: bool = False


@dataclass(frozen=True)
class ToolDispatchOutcome:
    """A body result parked until its model-order post/terminal commit."""

    result: ToolResult
    terminal_event: str | None = None  # completed | error | None
    terminal_error: str = ""


class ToolHooks:
    """Wraps tool execution with permission checks, doom loop detection, and SSE events."""

    def __init__(
        self,
        session_id: str,
        user_id: str = "default",
        config_rules: list | None = None,
        agent_rules: list | None = None,
        guard_rules: list | None = None,
        authority_rule_planes: list | tuple = (),
        authority_guard_planes: list | tuple = (),
        workdir: str = "/workspace",
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.config_rules = config_rules or []
        self.agent_rules = self._parse_agent_rules(agent_rules or [])
        self.guard_rules = guard_rules or []
        self.authority_rule_planes = self._parse_rule_planes(
            authority_rule_planes
        )
        self.authority_guard_planes = self._parse_rule_planes(
            authority_guard_planes
        )
        self.workdir = workdir or "/workspace"
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

    @staticmethod
    def _parse_rule_planes(raw_planes) -> list[list]:
        """Copy already-validated durable planes into call-local rulesets."""
        from permission.permission import Rule

        planes: list[list[Rule]] = []
        for raw_plane in raw_planes or ():
            plane: list[Rule] = []
            for raw in raw_plane:
                if isinstance(raw, Rule):
                    plane.append(Rule.model_validate(raw.model_dump()))
                elif isinstance(raw, dict):
                    plane.append(Rule.model_validate(raw))
                else:
                    raise ValueError("invalid inherited authority rule")
            if not plane:
                raise ValueError("inherited authority plane cannot be empty")
            planes.append(plane)
        return planes

    async def wrap_execute(
        self,
        tool_id: str,
        execute_fn: Any,
        args: dict,
        ctx: ToolContext,
        part_id: str = "",
    ) -> ToolResult:
        """Compatibility entry point for ordinary serial/nested callers."""
        prepared = await self.prepare_execute(
            tool_id,
            execute_fn,
            args,
            ctx,
            part_id=part_id,
            isolate_context=False,
        )
        if prepared.blocked_result is not None:
            return prepared.blocked_result
        outcome = await self.dispatch_execute(prepared)
        return await self.finalize_execute(prepared, outcome)

    async def prepare_execute(
        self,
        tool_id: str,
        execute_fn: Any,
        args: dict,
        ctx: ToolContext,
        *,
        part_id: str = "",
        isolate_context: bool = False,
    ) -> PreparedToolExecution:
        """Run permission/policy in order and prepare a body-only dispatch.

        Parallel bodies receive a shallow call-local ToolContext plus detached
        mutable budget state. Their externally visible context is merged only
        by :meth:`finalize_execute`, which the scheduler invokes in model order.
        """
        await ctx.assert_run_current()
        start_time = time.time()
        authorizer = self.authorize_tool
        if getattr(authorizer, "__func__", None) is ToolHooks.authorize_tool:
            blocked = await authorizer(tool_id, args, ctx=ctx)
        else:
            # Preserve compatibility with tests and extensions that replace
            # the hook with the historical two-argument callback.
            blocked = await authorizer(tool_id, args)
        await ctx.assert_run_current()
        if blocked is not None:
            return PreparedToolExecution(
                tool_id=tool_id,
                execute_fn=execute_fn,
                args=args,
                shared_ctx=ctx,
                run_ctx=ctx,
                part_id=part_id,
                start_time=start_time,
                isolated=False,
                blocked_result=blocked,
            )

        run_ctx = copy.copy(ctx) if isolate_context else ctx
        baseline_revealed = frozenset(getattr(ctx, "_capability_revealed_ids", set()))
        if isolate_context and hasattr(run_ctx, "_capability_revealed_ids"):
            run_ctx._capability_revealed_ids = set(baseline_revealed)

        previous_authorized_id = getattr(run_ctx, "_authorized_tool_id", "")
        previous_authorized_args = getattr(run_ctx, "_authorized_tool_args_key", "")
        previous_output_callback = getattr(run_ctx, "_on_output", None)
        previous_authorize_callback = getattr(run_ctx, "_authorize_tool", None)
        previous_nested_runtime = getattr(run_ctx, "_nested_tool_runtime", None)
        if (
            getattr(previous_authorize_callback, "__self__", None) is self
            and getattr(previous_authorize_callback, "__func__", None)
            is ToolHooks.authorize_tool
        ):
            async def _authorize_nested(nested_tool_id: str, nested_args: dict):
                return await self.authorize_tool(
                    nested_tool_id, nested_args, ctx=run_ctx
                )

            run_ctx._authorize_tool = _authorize_nested
        run_ctx._authorized_tool_id = tool_id
        run_ctx._authorized_tool_args_key = json.dumps(
            args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

        # Set part_id on the call-local context so tools can reference their
        # own card even while a sibling body is running.
        run_ctx.part_id = part_id
        if tool_id == "batch" and run_ctx.session_id and run_ctx.message_id:
            from agent.nested_tool_runtime import NestedToolRuntime

            run_ctx._nested_tool_runtime = NestedToolRuntime(self, run_ctx)

        # Publish tool.running event
        bus.publish(TOOL_RUNNING, _generation_payload(run_ctx, {
            "userId": self.user_id,
            "sessionId": self.session_id,
            "partId": part_id,
            "tool": tool_id,
            "input": args,
        }))

        # Set up incremental output callback for real-time streaming
        from bus.events import PART_UPDATED
        _last_output = {"text": ""}

        async def _on_output(output: str) -> None:
            """Push incremental tool output to frontend via part.updated."""
            await run_ctx.assert_run_current()
            if output == _last_output["text"]:
                return
            _last_output["text"] = output
            bus.publish(PART_UPDATED, _generation_payload(run_ctx, {
                "userId": self.user_id,
                "sessionId": self.session_id,
                "messageId": run_ctx.message_id,
                "part": {
                    "type": "tool",
                    "id": part_id,
                    "tool": tool_id,
                    "status": "running",
                    "output": output[-2000:] if len(output) > 2000 else output,
                    "input": args,
                },
            }))

        run_ctx._on_output = _on_output

        return PreparedToolExecution(
            tool_id=tool_id,
            execute_fn=execute_fn,
            args=args,
            shared_ctx=ctx,
            run_ctx=run_ctx,
            part_id=part_id,
            start_time=start_time,
            isolated=isolate_context,
            previous_authorized_id=previous_authorized_id,
            previous_authorized_args=previous_authorized_args,
            previous_output_callback=previous_output_callback,
            previous_authorize_callback=previous_authorize_callback,
            previous_nested_runtime=previous_nested_runtime,
            baseline_search_calls=int(getattr(ctx, "_capability_search_calls", 0)),
            baseline_result_chars=int(getattr(ctx, "_capability_result_chars", 0)),
            baseline_revealed_ids=baseline_revealed,
        )

    async def dispatch_execute(
        self,
        prepared: PreparedToolExecution,
    ) -> ToolDispatchOutcome:
        """Run only the around/body phase; no terminal state is published."""
        if prepared.blocked_result is not None:
            return ToolDispatchOutcome(prepared.blocked_result)

        tool_id = prepared.tool_id
        part_id = prepared.part_id
        ctx = prepared.run_ctx
        await ctx.assert_run_current()

        # Execute. A capable sandbox adds end-to-end trace headers here.
        try:
            request_context = getattr(ctx.sandbox, "request_context", None)
            if request_context is not None:
                async with request_context(
                    session_id=self.session_id,
                    tool_call_id=part_id,
                    operation=tool_id,
                ):
                    result = await prepared.execute_fn(prepared.args, ctx)
            else:
                result = await prepared.execute_fn(prepared.args, ctx)
        except Exception as e:
            # Handle plan mode rejection gracefully (not a real error)
            from tool.plan import PlanRejectedError
            from question.question import QuestionRejectedError
            if isinstance(e, (PlanRejectedError, QuestionRejectedError)):
                return ToolDispatchOutcome(
                    ToolResult(
                        title="Rejected",
                        output=str(e),
                        metadata={"rejected": True},
                    )
                )

            # Handle container connection errors with a clear message
            import httpx as _httpx
            if isinstance(e, _httpx.TransportError) and not _httpx_targets_sandbox(e, ctx):
                log.warning(
                    "External service transport error during %s: %s",
                    tool_id,
                    type(e).__name__,
                )
                return ToolDispatchOutcome(
                    ToolResult(
                        title="External Service Error",
                        output=(
                            "The external provider or object store connection failed. "
                            "The operation was not automatically retried because its outcome may be unknown."
                        ),
                        metadata={
                            "error": True,
                            "failure_code": "external_transport_error",
                            "outcome_unknown": True,
                        },
                    ),
                    terminal_event="error",
                    terminal_error=f"External transport failed: {type(e).__name__}",
                )
            if isinstance(e, (_httpx.TransportError, ConnectionError, OSError)):
                log.warning(f"Container connection error during {tool_id}: {e}")
                return ToolDispatchOutcome(
                    ToolResult(
                        title="Container Error",
                        output=f"The sandbox container is not available: {e}. The container will be recreated automatically on the next attempt.",
                        metadata={
                            "error": True,
                            "failure_code": "sandbox_transport_error",
                            "container_error": True,
                        },
                    ),
                    terminal_event="error",
                    terminal_error=f"Container unavailable: {e}",
                )

            return ToolDispatchOutcome(
                ToolResult(
                    title=f"Error in {tool_id}",
                    output=str(e),
                    metadata={"error": True, "failure_code": "tool_exception"},
                ),
                terminal_event="error",
                terminal_error=str(e),
            )
        finally:
            self._cleanup_prepared(prepared)

        if result.metadata.get("error"):
            failure_code = str(result.metadata.get("failure_code") or "tool_reported_error")
            result.metadata.setdefault("failure_code", failure_code)
            return ToolDispatchOutcome(
                result,
                terminal_event="error",
                terminal_error=f"{failure_code}: {result.output}",
            )
        return ToolDispatchOutcome(result, terminal_event="completed")

    async def abandon_execute(self, prepared: PreparedToolExecution) -> None:
        """Release a prepared call whose body was cancelled before dispatch."""
        self._cleanup_prepared(prepared)

    async def timeout_execute(
        self,
        prepared: PreparedToolExecution,
        timeout_seconds: float,
    ) -> ToolDispatchOutcome:
        """Close a scheduler-cancelled body as an ordered terminal error.

        ``dispatch_execute`` normally reaches its ``finally`` block before the
        scheduler invokes this method.  Cleanup remains explicit and
        idempotent here so a custom dispatch implementation cannot leave
        permission or output callbacks installed after a timeout.
        """
        self._cleanup_prepared(prepared)
        duration = f"{timeout_seconds:g}"
        message = (
            f"Tool execution exceeded {duration} seconds and was cancelled."
        )
        return ToolDispatchOutcome(
            ToolResult(
                title=f"{prepared.tool_id} timed out",
                output=message,
                metadata={
                    "error": True,
                    "failure_code": "tool_timeout",
                },
            ),
            terminal_event="error",
            terminal_error=message,
        )

    async def finalize_execute(
        self,
        prepared: PreparedToolExecution,
        outcome: ToolDispatchOutcome,
    ) -> ToolResult:
        """Commit context and terminal SSE for one model-order result slot."""
        await prepared.run_ctx.assert_run_current()
        self._merge_context(prepared)
        result = outcome.result
        if outcome.terminal_event == "error":
            bus.publish(TOOL_ERROR, _generation_payload(prepared.run_ctx, {
                "userId": self.user_id,
                "sessionId": self.session_id,
                "partId": prepared.part_id,
                "error": outcome.terminal_error or result.output,
            }))
            return result

        if outcome.terminal_event != "completed":
            return result

        duration = time.time() - prepared.start_time
        bus.publish(TOOL_COMPLETED, _generation_payload(prepared.run_ctx, {
            "userId": self.user_id,
            "sessionId": self.session_id,
            "partId": prepared.part_id,
            "output": result.output[:2000] if result.output else "",
            "title": result.title,
        }))

        result.metadata["duration"] = duration
        return result

    @staticmethod
    def _cleanup_prepared(prepared: PreparedToolExecution) -> None:
        if prepared.cleaned or prepared.blocked_result is not None:
            return
        prepared.cleaned = True
        ctx = prepared.run_ctx
        ctx._on_output = prepared.previous_output_callback
        ctx._authorize_tool = prepared.previous_authorize_callback
        ctx._nested_tool_runtime = prepared.previous_nested_runtime
        ctx._authorized_tool_id = prepared.previous_authorized_id
        ctx._authorized_tool_args_key = prepared.previous_authorized_args

    @staticmethod
    def _merge_context(prepared: PreparedToolExecution) -> None:
        """Apply call-local mutations at the ordered result cursor."""
        shared = prepared.shared_ctx
        local = prepared.run_ctx
        shared.part_id = prepared.part_id
        if not prepared.isolated:
            return

        if hasattr(shared, "_capability_search_calls"):
            delta = int(getattr(local, "_capability_search_calls", 0)) - prepared.baseline_search_calls
            shared._capability_search_calls += max(0, delta)
        if hasattr(shared, "_capability_result_chars"):
            delta = int(getattr(local, "_capability_result_chars", 0)) - prepared.baseline_result_chars
            shared._capability_result_chars += max(0, delta)
        if hasattr(shared, "_capability_revealed_ids"):
            local_ids = set(getattr(local, "_capability_revealed_ids", set()))
            shared._capability_revealed_ids.update(
                local_ids.difference(prepared.baseline_revealed_ids)
            )

    async def authorize_tool(
        self,
        tool_id: str,
        args: dict,
        *,
        ctx: ToolContext | None = None,
    ) -> ToolResult | None:
        """Apply doom-loop and permission policy to direct and nested calls."""
        try:
            checks = self._permission_checks(tool_id, args)
        except ValueError as exc:
            return ToolResult(
                title="Invalid tool input",
                output=f"The {tool_id} call was blocked because its targets could not be validated: {exc}",
                metadata={"blocked": True, "invalid_input": True},
            )

        if ctx is not None and tool_id in {
            "read", "write", "edit", "multiedit", "apply_patch", "grep", "glob"
        }:
            try:
                canonical_patterns = await self._canonical_permission_patterns(
                    tool_id, args, ctx
                )
                checks = self._permission_checks(
                    tool_id,
                    args,
                    additional_targets=canonical_patterns,
                )
            except ValueError as exc:
                return ToolResult(
                    title="Invalid tool input",
                    output=f"The {tool_id} call was blocked because its targets could not be validated: {exc}",
                    metadata={"blocked": True, "invalid_input": True},
                )
            except Exception as exc:
                log.warning(
                    "Canonical permission target resolution failed tool=%s error_type=%s",
                    tool_id,
                    type(exc).__name__,
                )
                return ToolResult(
                    title="Permission validation unavailable",
                    output=(
                        f"The {tool_id} call was blocked because its canonical "
                        "filesystem targets could not be resolved safely."
                    ),
                    metadata={
                        "blocked": True,
                        "canonical_validation": True,
                    },
                )

        merged_rules = (
            self.config_rules + self.agent_rules
            if self.agent_rules
            else self.config_rules
        )

        # Deployment policy is a non-bypassable guard. Agent rules and
        # persisted user approvals are deliberately evaluated later and cannot
        # loosen an effective configured deny. Preflight every permission plane
        # and every target before publishing any prompt, so a later denied file
        # cannot be hidden behind an earlier ask in a multi-file operation.
        for permission_name, patterns in checks:
            for pattern in patterns:
                guard = perm_mod.evaluate_guard(
                    permission_name, pattern, self.guard_rules
                )
                inherited_guards = [
                    perm_mod.evaluate_guard(permission_name, pattern, ruleset)
                    for ruleset in self.authority_guard_planes
                ]
                if guard.action == "deny" or any(
                    item.action == "deny" for item in inherited_guards
                ):
                    return ToolResult(
                        title="Permission denied",
                        output=f"Permission denied for tool '{tool_id}'. This tool is restricted by platform policy.",
                        metadata={"blocked": True},
                    )
                trusted = perm_mod.evaluate(
                    permission_name, pattern, merged_rules
                )
                inherited = [
                    perm_mod.evaluate(permission_name, pattern, ruleset)
                    for ruleset in self.authority_rule_planes
                ]
                if trusted.action == "deny" or any(
                    item.action == "deny" for item in inherited
                ):
                    return ToolResult(
                        title="Permission denied",
                        output=f"Permission denied for tool '{tool_id}'. This tool is restricted in the current agent mode.",
                        metadata={"blocked": True},
                    )

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
                    authority_rulesets=self.authority_rule_planes,
                    authority_guard_rulesets=self.authority_guard_planes,
                    is_doom_loop=True,
                    user_id=self.user_id,
                )
            except (perm_mod.PermissionDeniedError, perm_mod.PermissionRejectedError):
                return ToolResult(
                    title="Doom loop detected",
                    output=f"The same tool call ({tool_id}) was repeated {DOOM_LOOP_THRESHOLD} times with identical arguments. Execution was blocked.",
                    metadata={"blocked": True, "doom_loop": True},
                )

        # Evaluate the ordered trusted rules (defaults/config first, then the
        # current Agent's refinements). Read-like search tools retain their own
        # policy and additionally pass the read policy; neither plane can loosen
        # the other. An "always" reply remains only a user preference for asks.
        always_patterns = ["*"]

        try:
            for permission_name, patterns in checks:
                await perm_mod.ask(
                    session_id=self.session_id,
                    permission=permission_name,
                    patterns=patterns,
                    input_data=args,
                    config_rules=merged_rules,
                    guard_rules=self.guard_rules,
                    authority_rulesets=self.authority_rule_planes,
                    authority_guard_rulesets=self.authority_guard_planes,
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

    def _permission_checks(
        self,
        tool_id: str,
        args: dict,
        *,
        additional_targets: list[str] | None = None,
    ) -> list[tuple[str, list[str]]]:
        """Return every permission plane that must approve this invocation."""
        targets = self._with_sensitive_casefold(
            [
                *self._extract_patterns(tool_id, args),
                *(additional_targets or []),
            ]
        )
        if tool_id in perm_mod.EDIT_TOOLS:
            return [("edit", targets)]
        if tool_id == "grep":
            return [
                ("grep", [str(args.get("pattern") or "")]),
                ("read", targets),
            ]
        if tool_id == "glob":
            return [
                ("glob", [str(args.get("pattern") or "")]),
                ("read", targets),
            ]
        return [(tool_id, targets)]

    async def _canonical_permission_patterns(
        self,
        tool_id: str,
        args: dict,
        ctx: ToolContext,
    ) -> list[str]:
        """Resolve static aliases and project canonical targets into policy.

        This is an execution-plane snapshot, not a path lock. The Action Server
        still owns confinement at execution time, while this preflight makes a
        pre-existing symlink unable to hide a target from permission rules.
        """
        from sandbox.client import PathResolveTarget

        sandbox = getattr(ctx, "sandbox", None)
        resolver = getattr(sandbox, "resolve_paths", None)
        if not callable(resolver):
            raise RuntimeError("sandbox does not support canonical path resolution")

        targets: list[PathResolveTarget]
        if tool_id in {"read", "write", "edit", "multiedit"}:
            execution_path = ctx.resolve_file_path(
                str(args.get("file_path") or ""),
                allow_user_scope=(tool_id == "read"),
            )
            targets = [PathResolveTarget(
                path=execution_path,
                allow_missing=(tool_id == "write"),
                allow_scoped_skills=(tool_id == "read"),
            )]
        elif tool_id == "apply_patch":
            from tool.apply_patch import parse_patch

            targets = [
                PathResolveTarget(
                    path=ctx.resolve_file_path(operation.path),
                    allow_missing=(operation.type in {"add", "delete"}),
                )
                for operation in parse_patch(args.get("patch", ""))
            ]
        elif tool_id in {"grep", "glob"}:
            execution_path = ctx.resolve_file_path(
                str(args.get("path") or "/workspace"),
                allow_user_scope=True,
            )
            targets = [PathResolveTarget(
                path=execution_path,
                allow_scoped_skills=True,
            )]
        else:
            return []

        resolved = await resolver(targets)
        if len(resolved) != len(targets):
            raise RuntimeError("sandbox returned incomplete canonical targets")

        if tool_id == "glob":
            selector = str(args.get("pattern") or "").lstrip("/")
            return [
                self._join_permission_path(root, selector)
                for root in self._resolved_path_variants(resolved[0])
            ]
        if tool_id == "grep":
            patterns: list[str] = []
            file_type = str(args.get("type") or "").lstrip(".")
            for root in self._resolved_path_variants(resolved[0]):
                patterns.extend([
                    root,
                    self._join_permission_path(root, "**"),
                ])
                if file_type:
                    patterns.append(
                        self._join_permission_path(root, f"**/*.{file_type}")
                    )
            return patterns

        patterns = []
        for item in resolved:
            patterns.extend(self._resolved_path_variants(item))
        return patterns

    def _resolved_path_variants(self, resolved: Any) -> list[str]:
        """Return canonical absolute, workspace-relative and workdir-relative forms."""
        canonical = str(getattr(resolved, "canonical_path", "") or "")
        workspace_relative = getattr(resolved, "workspace_relative", None)
        variants = [canonical]
        if isinstance(workspace_relative, str) and workspace_relative:
            variants.append(workspace_relative)

        workdir = str(self.workdir or "/workspace").rstrip("/") or "/"
        if canonical == workdir:
            variants.append(".")
        elif canonical.startswith(f"{workdir.rstrip('/')}/"):
            variants.append(canonical[len(workdir.rstrip("/")) + 1:])

        return list(dict.fromkeys(value for value in variants if value))

    @staticmethod
    def _join_permission_path(root: str, suffix: str) -> str:
        if not suffix:
            return root
        if root in {"", "."}:
            return suffix
        if root == "/":
            return f"/{suffix}"
        return f"{root.rstrip('/')}/{suffix}"

    @staticmethod
    def _with_sensitive_casefold(patterns: list[str]) -> list[str]:
        """Retain exact subjects and add a canonical secret-policy projection."""
        from tool.sensitive_paths import casefold_sensitive_subject

        expanded: list[str] = []
        for raw in patterns:
            subject = str(raw or "")
            if subject not in expanded:
                expanded.append(subject)
            canonical = casefold_sensitive_subject(subject)
            if canonical is not None and canonical not in expanded:
                expanded.append(canonical)
        return expanded

    def _effective_search_path(self, args: dict) -> str:
        """Mirror grep/glob's runtime substitution before policy evaluation."""
        path = str(args.get("path") or "/workspace")
        if path == "/workspace":
            path = str(getattr(self, "workdir", "/workspace") or "/workspace")
        return path.rstrip("/") or "/"

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
        elif tool_id in ("read", "write", "edit", "multiedit"):
            return [args.get("file_path", "")]
        elif tool_id == "apply_patch":
            from tool.apply_patch import parse_patch

            return [operation.path for operation in parse_patch(args.get("patch", ""))]
        elif tool_id == "glob":
            path = self._effective_search_path(args)
            pattern = str(args.get("pattern") or "").lstrip("/")
            # Authorize the object names the call can reveal, not merely the
            # glob expression detached from its search root.
            return [f"{path}/{pattern}"]
        elif tool_id == "grep":
            path = self._effective_search_path(args)
            patterns = [path, f"{path.rstrip('/')}/**"]
            file_type = str(args.get("type") or "").lstrip(".")
            if file_type:
                patterns.append(f"{path.rstrip('/')}/**/*.{file_type}")
            return patterns
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
