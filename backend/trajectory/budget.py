"""Producer-side view of ``control/budgets.json`` (SPEC §5.7).

The emitter's writer thread refreshes the cached levels; ``emit()`` only
reads them, so business paths never touch the file system for budgets.
A missing or invalid file means no limits.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from trajectory.spool import SpoolFormatError, parse_budgets

log = logging.getLogger(__name__)

NORMAL, DEGRADED, BLOCKED = "normal", "degraded", "blocked"
_RANK = {NORMAL: 0, DEGRADED: 1, BLOCKED: 2}
DEGRADED_OUTPUT_BYTES = 8 * 1024
BLOCKED_FAMILIES = frozenset({"run", "turn", "step", "question", "permission", "job"})
BLOCKED_TYPES = frozenset({"tool.requested", "tool.finished", "request.started", "request.usage",
                           "request.finished", "recording.gap"})

# Filter verdicts. ``DROP_BUDGET`` is reported through gap controls;
# degraded drops are the intended reduced detail and are only counted.
DROP_BUDGET = "budget"
DROP_DEGRADED = "degraded"


class BudgetReader:
    def __init__(self, path: Path, refresh_ms: int, *, clock=time.monotonic):
        self.path = Path(path)
        self.refresh_seconds = max(1, int(refresh_ms)) / 1000
        self._clock = clock
        self._lock = threading.Lock()
        self._next_check = float("-inf")
        self._identity = None
        # One tuple so readers never see sessions and users from different files.
        self._levels: tuple[dict[str, str], dict[str, str]] = ({}, {})
        self.reloads = 0
        self.errors = 0

    def maybe_refresh(self, *, force: bool = False) -> bool:
        """Reload when the file identity changed; checked at most once per interval."""
        if not force and self._clock() < self._next_check:
            return False
        with self._lock:
            self._next_check = self._clock() + self.refresh_seconds
            try:
                with open(self.path, "rb") as handle:
                    status = os.fstat(handle.fileno())
                    # A rename-replaced file has a new inode even within one mtime tick.
                    identity = (status.st_ino, status.st_mtime_ns, status.st_size)
                    if identity == self._identity:
                        return False
                    raw = handle.read()
            except FileNotFoundError:
                return self._install(None, ({}, {}))
            except OSError as exc:
                self.errors += 1
                log.warning("Trajectory budget file unreadable error_type=%s", type(exc).__name__)
                return self._install(None, ({}, {}))
            try:
                levels = parse_budgets(raw)
            except SpoolFormatError as exc:
                self.errors += 1
                if identity != self._identity:
                    log.warning("Trajectory budget file ignored: %s", exc)
                levels = ({}, {})
            return self._install(identity, levels)

    def _install(self, identity, levels) -> bool:
        changed = identity != self._identity or levels != self._levels
        self._identity = identity
        self._levels = levels
        if changed:
            self.reloads += 1
        return changed

    def level(self, user_id: str | None, session_id: str | None) -> str:
        sessions, users = self._levels
        if not sessions and not users:
            return NORMAL
        by_session = sessions.get(session_id, NORMAL)
        by_user = users.get(user_id, NORMAL)
        return by_session if _RANK[by_session] >= _RANK[by_user] else by_user


def lifecycle(event_type) -> bool:
    if not isinstance(event_type, str):
        return False
    return event_type in BLOCKED_TYPES or event_type.split(".", 1)[0] in BLOCKED_FAMILIES


def filter_event(level: str, event_type, data) -> tuple[str | None, object]:
    """``(verdict, data)``: verdict ``None`` keeps the (possibly truncated) data."""
    if level == NORMAL:
        return None, data
    if level == BLOCKED:
        return (None if lifecycle(event_type) else DROP_BUDGET), data
    if not isinstance(data, dict) or not isinstance(event_type, str):
        return None, data
    if event_type == "request.delta":
        # finish() marks the last chunk final; it carries the settled output.
        if data.get("mode", "delta") == "delta" and data.get("final") is not True:
            return DROP_DEGRADED, data
        return None, data
    if event_type.startswith("tool."):
        if event_type == "tool.output" and data.get("stage") != "executor_result":
            return DROP_DEGRADED, data
        return None, truncate_output(data)
    return None, data


def truncate_output(data: dict) -> dict:
    output = data.get("output")
    # Four UTF-8 bytes per code point at most: shorter strings always fit.
    if not isinstance(output, str) or len(output) * 4 <= DEGRADED_OUTPUT_BYTES:
        return data
    encoded = output.encode("utf-8", "surrogatepass")
    if len(encoded) <= DEGRADED_OUTPUT_BYTES:
        return data
    kept = encoded[:DEGRADED_OUTPUT_BYTES].decode("utf-8", "ignore")
    return {**data, "output": kept, "truncated": True, "original_bytes": len(encoded)}
