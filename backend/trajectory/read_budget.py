"""Per-request decoded-content budget; background recording has no read budget."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading


class ReadTooLarge(Exception):
    pass


class ReadBudget:
    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    @property
    def remaining(self) -> int:
        with self._lock:
            return self.limit - self.used

    def consume(self, size: int) -> None:
        # Downloads decode in worker threads; all share this request's budget.
        with self._lock:
            if size > self.limit - self.used:
                raise ReadTooLarge("Trajectory result is too large; use a smaller page or download individual content")
            self.used += size


_current: ContextVar[ReadBudget | None] = ContextVar("trajectory_read_budget", default=None)


def current_read_budget() -> ReadBudget | None:
    return _current.get()


@contextmanager
def read_budget(limit: int):
    budget = ReadBudget(limit)
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)
