"""Stop a run whose model keeps receiving a prompt it has already been sent.

Every step appends at least a tool call and its result, so the provider's
input token count grows from one step to the next — or drops, after
compaction or pruning. A run whose counts keep landing on values it has
already sent is rebuilding its request from a frozen slice of the
conversation: the model cannot see what it just did or what the user just
answered, and every further step re-decides the same thing at full price.

On 2026-09-14 a session crossed the 200-message page `get_messages` used to
return and spent 140 steps that way, re-asking an answered question six
times. This guard would have stopped it about four minutes in.
"""
from collections import deque

from sqlalchemy import select

#: How many recent steps a repeated input count is compared against. The
#: frozen prompt alternated between two sizes, so an exact-previous-step
#: comparison would keep resetting.
STALL_WINDOW = 10
#: Consecutive repeated steps before the run is stopped. Real steps always add
#: bytes, so even two in a row is suspicious; eight leaves room for coincidence.
STALL_STEPS = 8

#: Shown by clients that have no copy for CONTEXT_STALLED yet; matches the
#: zh-CN entry in the web and mobile errors.json.
CONTEXT_STALLED_MESSAGE = "这轮对话的上下文没有更新，已自动停止以免重复扣费。请重新发送消息继续，如仍出现请联系我们。"


class ContextStallDetector:
    """Counts consecutive steps whose input size the run has recently sent."""

    def __init__(self, window: int = STALL_WINDOW, threshold: int = STALL_STEPS):
        self.threshold = threshold
        self.repeats = 0
        self._recent: deque[int] = deque(maxlen=window)

    @property
    def window(self) -> int:
        return self._recent.maxlen or 0

    def observe(self, input_tokens: int | None) -> bool:
        """Record one finished step; True once the run has stalled.

        A step with no reported usage is no evidence either way: it neither
        extends nor breaks the streak.
        """
        if not input_tokens or input_tokens < 0:
            return False
        self.repeats = self.repeats + 1 if input_tokens in self._recent else 0
        self._recent.append(input_tokens)
        return self.repeats >= self.threshold


async def recent_step_input_tokens(session_id: str, limit: int = STALL_WINDOW) -> list[int]:
    """Input token counts of the session's latest finished steps, oldest first.

    A run seeds its detector from these, so a stall that began before a
    question suspended the previous run is still recognised when the answer
    resumes it — each resume is a new run with its own step counter.
    """
    from db.base import get_db_session
    from db.models.part import Part

    async with get_db_session() as db:
        rows = (await db.scalars(
            select(Part.data)
            .where(Part.session_id == session_id, Part.type == "step-finish")
            .order_by(Part.created_at.desc(), Part.id.desc())
            .limit(limit)
        )).all()
    return [int((data or {}).get("input_tokens") or 0) for data in reversed(rows)]
