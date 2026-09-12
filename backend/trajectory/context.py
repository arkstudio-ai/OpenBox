"""Immutable execution ownership; task-local state never changes the viewer."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from typing import Iterator


@dataclass(frozen=True, slots=True)
class TraceContext:
    user_id: str
    session_id: str
    source_session_id: str | None = None
    workspace_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    generation: int | None = None
    agent_id: str | None = None
    parent_agent_id: str | None = None
    step_id: str | None = None
    request_id: str | None = None
    call_id: str | None = None
    parent_call_id: str | None = None
    message_id: str | None = None
    part_id: str | None = None
    caused_by_event_id: str | None = None

    def __post_init__(self):
        if not self.user_id or not self.session_id:
            raise ValueError("Trajectory owner and session are required")
        if self.source_session_id is None:
            object.__setattr__(self, "source_session_id", self.session_id)

    def derive(self, **changes) -> "TraceContext":
        return replace(self, **changes)

    def to_dict(self) -> dict:
        return {key: value for key, value in asdict(self).items() if value is not None}

    @classmethod
    def from_dict(cls, value: dict) -> "TraceContext":
        return cls(**value)


_context: ContextVar[TraceContext | None] = ContextVar("trajectory_context", default=None)


def current() -> TraceContext | None:
    return _context.get()


@contextmanager
def bind(context: TraceContext | None) -> Iterator[TraceContext | None]:
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)
