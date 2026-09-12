"""Pure v1 projection shared by persistence, historical reads and fixtures."""
from copy import deepcopy
from datetime import datetime
import json
from trajectory.types import EVENT_TYPES, ID_FIELDS, PROJECTOR_VERSION, digest

TERMINAL = {"completed", "failed", "cancelled", "denied", "timed_out", "unknown", "interrupted", "expired"}
ERROR = {"failed", "denied", "timed_out"}


def empty_state() -> dict:
    return {"projector_version": PROJECTOR_VERSION, "through_seq": "0", "records": {},
            "unsupported_events": [], "coverage_start": None}


def _identity(event, field):
    return event.get(field) or event.get("data", {}).get(field)


def targets(event: dict, state: dict | None = None) -> list[tuple[str, str]]:
    family, action = event["type"].split(".", 1)
    data = event.get("data", {})
    identity = None
    kind = family
    if family in {"trajectory", "baseline"}:
        kind, identity = "baseline", event.get("trajectory_id", event["session_id"])
    elif family == "input":
        kind, identity = "user", _identity(event, "message_id") or event["event_id"]
    elif family in {"message", "part"}:
        role = data.get("role") or data.get("message", {}).get("role")
        part = data.get("part", {})
        if family == "part" and part.get("type") not in {None, "text", "reasoning"}:
            return []  # Tool/plan/file projection facts already have typed records.
        kind = "user" if role == "user" else "assistant"
        if kind == "assistant" and family == "message" and data.get("operation") == "created" and not data.get("message", {}).get("finish"):
            return []  # Empty chat container is not a second AI output.
        if kind == "assistant" and state is not None and not _identity(event, "request_id"):
            related = [row for row in state["records"].values() if row["kind"] == "assistant" and row.get("message_id") == _identity(event, "message_id")]
            if related:
                return [(max(related, key=lambda row: int(row["start_seq"]))["record_id"], "assistant")]
        if kind == "assistant" and family == "part" and not _identity(event, "request_id") and not (part.get("text") or part.get("content")):
            return []
        identity = (_identity(event, "request_id") if kind == "assistant" else None) or _identity(event, "message_id") or event["event_id"]
    elif family == "request":
        if action in {"retry_scheduled", "route_changed"}:
            return [(f"retry:{event['event_id']}", "retry")]
        identity = _identity(event, "request_id")
        base = [(f"request:{identity}", "request")]
        if action == "delta":
            base.append((f"assistant:{identity}", "assistant"))
        return base
    elif family == "tool":
        identity = _identity(event, "call_id")
    elif family in {"turn", "run", "step", "agent"}:
        identity = _identity(event, f"{family}_id")
        base = [(f"{family}:{identity}", family)]
        if family == "run" and action in {"interrupted", "cancel_requested"}:
            base.append((f"interrupt:{event['event_id']}", "interrupt"))
        elif family == "run" and action == "started" and data.get("resume_of_run_id"):
            base.append((f"resume:{identity}", "resume"))
        return base
    elif family in {"question", "permission", "job", "artifact", "compaction", "takeover"}:
        identity = _identity(event, f"{family}_id") or data.get("id") or _identity(event, "call_id") or event["event_id"]
    elif family == "session":
        kind = "settings"
    elif family == "recording":
        kind = "gap"
    elif family == "operation":
        kind = "late_result"
    elif family == "context":
        kind = "context"
    identity = identity or event["event_id"]
    return [(f"{kind}:{identity}", kind)]


def _preview(value, limit=240):
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text[:limit]


def _new_record(event, record_id, kind):
    return {"record_id": record_id, "kind": kind, "title": kind, "preview": None,
            "result_preview": None, "status": "pending", "status_reason": None,
            **{key: event.get(key) for key in ID_FIELDS},
            "start_seq": str(event["seq"]), "end_seq": None, "as_of_seq": str(event["seq"]),
            "started_at": None if kind == "tool" else event["occurred_at"], "finished_at": None,
            "duration_ms": None, "timing_source": None, "data": {}, "blocks": [], "usage": {}}


def _blocks(record, data):
    blocks = data.get("blocks")
    if blocks is None:
        blocks = [{"block_id": data.get("block_id", "text:0"), "type": data.get("block_type", "text"), "delta": data.get("delta", "")}]
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        block_id = str(block.get("block_id", f"{block.get('type', 'text')}:{index}"))
        previous = next((item for item in record["blocks"] if item["block_id"] == block_id), None)
        if previous is None:
            previous = {"block_id": block_id, "type": block.get("type", block.get("block_type", "text")), "text": ""}
            record["blocks"].append(previous)
        chunk_index = block.get("chunk_index", data.get("chunk_index"))
        if chunk_index is not None and previous.get("chunk_index", -1) >= chunk_index:
            continue
        delta = block.get("delta", block.get("text", block.get("arguments", "")))
        if delta is None:
            delta = ""
        if isinstance(delta, dict):
            delta = delta.get("text", delta.get("arguments", json.dumps(delta, ensure_ascii=False)))
        if not isinstance(delta, str):
            delta = json.dumps(delta, ensure_ascii=False)
        previous["text"] = delta if block.get("mode", data.get("mode", "delta")) == "replace" else previous.get("text", "") + delta
        for key, value in block.items():
            if key not in {"delta", "text", "arguments", "mode", "chunk_index"}:
                previous[key] = deepcopy(value)
        if chunk_index is not None:
            previous["chunk_index"] = chunk_index
    text = "".join(block.get("text", "") for block in record["blocks"] if block.get("type") in {"text", "output_text", "reasoning", "reasoning_text"})
    record["preview"] = _preview(text) or ("tool calls" if record["blocks"] else None)


def _usage(record, data):
    incoming = data.get("usage", {})
    if not isinstance(incoming, dict):
        return
    if data.get("mode", "replace") == "replace":
        record["usage"] = deepcopy(incoming)
    else:
        for key, value in incoming.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                record["usage"][key] = record["usage"].get(key, 0) + value
            else:
                record["usage"][key] = deepcopy(value)


def _update(record, event):
    data = event.get("data", {})
    family, action = event["type"].split(".", 1)
    record["as_of_seq"] = str(event["seq"])
    for key in ID_FIELDS:
        if event.get(key) is not None:
            record[key] = event[key]
    # Streaming data is held in one stable content block, not as duplicated
    # per-chunk copies in a record's details. Raw chunks remain in the log.
    if event["type"] not in {"request.delta", "tool.output", "request.usage"}:
        record["data"].update(deepcopy(data))
    if family == "part" and isinstance(data.get("part"), dict):
        part_id = event.get("part_id") or data["part"].get("id")
        if part_id:
            record["data"].setdefault("committed_parts", {})[part_id] = deepcopy(data["part"])
    if event["type"] == "trajectory.started":
        record["data"]["coverage_start"] = event["occurred_at"]
    if event["type"] == "tool.requested":
        record["data"]["requested_at"] = event["occurred_at"]
    if event["type"] == "request.delta":
        _blocks(record, data)
        record["status"] = "streaming"
    elif event["type"] == "request.usage":
        _usage(record, data)
    elif event["type"] == "tool.output":
        chunk_index = data.get("chunk_index")
        last_index = record["data"].get("output_chunk_index", -1)
        if chunk_index is None or chunk_index > last_index:
            output = data.get("output", data.get("delta", ""))
            if data.get("mode", "delta") == "replace":
                record["data"]["output"] = deepcopy(output)
            elif isinstance(output, str):
                record["data"]["output"] = str(record["data"].get("output", "")) + output
            else:
                record["data"]["output"] = deepcopy(output)
            if chunk_index is not None:
                record["data"]["output_chunk_index"] = chunk_index
            record["result_preview"] = _preview(record["data"].get("output"))
    elif action in {"started", "spawned"}:
        record["status"] = "running"
        record["started_at"] = event["occurred_at"]
    elif event["type"] == "input.accepted":
        record["status"] = "accepted"
    elif event["type"] == "input.injected":
        record["status"] = "injected"
    elif action in {"requested", "asked", "submitted", "prepared"}:
        record["status"] = "waiting" if family in {"permission", "question"} else "pending"
    elif action in {"finished", "resolved", "cancelled", "expired", "interrupted", "removed"}:
        status = data.get("status") or data.get("outcome") or {"cancelled": "cancelled", "expired": "expired", "interrupted": "interrupted", "removed": "deleted"}.get(action, "completed")
        status = {"success": "completed", "succeeded": "completed", "error": "failed", "timeout": "timed_out", "rejected": "denied"}.get(status, status)
        record["status"] = status
        record["finished_at"] = event["occurred_at"]
        record["end_seq"] = str(event["seq"])
        if "usage" in data:
            _usage(record, data)
    elif family in {"message", "part"}:
        # Compatibility checkpoints are not new execution completion facts.
        # They cannot turn a failed request into a successful AI message.
        if record["kind"] == "user" and record["status"] == "pending":
            record["status"] = "accepted"
        elif data.get("message", {}).get("finish") and record["status"] not in TERMINAL:
            record["status"] = "failed" if data["message"].get("error") else "completed"
            record["end_seq"] = str(event["seq"])
    elif family not in {"request", "tool", "run", "turn", "step", "agent", "question", "permission", "job", "compaction", "takeover"}:
        record["status"] = data.get("status", "completed")
        record["end_seq"] = str(event["seq"])
    if record["kind"] in {"interrupt", "resume", "retry", "gap", "late_result"}:
        record["status"] = data.get("status", "completed")
        record["end_seq"] = str(event["seq"])
    if data.get("error") or data.get("reason"):
        record["status_reason"] = _preview(data.get("reason") or data.get("error"), 500)
    if "duration_ms" in data:
        record["duration_ms"] = data["duration_ms"]
        record["timing_source"] = data.get("timing_source", "producer_monotonic") if data["duration_ms"] is not None else None
    elif record["finished_at"] and record["started_at"] and record["kind"] in {"request", "tool", "run", "turn", "step", "agent", "permission", "question", "job", "compaction", "takeover"} and record["status"] not in {"denied", "unknown"}:
        record["duration_ms"] = max(0, (datetime.fromisoformat(record["finished_at"].replace("Z", "+00:00")) - datetime.fromisoformat(record["started_at"].replace("Z", "+00:00"))).total_seconds() * 1000)
        record["timing_source"] = "session_timestamps"
    if record["kind"] == "tool" and record["started_at"] is None:
        record["duration_ms"] = None
        record["timing_source"] = None
    record["title"] = str(data.get("title") or data.get("name") or data.get("tool") or data.get("tool_name") or data.get("model") or record["title"])
    candidate = _preview(data.get("text") or data.get("content") or data.get("input") or data.get("prompt") or data.get("questions") or data.get("requested_arguments") or data.get("arguments") or data.get("summary"))
    if candidate is not None and (record["preview"] is None or family in {"input", "message", "part"}):
        record["preview"] = candidate
    for key in ("output", "result", "answers", "model_output"):
        if key in data:
            record["result_preview"] = _preview(data[key])
            break


def _system_snapshot(state, event):
    if event["type"] != "request.prepared":
        return None
    actual = event.get("data", {}).get("input")
    if not isinstance(actual, dict):
        return None
    system = actual.get("system", actual.get("instructions"))
    messages = actual.get("messages", actual.get("input"))
    if system is None and isinstance(messages, list):
        system = [message for message in messages if isinstance(message, dict) and message.get("role") in {"system", "developer"}]
    if system is None and "tools" not in actual:
        return None
    snapshot = {"system": deepcopy(system), "tools": deepcopy(actual.get("tools"))}
    previous = [row for row in state["records"].values() if row["kind"] == "system" and
                row.get("agent_id") == event.get("agent_id") and row.get("source_session_id") == event.get("source_session_id")]
    before = max(previous, key=lambda row: int(row["start_seq"])) if previous else None
    if before and {key: before["data"].get(key) for key in snapshot} == snapshot:
        return None
    record = _new_record(event, f"system:{event.get('request_id')}", "system")
    record.update(title="System state" if before is None else "System updated", status="completed", end_seq=str(event["seq"]),
        data={**snapshot, "before": {key: before["data"].get(key) for key in snapshot} if before else None,
              "source_request_id": event.get("request_id"), "capture_level": event["data"].get("capture_level")})
    return record


def reduce(state: dict, event: dict) -> dict:
    """Copy on write: caller's state and events are never mutated."""
    if int(event["seq"]) <= int(state["through_seq"]):
        return state
    result = {**state, "records": dict(state["records"]), "through_seq": str(event["seq"])}
    if event.get("version") != 1 or event.get("type") not in EVENT_TYPES:
        result["unsupported_events"] = [*state.get("unsupported_events", []), {"seq": str(event["seq"]), "type": event.get("type"), "version": event.get("version")}]
        return result
    if event["type"] == "trajectory.started":
        result["coverage_start"] = event["occurred_at"]
    system = _system_snapshot(result, event)
    if system is not None:
        result["records"][system["record_id"]] = system
    for record_id, kind in targets(event, result):
        previous = result["records"].get(record_id)
        record = deepcopy(previous) if previous is not None else _new_record(event, record_id, kind)
        _update(record, event)
        result["records"][record_id] = record
    if event["type"] == "request.finished":
        assistant_id = f"assistant:{event.get('request_id')}"
        if assistant_id in result["records"]:
            record = deepcopy(result["records"][assistant_id])
            _update(record, event)
            result["records"][assistant_id] = record
    if event["type"] in {"run.interrupted", "recording.gap"}:
        for record_id, previous in result["records"].items():
            if previous["kind"] in {"tool", "request", "assistant", "step"} and previous["run_id"] == event.get("run_id") and previous["status"] not in TERMINAL:
                record = deepcopy(previous)
                record.update(status="unknown", status_reason="run_interrupted_without_committed_result", end_seq=str(event["seq"]), as_of_seq=str(event["seq"]))
                result["records"][record_id] = record
    return result


def replay(events: list[dict], state: dict | None = None) -> dict:
    state = state if state is not None else empty_state()
    for event in events:
        state = reduce(state, event)
    return state


def token_value(usage: dict, names: tuple) -> float | int | None:
    for name in names:
        value = usage.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
    return None


def contribution(record: dict | None) -> dict:
    stats = {"request_count": 0, "tool_count": 0, "error_count": 0, "unknown_count": 0,
             "input_tokens": 0, "output_tokens": 0, "usage_missing": 0}
    if record is None or record["kind"] not in {"request", "tool"}:
        return stats
    stats[f"{record['kind']}_count"] = 1
    stats["error_count"] = int(record["status"] in ERROR)
    stats["unknown_count"] = int(record["status"] == "unknown")
    if record["kind"] == "request":
        input_tokens = token_value(record["usage"], ("input_tokens", "prompt_tokens", "input"))
        output_tokens = token_value(record["usage"], ("output_tokens", "completion_tokens", "output"))
        stats.update(input_tokens=input_tokens or 0, output_tokens=output_tokens or 0,
                     usage_missing=int(input_tokens is None or output_tokens is None))
    return stats


def statistics(state: dict) -> dict:
    totals = contribution(None)
    intervals = []
    for record in state["records"].values():
        for key, value in contribution(record).items():
            totals[key] += value
        if record["kind"] == "run" and record.get("finished_at"):
            intervals.append((record["started_at"], record["finished_at"]))
    duration = None
    if intervals:
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        duration = sum(max(0, (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() * 1000) for start, end in merged)
    missing = totals.pop("usage_missing")
    return {**totals, "input_tokens": totals["input_tokens"] if totals["request_count"] > missing else None,
            "output_tokens": totals["output_tokens"] if totals["request_count"] > missing else None,
            "usage_complete": not missing, "duration_ms": duration,
            "through_seq": state["through_seq"], "coverage_start": state["coverage_start"]}


def agents(state: dict) -> list[dict]:
    return [{"agent_id": row["agent_id"], "parent_agent_id": row["parent_agent_id"],
             "source_session_id": row["source_session_id"], "name": row["title"],
             "status": row["status"], "record_id": row["record_id"]}
            for row in state["records"].values() if row["kind"] == "agent"]
