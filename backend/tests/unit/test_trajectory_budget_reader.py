"""Producer-side budget file reader and SPEC §5.7 filtering."""
import os
import time

import pytest

from trajectory import spool
from trajectory.budget import (BLOCKED, DEGRADED, DROP_BUDGET, DROP_DEGRADED, NORMAL, BudgetReader,
                               filter_event)
from trajectory.context import TraceContext


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def entry(level, reason="trajectory_bytes"):
    return {"level": level, "reason": reason, "since": "2026-09-14T08:00:00Z"}


def write(root, *, sessions=None, users=None):
    spool.write_budgets(root, spool.budgets_document(sessions=sessions, users=users))


def test_missing_file_means_no_limits(tmp_path):
    reader = BudgetReader(spool.budgets_path(tmp_path), 5000)
    assert reader.maybe_refresh() is False
    assert reader.level("user", "root") == NORMAL


def test_reload_happens_on_file_change_at_most_once_per_refresh_interval(tmp_path):
    clock = Clock()
    reader = BudgetReader(spool.budgets_path(tmp_path), 5000, clock=clock)
    write(tmp_path, sessions={"root": entry(DEGRADED)})
    assert reader.maybe_refresh() is True
    assert reader.level("user", "root") == DEGRADED
    write(tmp_path, sessions={"root": entry(BLOCKED)})
    clock.now += 4.999
    assert reader.maybe_refresh() is False
    assert reader.level("user", "root") == DEGRADED
    clock.now += 0.001
    assert reader.maybe_refresh() is True
    assert reader.level("user", "root") == BLOCKED
    reloads = reader.reloads
    clock.now += 5
    assert reader.maybe_refresh() is False
    assert reader.reloads == reloads
    write(tmp_path, sessions={})
    assert reader.maybe_refresh() is False
    assert reader.maybe_refresh(force=True) is True
    assert reader.level("user", "root") == NORMAL


def test_rewrite_with_same_size_and_mtime_is_still_detected(tmp_path):
    reader = BudgetReader(spool.budgets_path(tmp_path), 5000)
    write(tmp_path, sessions={"root": {"level": DEGRADED, "reason": "a"}})
    first = os.stat(spool.budgets_path(tmp_path))
    assert reader.maybe_refresh(force=True) and reader.level("u", "root") == DEGRADED
    write(tmp_path, sessions={"root": {"level": BLOCKED, "reason": "ab"}})
    path = spool.budgets_path(tmp_path)
    os.utime(path, ns=(first.st_atime_ns, first.st_mtime_ns))
    assert os.stat(path).st_size == first.st_size and os.stat(path).st_mtime_ns == first.st_mtime_ns
    assert reader.maybe_refresh(force=True) is True
    assert reader.level("u", "root") == BLOCKED


def test_invalid_or_removed_file_clears_limits(tmp_path):
    reader = BudgetReader(spool.budgets_path(tmp_path), 1)
    write(tmp_path, users={"user": entry(BLOCKED, "user_daily_bytes")})
    assert reader.maybe_refresh(force=True) and reader.level("user", "any") == BLOCKED
    spool.budgets_path(tmp_path).write_bytes(b'{"version": 1, "sessions": [')
    assert reader.maybe_refresh(force=True) is True
    assert reader.level("user", "any") == NORMAL and reader.errors == 1
    write(tmp_path, users={"user": entry(DEGRADED, "user_daily_bytes")})
    assert reader.maybe_refresh(force=True) and reader.level("user", "any") == DEGRADED
    os.unlink(spool.budgets_path(tmp_path))
    assert reader.maybe_refresh(force=True) is True
    assert reader.level("user", "any") == NORMAL


def test_level_is_the_stricter_of_root_session_and_user(tmp_path):
    reader = BudgetReader(spool.budgets_path(tmp_path), 5000)
    write(tmp_path, sessions={"root": entry(DEGRADED)}, users={"heavy": entry(BLOCKED, "user_daily_bytes")})
    reader.maybe_refresh(force=True)
    assert reader.level("heavy", "root") == BLOCKED
    assert reader.level("other", "root") == DEGRADED
    assert reader.level("heavy", "elsewhere") == BLOCKED
    assert reader.level("other", "elsewhere") == NORMAL


def test_normal_level_passes_everything_unchanged():
    data = {"output": "x" * 20000, "stage": "executor_stream"}
    for event_type in ("request.delta", "tool.output", "request.prepared", "message.committed"):
        verdict, kept = filter_event(NORMAL, event_type, data)
        assert verdict is None and kept is data


def test_degraded_drops_intermediate_deltas_but_keeps_the_final_chunk():
    for data in ({"chunk_index": 1}, {"mode": "delta", "blocks": []}, {"mode": "delta", "final": 1},
                 {"mode": "delta", "final": False}):
        assert filter_event(DEGRADED, "request.delta", data)[0] == DROP_DEGRADED
    for data in ({"mode": "delta", "final": True}, {"final": True}, {"mode": "replace", "blocks": []}):
        verdict, kept = filter_event(DEGRADED, "request.delta", data)
        assert verdict is None and kept is data


def test_degraded_drops_streamed_tool_output_and_truncates_tool_outputs():
    assert filter_event(DEGRADED, "tool.output", {"output": "x", "stage": "executor_stream"})[0] == DROP_DEGRADED
    assert filter_event(DEGRADED, "tool.output", {"output": "x"})[0] == DROP_DEGRADED
    small = {"output": "x" * 8192, "stage": "executor_result"}
    assert filter_event(DEGRADED, "tool.output", small) == (None, small)
    large = {"output": "y" * 9000, "stage": "executor_result", "title": "t"}
    verdict, kept = filter_event(DEGRADED, "tool.output", large)
    assert verdict is None
    assert kept == {"output": "y" * 8192, "stage": "executor_result", "title": "t", "truncated": True,
                    "original_bytes": 9000}
    assert large["output"] == "y" * 9000 and "truncated" not in large
    wide = {"output": "界" * 3000}
    verdict, kept = filter_event(DEGRADED, "tool.finished", wide)
    assert verdict is None and kept["original_bytes"] == 9000 and kept["truncated"] is True
    assert kept["output"] == "界" * 2730 and len(kept["output"].encode()) <= 8192
    for data in ({"output": {"nested": "x" * 9000}}, {"model_output": "x" * 9000}):
        assert filter_event(DEGRADED, "tool.finished", data) == (None, data)
    for event_type in ("request.prepared", "request.started", "message.committed", "part.committed"):
        data = {"output": "x" * 9000}
        assert filter_event(DEGRADED, event_type, data) == (None, data)


LIFECYCLE = ["run.started", "run.finished", "run.cancel_requested", "run.interrupted", "turn.started",
             "turn.finished", "step.started", "step.finished", "tool.requested", "tool.finished",
             "request.started", "request.usage", "request.finished", "question.asked", "question.resolved",
             "permission.requested", "permission.resolved", "job.submitted", "job.progress", "job.finished",
             "recording.gap"]
CONTENT = ["request.prepared", "request.delta", "request.retry_scheduled", "request.route_changed",
           "tool.started", "tool.output", "message.committed", "part.committed", "artifact.recorded",
           "agent.spawned", "baseline.captured", "input.accepted", "history.forked", "compaction.started",
           "skill.loaded", "todo.changed", "session.settings_changed", "operation.late_result"]


@pytest.mark.parametrize("event_type", LIFECYCLE)
def test_blocked_passes_lifecycle_events_and_keeps_the_degraded_tool_output_limit(event_type):
    data = {"output": "x" * 20000, "status": "completed"}
    verdict, kept = filter_event(BLOCKED, event_type, data)
    assert verdict is None
    if event_type.startswith("tool."):
        # Blocked is stricter than degraded, so tool outputs never pass unbounded.
        assert kept == {"output": "x" * 8192, "status": "completed", "truncated": True, "original_bytes": 20000}
        assert data["output"] == "x" * 20000 and "truncated" not in data
    else:
        assert kept is data
    small = {"output": "short", "status": "completed"}
    assert filter_event(BLOCKED, event_type, small) == (None, small)


@pytest.mark.parametrize("event_type", CONTENT)
def test_blocked_drops_everything_else_as_budget_gaps(event_type):
    data = {"final": True, "stage": "executor_result", "mode": "replace"}
    assert filter_event(BLOCKED, event_type, data)[0] == DROP_BUDGET


def test_emit_applies_budget_file_levels_and_reports_blocked_drops(tmp_path, monkeypatch):
    from trajectory.emitter import emit, get_emitter, reset_emitter_for_tests
    root = tmp_path / "spool"
    reset_emitter_for_tests()
    monkeypatch.setenv("TRAJECTORY_SINK", "spool")
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(root))
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    monkeypatch.setenv("TRAJECTORY_BUDGET_REFRESH_MS", "20")
    write(root, sessions={"degraded_root": entry(DEGRADED)})
    try:
        emitter = get_emitter()
        degraded = TraceContext("user", "degraded_root", run_id="run_d", request_id="req_d", call_id="call_d")
        assert emit("request.delta", {"chunk_index": 0, "blocks": []}, context=degraded) is None
        final_id = emit("request.delta", {"chunk_index": 1, "final": True}, context=degraded)
        assert emit("tool.output", {"output": "x", "stage": "executor_stream"}, context=degraded) is None
        result_id = emit("tool.output", {"output": "z" * 10000, "stage": "executor_result"}, context=degraded)
        assert final_id and result_id
        # The writer thread picks up a rewritten file within the refresh interval.
        write(root, sessions={"degraded_root": entry(DEGRADED), "blocked_root": entry(BLOCKED)})
        deadline = time.monotonic() + 5
        while emitter.budgets.level("user", "blocked_root") != BLOCKED and time.monotonic() < deadline:
            time.sleep(0.01)
        blocked = TraceContext("user", "blocked_root", run_id="run_b", request_id="req_b", call_id="call_b")
        started_id = emit("request.started", {"model": "m"}, context=blocked)
        assert started_id
        assert emit("request.prepared", {"input": {"messages": []}}, context=blocked) is None
        finished_id = emit("tool.finished", {"output": "y" * 9000, "status": "completed"}, context=blocked)
        assert finished_id
        assert emitter.flush(5)
        records = [spool.decode_line(line) for path in sorted(emitter.producer_dir.glob("*.jsonl"))
                   for line in path.read_bytes().splitlines()]
    finally:
        reset_emitter_for_tests()
    events = {record["event"]["event_id"]: record["event"] for record in records if record["k"] == "event"}
    assert list(events) == [final_id, result_id, started_id, finished_id]
    assert events[result_id]["data"]["output"] == "z" * 8192
    assert events[result_id]["data"]["truncated"] is True and events[result_id]["data"]["original_bytes"] == 10000
    assert events[finished_id]["data"] == {"output": "y" * 8192, "status": "completed", "truncated": True,
                                           "original_bytes": 9000}
    gaps = [record["control"] for record in records if record["k"] == "control" and record["control"]["type"] == "gap"]
    assert [(gap["reason"], gap["dropped_events"]) for gap in gaps] == [("budget", 1)]
    assert gaps[0]["sessions"] == [{"user_id": "user", "session_id": "blocked_root", "run_ids": ["run_b"],
                                    "request_ids": ["req_b"]}]
    stats = emitter.stats()
    assert stats["filtered_events"] == 2 and stats["truncated_events"] == 2
    assert stats["dropped_by_reason"] == {"budget": 1}
