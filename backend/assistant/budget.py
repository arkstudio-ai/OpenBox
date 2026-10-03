"""Durable budgets for one main input, including recovery and compaction.

Admission is conservative: an admitted request counts even if its transport
never starts. A process restart cannot replenish it. The deadline cooperates
with the existing stream/tool abort path, preserving uncertain tool outcomes.
Long execution belongs to the separate Task Session, not this coordination turn.
"""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import time

from sqlalchemy import func, select

from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from session.agent_event_log import append_agent_event_locked, prepare_agent_event_write

CODE = "ASSISTANT_TURN_BUDGET"
PUBLIC_MESSAGE = "本轮助理处理已达到执行上限，已停止。已完成的操作仍然保留；请缩小请求范围后继续。"
MESSAGES = {
    "time": "本轮助理处理已达到时间上限，已停止。已完成的操作仍然保留；请缩小请求范围后继续。",
    "request": "本轮助理处理已达到模型请求上限，已停止。已完成的操作仍然保留；请缩小请求范围后继续。",
    "tool": "本轮助理处理已达到工具调用上限，已停止。已完成的操作仍然保留；请缩小请求范围后继续。",
}
current: ContextVar["TurnBudget | None"] = ContextVar("assistant_turn_budget", default=None)


class AssistantBudgetExceeded(Exception):
    pass


@dataclass
class TurnBudget:
    lease: object
    turn_id: str
    mode: str
    deadline: float
    local_deadline: float
    limits: dict
    error: dict | None = None
    watcher: asyncio.Task | None = field(default=None, repr=False)

    def exhaust(self, reason):
        if self.error is None:
            self.error = {"code": CODE, "message": MESSAGES[reason]}
        self.lease.abort.set()
        return AssistantBudgetExceeded(self.error["message"])

    def check(self):
        if self.error:
            raise AssistantBudgetExceeded(self.error["message"])
        if time.monotonic() >= self.local_deadline:
            raise self.exhaust("time")

    async def watch(self):
        try:
            await asyncio.wait_for(self.lease.abort.wait(), max(0, self.local_deadline - time.monotonic()))
        except TimeoutError:
            self.exhaust("time")

    async def close(self):
        if self.watcher is not None:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
            self.watcher = None

    async def admit(self, kind, identity):
        self.check()
        lease = self.lease
        fence = (lease.session_id, lease.run_id, lease.generation)
        from agent.inbox import _database_utcnow
        async with get_db_session() as db:
            main = await prepare_agent_event_write(db, session_id=lease.session_id,
                user_id=lease.user_id, run_fence=fence)
            if (await _database_utcnow(db)).timestamp() >= self.deadline:
                raise self.exhaust("time")
            event_kind = f"assistant.budget.{kind}"
            conditions = (AgentEvent.session_id == main.id, AgentEvent.user_id == main.user_id,
                          AgentEvent.turn_id == self.turn_id, AgentEvent.kind == event_kind)
            if await db.scalar(select(AgentEvent.id).where(*conditions, AgentEvent.step_id == identity)):
                return
            used = int(await db.scalar(select(func.count()).select_from(AgentEvent).where(*conditions)))
            maximum = self.limits["model_requests" if kind == "request" else "tool_calls"]
            if used >= maximum:
                raise self.exhaust(kind)
            await append_agent_event_locked(db, main, kind=event_kind, run_fence=fence,
                turn_id=self.turn_id, step_id=identity, payload={"ordinal": used + 1},
                idempotency_key=f"assistant-budget:{self.turn_id}:{kind}:{identity}")


async def start(lease):
    """Freeze policy on first execution of the exact claimed logical input."""
    from agent.inbox import _database_utcnow
    from core.config import get_config
    fence = (lease.session_id, lease.run_id, lease.generation)
    async with get_db_session() as db:
        main = await prepare_agent_event_write(db, session_id=lease.session_id, user_id=lease.user_id, run_fence=fence)
        rows = list((await db.scalars(select(AgentInboxItem).where(
            AgentInboxItem.session_id == main.id, AgentInboxItem.user_id == main.user_id,
            AgentInboxItem.run_id == lease.run_id, AgentInboxItem.generation == lease.generation,
            AgentInboxItem.state == "claimed"))).all())
        if main.kind != "assistant" or len(rows) != 1 or not rows[0].message_id:
            raise AssistantError(409, "ASSISTANT_BUDGET_INPUT", "A single claimed assistant input is required")
        item = rows[0]
        mode = "report_only" if item.origin == "task_result" else "ordinary"
        existing = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.user_id == main.user_id, AgentEvent.turn_id == item.message_id,
            AgentEvent.kind == "assistant.budget.started").order_by(AgentEvent.sequence).limit(1))
        now = (await _database_utcnow(db)).timestamp()
        if existing is None:
            limits = getattr(get_config().assistant, mode).model_dump()
            payload = {"inbox_id": item.id, "mode": mode, "deadline": now + limits["wall_time_seconds"], "limits": limits}
            await append_agent_event_locked(db, main, kind="assistant.budget.started", payload=payload,
                run_fence=fence, turn_id=item.message_id, idempotency_key=f"assistant-budget:{item.id}")
        else:
            payload = existing.payload
            if payload["inbox_id"] != item.id or payload["mode"] != mode:
                raise AssistantError(409, "ASSISTANT_BUDGET_INPUT", "Assistant budget binding changed")
        budget = TurnBudget(lease, item.message_id, mode, payload["deadline"],
            time.monotonic() + max(0, payload["deadline"] - now), payload["limits"])
    budget.watcher = asyncio.create_task(budget.watch())
    return budget


async def admit_tool(ctx, part_id):
    budget = current.get()
    if budget is not None and ctx.session_id == budget.lease.session_id:
        if not part_id:
            raise AssistantError(409, "ASSISTANT_BUDGET_TOOL", "A persisted tool identity is required")
        if ctx.run_fence != (budget.lease.session_id, budget.lease.run_id, budget.lease.generation):
            raise AssistantError(409, "ASSISTANT_BUDGET_RUN", "Assistant tool run changed")
        await budget.admit("tool", part_id)


async def run_tool_body(ctx, execute, args):
    """Stop a stalled body at the absolute turn deadline, then drain cleanup.

    The dispatcher records a started body's outcome as unknown. Cancellation
    cannot undo a committed effect or prove that an external request failed.
    """
    budget = current.get()
    if budget is None or ctx.session_id != budget.lease.session_id:
        return await execute(args, ctx)
    budget.check()
    scope = asyncio.timeout(max(0, budget.local_deadline - time.monotonic()))
    try:
        async with scope:
            return await execute(args, ctx)
    except TimeoutError:
        if scope.expired():
            raise budget.exhaust("time") from None
        raise


def check_tool(ctx):
    budget = current.get()
    if budget is not None and ctx.session_id == budget.lease.session_id:
        budget.check()
