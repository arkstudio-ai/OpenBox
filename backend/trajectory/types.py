"""Versioned event validation and canonical wire encoding."""
from datetime import datetime, timezone
import hashlib
import json
import math
from uuid import uuid4

from trajectory.context import TraceContext

VERSION = 1
PROJECTOR_VERSION = 1
FAMILIES = {
    "trajectory": {"started"}, "baseline": {"captured"},
    "input": {"accepted", "injected"}, "session": {"settings_changed"},
    "history": {"reverted", "regenerated", "forked"},
    "turn": {"started", "finished"}, "run": {"started", "finished", "cancel_requested", "interrupted"},
    "step": {"started", "finished"},
    "request": {"prepared", "started", "delta", "usage", "finished", "retry_scheduled", "route_changed"},
    "tool": {"requested", "started", "output", "finished"},
    "permission": {"requested", "resolved", "expired"},
    "question": {"asked", "draft_saved", "resolved", "cancelled"},
    "agent": {"spawned", "message", "finished"},
    "compaction": {"started", "finished"}, "context": {"replaced", "injected"},
    "message": {"committed"}, "part": {"committed"},
    "plan": {"changed"}, "todo": {"changed"}, "skill": {"loaded"}, "tool_catalog": {"changed"},
    "job": {"submitted", "progress", "finished"}, "artifact": {"recorded", "removed"},
    "takeover": {"requested", "started", "finished"}, "recording": {"gap"},
    "operation": {"late_result"},
}
EVENT_TYPES = {f"{family}.{name}" for family, names in FAMILIES.items() for name in names}
ID_FIELDS = tuple(key for key in TraceContext.__dataclass_fields__ if key not in {"user_id", "session_id", "workspace_id"})


class TrajectoryError(ValueError):
    code = "trajectory_invalid"


class OwnershipError(TrajectoryError):
    code = "trajectory_ownership"


class IdempotencyConflict(TrajectoryError):
    code = "trajectory_idempotency_conflict"


class CorruptContent(TrajectoryError):
    code = "trajectory_corrupt"


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def sequence(value: str | int, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not str(value).isdigit():
        raise TrajectoryError("Sequence must be a nonnegative decimal integer")
    parsed = int(value)
    if parsed > 9223372036854775807 or (maximum is not None and parsed > maximum):
        raise TrajectoryError("Sequence exceeds committed watermark")
    return parsed


def prepare(context: TraceContext, event: dict) -> dict:
    event_type = event.get("type")
    if event_type not in EVENT_TYPES or event.get("version", VERSION) != VERSION:
        raise TrajectoryError("Unsupported trajectory event type or version")
    data = event.get("data", {})
    if not isinstance(data, dict):
        raise TrajectoryError("Event data must be an object")
    # Reject unserializable objects/non-finite floats before any write.
    canonical(data)
    result = context.to_dict()
    for key in ID_FIELDS:
        if key in event:
            result[key] = event[key]
    result.update(type=event_type, version=VERSION, event_id=event.get("event_id") or f"evt_{uuid4().hex}",
                  occurred_at=iso(event.get("occurred_at") or now()), data=data)
    family = event_type.split(".")[0]
    required = {"request": "request_id", "tool": "call_id", "run": "run_id", "turn": "turn_id", "step": "step_id", "agent": "agent_id"}.get(family)
    if required and not result.get(required):
        raise TrajectoryError(f"{event_type} requires {required}")
    for key in ("user_id", "session_id", "event_id", *ID_FIELDS):
        if key == "generation" or result.get(key) is None:
            continue
        if not isinstance(result[key], str) or not result[key] or len(result[key]) > 128:
            raise TrajectoryError(f"Invalid event identity: {key}")
    if event_type in {"request.delta", "tool.output"}:
        chunk_index = data.get("chunk_index")
        if chunk_index is not None and (isinstance(chunk_index, bool) or not isinstance(chunk_index, int) or chunk_index < 0):
            raise TrajectoryError("chunk_index must be nonnegative integer")
        if data.get("mode", "delta") not in {"delta", "replace"}:
            raise TrajectoryError("Unknown stream update mode")
        if "blocks" in data and not isinstance(data["blocks"], list):
            raise TrajectoryError("Stream blocks must be an array")
    if event_type == "request.usage" and data.get("mode", "replace") not in {"replace", "delta"}:
        raise TrajectoryError("Unknown usage update mode")
    for field in ("duration_ms", "elapsed_ms", "ttft_ms", "generation_ms"):
        if field in data and data[field] is not None:
            number = data[field]
            if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or number < 0:
                raise TrajectoryError(f"Invalid timing: {field}")
    return result


class RecordingError(TrajectoryError):
    code = "trajectory_recording_failed"


def recording_boundary(function):
    from functools import wraps
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except TrajectoryError:
            raise
        except Exception as exc:
            # Type-only message: database URLs and provider bodies can contain
            # secrets. The chained exception is for internal diagnostics.
            raise RecordingError(f"Trajectory persistence failed: {type(exc).__name__}") from exc
    return wrapped
