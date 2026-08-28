"""Demo skill: exercises the whole outcome protocol with zero side effects.

This is the E2E reference for the runtime (rebuild plan Phase 3): a builtin
handler that succeeds immediately, waits on a fake external service, or waits
on a user answer — all through bounded, checkpointed invocations.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from skill_runtime.registry import register_builtin
from skill_runtime.types import Failed, Succeeded, WaitExternal, WaitUser

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
            return Succeeded(result={"echo": text, "answered": True})
        if checkpoint.get("asked"):
            # Woken without an answer (e.g. reconcile) — keep waiting.
            return WaitUser(checkpoint=checkpoint, prompt="What should I echo?")
        await ctx.progress(phase="waiting_answer")
        return WaitUser(
            checkpoint={"asked": True},
            prompt="What should I echo?",
            input_schema={"type": "object", "required": ["text"]},
        )

    return Failed(error_code="unknown_operation", message=f"demo-echo has no operation {operation!r}")


register_builtin(SKILL_KEY, run, handler_version=1)
