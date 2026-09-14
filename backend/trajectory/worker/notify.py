"""``trajectory.available`` publication after trace commits (SPEC §8.7).

The in-process bus reaches the admin WebSocket of this worker; when the process
initialized the Redis bus (``REDIS_URL``) the same call broadcasts on the bus
channel so other worker replicas fan out. Notifications carry only a watermark:
a lost one is recovered by HTTP catch-up, so publishing never raises.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping

from core.log import create_logger

log = create_logger("trajectory.worker.notify")

EVENT_TYPE = "trajectory.available"


class _WorkerBusNoise(logging.Filter):
    """The bus warns on every publish without catch-all subscribers, which is
    the normal state of a worker process (its WebSocket subscribes by type)."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not (message.startswith(f"publish({EVENT_TYPE})") and "0 subscribers" in message)


_filter_installed = False


def _install_filter() -> None:
    global _filter_installed
    if not _filter_installed:
        logging.getLogger("openbox.bus").addFilter(_WorkerBusNoise())
        _filter_installed = True


def available_payload(trajectory, *, deleted: bool = False) -> dict:
    """Exactly ``user_id, owner_user_id, session_id, trajectory_id, committed_seq`` (+ ``deleted``)."""
    if isinstance(trajectory, Mapping):
        get = trajectory.get
    else:
        def get(key, default=None):
            return getattr(trajectory, key, default)
    payload = {
        "user_id": get("user_id"),
        "owner_user_id": get("user_id"),
        "session_id": get("session_id"),
        "trajectory_id": get("trajectory_id") or get("id"),
        "committed_seq": str(get("committed_seq") or 0),
    }
    if deleted:
        payload["deleted"] = True
    return payload


def publish_available(trajectory, *, deleted: bool = False) -> None:
    """Publish one watermark notification; failures are logged, never raised."""
    try:
        payload = available_payload(trajectory, deleted=deleted)
        _install_filter()
        from bus.bus import publish
        publish(EVENT_TYPE, payload)
    except Exception as exc:
        log.warning("Trajectory notification failed error_type=%s", type(exc).__name__)
