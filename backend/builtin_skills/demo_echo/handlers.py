"""Demo skill: exercises the whole outcome protocol with zero side effects.

This is the E2E reference for the runtime (rebuild plan Phase 3): a builtin
handler that succeeds immediately, waits on a fake external service, or waits
on a user answer — all through bounded, checkpointed invocations.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from skill_runtime.registry import register_builtin
from skill_runtime.types import Failed, Retry, Succeeded, WaitExternal, WaitUser

SKILL_KEY = "builtin:demo-echo"


async def run(ctx, operation: str, payload: dict, checkpoint: dict):
    if operation == "echo":
        return Succeeded(result={"echo": payload.get("text", "")})

    if operation == "slow_echo":
        if not checkpoint:
            delay = min(int(payload.get("delay_seconds", 1)), 300)
            await ctx.progress({"stage": "submitted"}, phase="waiting_provider")
            return WaitExternal(
                checkpoint={"submitted": True},
                wake_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
                external_handle="demo-task",
            )
        await ctx.progress({"stage": "delivering"}, phase="delivering")
        return Succeeded(result={"echo": payload.get("text", ""), "delayed": True})

    if operation == "ask_then_echo":
        answers = [i for i in ctx.inputs if i.kind == "user_answer"]
        if answers:
            text = str((answers[-1].payload or {}).get("text", ""))
            # All answers visible in this invocation are superseded by the
            # newest one; acknowledge them only after the value is accepted.
            ctx.consume_inputs(answers)
            return Succeeded(result={"echo": text, "answered": True})
        if checkpoint.get("asked"):
            # Woken without an answer (e.g. reconcile) — keep waiting.
            return WaitUser(
                checkpoint=checkpoint,
                prompt="What should I echo?",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        await ctx.progress(phase="waiting_answer")
        return WaitUser(
            checkpoint={"asked": True},
            prompt="What should I echo?",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )

    if operation == "fail_then_succeed":
        attempts = int(checkpoint.get("attempts") or 0) + 1
        if attempts <= int(payload.get("failures", 1)):
            # Checkpointed so the retry is observable as progress, not a loop.
            return Retry(
                checkpoint={"attempts": attempts},
                error_code="demo_transient",
                retry_at=datetime.now(timezone.utc) + timedelta(seconds=int(payload.get("backoff_seconds", 120))),
                error_message=f"demo failure #{attempts}",
            )
        return Succeeded(result={"attempts": attempts, "recovered": True})

    if operation == "slow_step":
        await asyncio.sleep(int(payload.get("sleep_seconds", 30)))
        return Succeeded(result={"slept": True})

    if operation == "park_notice":
        # Prompt-only park: no input schema, so the card must show the notice
        # without an answer box (the handler would ignore free text anyway).
        return WaitUser(
            checkpoint={"parked": True},
            prompt=str(payload.get("notice") or "需要人工核实后再决定继续或取消。"),
            input_schema={},
        )

    return Failed(error_code="unknown_operation", message=f"demo-echo has no operation {operation!r}")


register_builtin(SKILL_KEY, run, handler_version=1)
