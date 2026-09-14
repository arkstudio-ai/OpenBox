"""``trajectory.available`` publication after trace commits (SPEC §8.7, WAVE3 shared contract 3).

The in-process bus reaches the admin WebSocket of this process. When the
process opened the trajectory hint channel (``bus.trajectory_hints``, with
``REDIS_URL``), the same call publishes on ``trajectory:hints`` so other worker
replicas fan out; hints never travel on the business bus channel.
Notifications carry only a watermark: a lost one is recovered by HTTP
catch-up, so publishing never raises.
"""
from __future__ import annotations

from collections.abc import Mapping

from core.log import create_logger

log = create_logger("trajectory.worker.notify")

EVENT_TYPE = "trajectory.available"


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
        from bus.trajectory_hints import publish_trajectory_hint
        publish_trajectory_hint(payload)
    except Exception as exc:
        log.warning("Trajectory notification failed error_type=%s", type(exc).__name__)
