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
from core.log import create_logger
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from session.agent_event_log import append_agent_event_locked, prepare_agent_event_write

CODE = "ASSISTANT_TURN_BUDGET"
log = create_logger("assistant.budget")
PUBLIC_MESSAGE = "个人助理本轮已达到处理上限，已停止。已完成的操作仍然保留；请先查看相关任务状态，再继续沟通。"
MESSAGES = {
    "time": "个人助理本轮回复超时，已停止。已完成的操作仍然保留；请先查看相关任务状态，再继续沟通。",
    "request": "个人助理本轮达到模型请求次数上限，已停止。已完成的操作仍然保留；请先查看相关任务状态，再继续沟通。",
    "tool": "个人助理本轮达到工具调用次数上限，已停止。已完成的操作仍然保留；请先查看相关任务状态，再继续沟通。",
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
    slowest_step_seconds: float = 0
    receipt_reconciled: bool = False
    exhausted_by: str = ""

    def exhaust(self, reason):
        if self.error is None:
            self.error = {"code": CODE, "message": MESSAGES[reason]}
            self.exhausted_by = reason
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

    def needs_reply(self, step):
        # Reserve a reply before starting another tool/model round. Slow
        # providers need more than the old fixed 30-second final allowance.
        reserve = max(min(30, self.limits["wall_time_seconds"] / 4),
                      min(self.limits["wall_time_seconds"] * .65, self.slowest_step_seconds * 2 + 5))
        return (step >= self.limits["model_requests"] - 1
                or self.local_deadline - time.monotonic() <= reserve)

    async def reconcile_error(self):
        if not self.error or self.receipt_reconciled:
            return
        from assistant.receipts import describe, for_run
        self.receipt_reconciled = True
        try:
            async with asyncio.timeout(3):
                receipts = await for_run(self.lease.session_id, self.lease.user_id,
                                         self.lease.run_id, self.lease.generation)
        except Exception as exc:
            # Missing proof must never prevent settlement of the original
            # failure, or be treated as proof of acceptance.
            log.warning("budget receipt unread run=%s error=%s",
                        self.lease.run_id, type(exc).__name__)
            return
        if receipts:
            reason = {"time": "回复超时", "request": "达到模型请求次数上限",
                      "tool": "达到工具调用次数上限"}.get(self.exhausted_by, "达到处理上限")
            self.error = {"code": CODE, "message": (
                f"个人助理本轮{reason}，已停止；已提交的任务指令不会因此撤销。"
                + describe(receipts) + "请先查看原任务状态，避免重复提交。")}

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
