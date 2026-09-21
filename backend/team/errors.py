"""Stable errors shared by HTTP, model tools and recovery."""
from __future__ import annotations

from typing import Any


class TeamError(ValueError):
    def __init__(self, code: str, message: str, *, current: Any = None, status: int = 409):
        super().__init__(message)
        self.code = code
        self.current = current
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.current is not None:
            result["current"] = self.current
        return result


class TeamExecutionPaused(TeamError):
    """Execution stopped by an already persisted team control or account billing state.

    This is not a failed provider response. The normal interrupted-step path
    closes the transcript; the team's durable state supplies recovery actions.
    """
