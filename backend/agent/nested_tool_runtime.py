"""The single lifecycle gateway for tool calls nested inside another tool.

Top-level model calls are scheduled by :mod:`agent.processor`.  Composite
tools (currently ``batch``) used to jump directly from the registry to
``ToolInfo.execute``.  That bypassed durable ToolParts, ordered permission
preparation, request tracing, terminal events, and generation-fenced commits.

``NestedToolRuntime`` reuses the exact staged :class:`ToolHooks` contract.  It
prepares calls in request order, overlaps only explicitly parallel-safe bodies,
and commits their independent ToolParts in request order.  The outer composite
tool remains one provider result while every nested side effect has its own
durable identity and recovery state.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Sequence

from models.message import ToolPartData, ToolStatus
from tool.tool import ToolContext, ToolResult


_NESTED_BINDING_DIGEST = hashlib.sha256(
    b"openbox:nested-tool-runtime:v1"
).hexdigest()


@dataclass
class _NestedCall:
    index: int
    tool_id: str
    args: dict[str, Any]
    part: ToolPartData
    hook_prepared: Any = None
    direct_result: ToolResult | None = None
    hook_outcome: Any = None
    persisted: bool = False
    committed: bool = False
    parallel_safe: bool = False


def _call_id(ctx: ToolContext, index: int, tool_id: str) -> str:
    # The parent call/slot owns identity across Driver recovery. A changed
    # tool or arguments at the same slot must conflict, not create a new call.
    seed = f"{ctx.session_id}\0{ctx.message_id}\0{ctx.part_id}\0{index}"
    return f"nested_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:48]}"


def _blocked(title: str, output: str, code: str) -> ToolResult:
    return ToolResult(
        title=title,
        output=output,
        metadata={"error": True, "blocked": True, "failure_code": code},
    )


class NestedToolRuntime:
    """One call-local nested scheduler bound to an existing ``ToolHooks``."""

    def __init__(self, hooks: Any, ctx: ToolContext):
        self.hooks = hooks
        self.ctx = ctx

    async def _save(self, call: _NestedCall, *, is_new: bool) -> None:
        from session.session import save_part

        await save_part(
            call.part,
            is_new=is_new,
            user_id=self.ctx.user_id or "default",
            run_fence=self.ctx.run_fence,
        )

    async def _prepare(self, call: _NestedCall) -> None:
        await self.ctx.assert_run_current()
        from agent.effect_ledger import EffectConflictError, request_hash
        from db.base import get_db_session
        from db.models.part import Part
        from session.tool_part_identity import tool_part_identity_values
        async with get_db_session() as db:
            previous = await db.get(Part, call.part.id)
            if previous is not None:
                if (previous.user_id != self.ctx.user_id or previous.session_id != self.ctx.session_id
                        or previous.message_id != self.ctx.message_id or previous.type != "tool"
                        or request_hash(previous.data.get("input")) != request_hash(call.args)
                        or previous.data.get("call_id") != call.part.call_id
                        or any(getattr(previous, key) != value
                            for key, value in tool_part_identity_values(call.part).items())):
                    raise EffectConflictError("Nested call conflicts with its original slot")
                if previous.data.get("status") in {ToolStatus.COMPLETED, ToolStatus.ERROR}:
                    call.direct_result = ToolResult(title=previous.data.get("title") or "",
                        output=previous.data.get("output") or previous.data.get("error") or "",
                        metadata={**(previous.data.get("metadata") or {}),
                            "error": previous.data.get("status") == ToolStatus.ERROR})
                    call.persisted = call.committed = True
                    return
        await self._save(call, is_new=previous is None)
        call.persisted = True

        if call.tool_id == "batch":
            call.direct_result = _blocked(
                "Recursive batch blocked",
                "Cannot recursively call the batch tool.",
                "nested_batch_recursion",
            )
            return
        from memory.transient_tools import memory_operation
        if memory_operation(call.tool_id, {"input": call.args}):
            call.direct_result = _blocked(
                "Nested source read unavailable",
                "Call memory and task reads directly so their temporary results retain authorization and version checks.",
                "nested_source_read",
            )
            return
        if (
            self.ctx.available_tools is not None
            and call.tool_id not in self.ctx.available_tools
        ):
            call.direct_result = _blocked(
                "Nested tool unavailable",
                f"Tool '{call.tool_id}' is not available to the current agent.",
                "nested_tool_unavailable",
            )
            return

        execution_lookup = getattr(self.ctx, "_tool_execution_lookup", None)
        if execution_lookup is not None:
            tool = execution_lookup.get(call.tool_id)
        else:
            # Compatibility for direct unit callers which do not enter the
            # provider-step runtime. Production always supplies the exact
            # step-local lookup above.
            from tool.registry import get_tool

            tool = get_tool(call.tool_id)
        if tool is None:
            call.direct_result = _blocked(
                "Nested tool not found",
                f"Tool '{call.tool_id}' was not found.",
                "nested_tool_not_found",
            )
            return
        if tool.parallel_safe is False:
            guidance = (
                " Use computer(action='batch', actions=[...]) for ordered desktop actions."
                if call.tool_id == "computer" else ""
            )
            call.direct_result = _blocked(
                "Unsafe parallel tool blocked",
                f"Tool '{call.tool_id}' is not safe for parallel execution.{guidance}",
                "nested_tool_not_parallel_safe",
            )
            return
        call.parallel_safe = tool.parallel_safe is True

        call.hook_prepared = await self.hooks.prepare_execute(
            call.tool_id,
            tool.execute,
            call.args,
            self.ctx,
            part_id=call.part.id,
            isolate_context=True,
        )
        if call.hook_prepared.blocked_result is not None:
            call.direct_result = call.hook_prepared.blocked_result

    async def _dispatch(self, call: _NestedCall) -> _NestedCall:
        if call.direct_result is None:
            call.hook_outcome = await self.hooks.dispatch_execute(call.hook_prepared)
        return call

    async def _commit(self, call: _NestedCall) -> ToolResult:
        await self.ctx.assert_run_current()
        if call.committed:
            return call.direct_result
        result = call.direct_result
        if call.hook_prepared is not None:
            if result is not None:
                # Permission/policy rejection still receives the same terminal
                # event and cleanup boundary as an executor failure.
                from agent.hooks import ToolDispatchOutcome

                hook_outcome = ToolDispatchOutcome(
                    result,
                    terminal_event="error",
                    terminal_error=result.output,
                )
                result = await self.hooks.finalize_execute(
                    call.hook_prepared, hook_outcome,
                )
            else:
                result = await self.hooks.finalize_execute(
                    call.hook_prepared, call.hook_outcome,
                )
        if result is None:  # defensive: every prepared slot must terminate
            result = ToolResult(
                title=f"Error in {call.tool_id}",
                output="Nested tool produced no terminal result.",
                metadata={"error": True, "failure_code": "nested_missing_result"},
            )

        from agent.processor import persisted_tool_metadata

        failed = bool(result.metadata.get("error") or result.metadata.get("blocked"))
        call.part.status = ToolStatus.ERROR if failed else ToolStatus.COMPLETED
        call.part.output = result.output
        call.part.title = result.title
        call.part.error = result.output if failed else None
        call.part.metadata = persisted_tool_metadata(result.metadata)
        await self._save(call, is_new=False)
        call.committed = True
        return result

    async def _close_canceled(self, calls: Sequence[_NestedCall]) -> None:
        for call in calls:
            if call.hook_prepared is not None:
                await self.hooks.abandon_execute(call.hook_prepared)
            if call.committed:
                continue
            if not call.persisted:
                continue
            call.part.status = ToolStatus.ERROR
            call.part.title = "Nested tool canceled"
            call.part.error = "Nested tool execution was canceled before commit."
            call.part.metadata = {
                "blocked": True,
                "failure_code": "nested_tool_canceled",
            }
            try:
                await self._save(call, is_new=False)
            except Exception:
                # A stale generation cannot close its Part; run-scoped tail
                # repair owns that exact recovery transition.
                pass

    async def execute_batch(
        self,
        invocations: Sequence[tuple[str, dict[str, Any]]],
    ) -> list[ToolResult]:
        """Prepare in order, run safe bodies concurrently, commit in order."""
        calls: list[_NestedCall] = []
        tasks: list[asyncio.Task[_NestedCall]] = []
        try:
            for index, (tool_id, args) in enumerate(invocations):
                await self.ctx.assert_run_current()
                call = _NestedCall(
                    index=index,
                    tool_id=tool_id,
                    args=dict(args),
                    part=ToolPartData(
                        id="part_" + _call_id(self.ctx, index, tool_id).removeprefix("nested_"),
                        tool=tool_id,
                        status=ToolStatus.RUNNING,
                        input=dict(args),
                        call_id=_call_id(self.ctx, index, tool_id),
                        canonical_tool_id=tool_id,
                        wire_tool_name=tool_id,
                        provider_binding_digest=_NESTED_BINDING_DIGEST,
                        provider_dialect="nested",
                        stream_seq=index,
                        session_id=self.ctx.session_id,
                        message_id=self.ctx.message_id,
                    ),
                )
                calls.append(call)
                await self._prepare(call)
            pending: list[asyncio.Task[_NestedCall]] = []
            dispatched: list[_NestedCall] = []
            for call in calls:
                if call.parallel_safe:
                    task = asyncio.create_task(self._dispatch(call), name=f"nested-tool:{call.part.id}")
                    tasks.append(task)
                    pending.append(task)
                else:
                    if pending:
                        dispatched.extend(await asyncio.gather(*pending))
                        pending.clear()
                    dispatched.append(await self._dispatch(call))
            if pending:
                dispatched.extend(await asyncio.gather(*pending))
            results: list[ToolResult] = []
            for call in dispatched:
                results.append(await self._commit(call))
            return results
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._close_canceled(calls)
            raise
