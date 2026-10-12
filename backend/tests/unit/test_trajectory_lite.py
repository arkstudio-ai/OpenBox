"""Lite admin view (docs/SESSION_TRAJECTORY_LITE_PLAN.md): turn summaries, their reads and the label dictionary."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from tests.unit.test_worker_projection_support import (AT, FIXTURES, Ingest, Metrics, add_meta, add_trajectory,  # noqa: F401
    blobs, load_fixture, project_all, settings, trace_db)
from trajectory import lite, turns
from trajectory.ops import turns_backfill
from trajectory.store.database import trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryRecord, TrajectoryTurnSummary
from trajectory.types import TrajectoryError, iso
from trajectory.worker.projection import ProjectionService

SESSION_FIXTURE = next(path for path in FIXTURES if path.stem == "session_v1")
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def _record(record_id, kind, seq, *, status="completed", turn_id="turn_x", title=None, started=None, finished=None,
            end_seq=None, preview=None, reason=None, usage=None, message_id=None, agent_id="agent_main"):
    started_at = iso(AT + timedelta(seconds=started if started is not None else seq))
    return {"record_id": record_id, "kind": kind, "title": title or kind, "preview": preview, "result_preview": None,
            "status": status, "status_reason": reason, "turn_id": turn_id, "run_id": "run_x", "agent_id": agent_id,
            "message_id": message_id, "start_seq": str(seq), "end_seq": str(end_seq) if end_seq else None,
            "as_of_seq": str(end_seq or seq), "started_at": started_at,
            "finished_at": iso(AT + timedelta(seconds=finished)) if finished is not None else None,
            "duration_ms": None, "timing_source": None, "usage": usage or {}}


def _base(**run):
    return [_record("user:m1", "user", 1, status="accepted", preview="发一条视频", message_id="m1"),
            _record("turn:turn_x", "turn", 2, status="running"),
            _record("run:run_x", "run", 3, status=run.get("status", "running"), end_seq=run.get("end_seq"),
                    finished=run.get("finished"), reason=run.get("reason"))]


# -- Rules (§6.2) --

def test_waiting_wins_over_every_other_fact():
    records = _base(status="waiting", end_seq=9, finished=9) + [
        _record("question:q1", "question", 8, status="waiting"),
        _record("tool:c1", "tool", 5, status="failed", title="desktop_publish", reason="login_expired")]
    derived = turns.derive(records)
    assert (derived["status"], derived["waiting_kind"]) == ("waiting", "question")
    assert derived["finished_at"] is None and derived["duration_ms"] is None
    # A run that finished waiting without a still-open question is waiting on the (recorded) question kind.
    assert turns.derive(_base(status="waiting", end_seq=9, finished=9))["waiting_kind"] == "question"


def test_failed_run_names_its_reason_and_falls_back_to_the_failed_step():
    with_reason = turns.derive(_base(status="failed", end_seq=9, finished=9, reason="model refused"))
    assert (with_reason["status"], with_reason["status_reason"]) == ("failed", "model refused")
    without = turns.derive(_base(status="failed", end_seq=9, finished=9) + [
        _record("tool:c1", "tool", 5, status="failed", title="desktop_publish", reason="login_expired", finished=6)])
    assert without["status_reason"] == "login_expired"
    assistant_failed = turns.derive(_base(status="completed", end_seq=9, finished=9) + [
        _record("assistant:r1", "assistant", 4, status="failed", reason="context overflow")])
    assert (assistant_failed["status"], assistant_failed["status_reason"]) == ("failed", "context overflow")


def test_cancelled_interrupted_and_resumed_turns():
    assert turns.derive(_base(status="interrupted", end_seq=9, finished=9))["status"] == "cancelled"
    interrupted = _base() + [_record("interrupt:e1", "interrupt", 6)]
    assert turns.derive(interrupted)["status"] == "cancelled"
    resumed = interrupted + [_record("resume:run_y", "resume", 7), _record("run:run_y", "run", 8, status="running")]
    assert turns.derive(resumed)["status"] == "running"


def test_finished_turns_split_on_failed_steps_and_count_tools():
    clean = turns.derive(_base(status="completed", end_seq=9, finished=9) + [
        _record("tool:c1", "tool", 5, title="video_generate", finished=6),
        _record("tool:c2", "tool", 6, title="video_generate", finished=7),
        _record("assistant:r1", "assistant", 7, preview="做好了", message_id="m2")])
    assert clean["status"] == "completed" and clean["status_reason"] is None
    assert clean["tool_counts"] == {"video_generate": {"total": 2, "failed": 0}} and clean["tool_failures"] == 0
    assert (clean["user_preview"], clean["assistant_preview"]) == ("发一条视频", "做好了")
    assert (clean["user_message_id"], clean["assistant_message_id"]) == ("m1", "m2")
    assert clean["duration_ms"] == 8000 and clean["first_seq"] == 1 and clean["last_seq"] == 9
    errors = turns.derive(_base(status="completed", end_seq=9, finished=9) + [
        _record("tool:c1", "tool", 5, title="hot_trends", status="failed", reason="upstream 502", finished=6),
        _record("job:j1", "job", 6, status="timed_out")])
    assert (errors["status"], errors["status_reason"]) == ("completed_with_errors", "upstream 502")
    assert errors["tool_failures"] == 2 and errors["job_count"] == 1
    # A turn record that finished closes the turn even when its run has no end (cron sessions never emit turn.finished,
    # ordinary sessions may lose the run.finished to a gap).
    closed_turn = _base() + [_record("turn:turn_x", "turn", 2, end_seq=9, finished=9)]
    assert turns.derive([row for row in closed_turn if row["record_id"] != "turn:turn_x" or row["end_seq"]])["status"] == "completed"


def test_running_turns_age_into_unknown_for_readers():
    derived = turns.derive(_base())
    assert derived["status"] == "running" and derived["last_activity_at"] == AT + timedelta(seconds=3)
    assert turns.effective_status("running", derived["last_activity_at"], at=derived["last_activity_at"] + timedelta(minutes=29), grace=30) == "running"
    assert turns.effective_status("running", derived["last_activity_at"], at=derived["last_activity_at"] + timedelta(minutes=31), grace=30) == "unknown"
    assert turns.effective_status("completed", None, at=NOW) == "completed"
    assert turns.derive([]) is None and turns.derive([_record("system:s", "system", 1, turn_id=None)]) is None


def test_usage_is_summed_only_when_known():
    rows = _base(status="completed", end_seq=9, finished=9) + [
        _record("request:r1", "request", 4, usage={"input_tokens": 40, "output_tokens": 8}),
        _record("request:r2", "request", 5)]
    derived = turns.derive(rows)
    assert (derived["request_count"], derived["input_tokens"], derived["output_tokens"], derived["usage_complete"]) == (2, 40, 8, False)
    unknown = turns.derive(_base(status="completed", end_seq=9, finished=9) + [_record("request:r2", "request", 5)])
    assert (unknown["input_tokens"], unknown["output_tokens"]) == (None, None)


# -- Projection and backfill --

async def _project_fixture(blobs, *, max_events=None):
    fixture = load_fixture(SESSION_FIXTURE)
    events = [{**event, "occurred_at": iso(event["occurred_at"])} for event in fixture["events"]]
    first = events[0]
    await add_meta(first["session_id"], first["user_id"], title="日志排查")
    await add_trajectory(first["trajectory_id"], first["session_id"], first["user_id"])
    await Ingest(blobs).append(first["trajectory_id"], events)
    service = ProjectionService(settings(), blob_store=blobs, metrics=Metrics())
    assert await project_all(service, first["trajectory_id"], max_events=max_events) == len(events)
    return first["trajectory_id"], first["session_id"], first["user_id"]


async def _turn_rows(trajectory_id):
    async with trace_session() as db:
        return (await db.scalars(select(TrajectoryTurnSummary).where(TrajectoryTurnSummary.trajectory_id == trajectory_id))).all()


def _assert_fixture_turn(row, trajectory_id):
    assert (row.trajectory_id, row.turn_id, row.status, row.waiting_kind) == (trajectory_id, "turn_a", "completed", None)
    assert (row.user_preview, row.assistant_preview) == ("检查日志", "完成")
    assert row.tool_counts == {"read": {"total": 1, "failed": 0}} and row.tool_failures == 0
    assert (row.request_count, row.input_tokens, row.output_tokens, row.usage_complete) == (3, 45, 10, False)
    assert (row.agent_count, row.retry_count, row.job_count, row.artifact_count) == (1, 1, 0, 0)
    assert row.user_message_id == "msg_user" and row.main_agent_id == "agent_a"
    assert row.finished_at is not None and row.duration_ms is not None and row.duration_ms > 0
    assert (row.first_seq, row.last_seq) == (1, 34)


@pytest.mark.parametrize("max_events", [None, 3], ids=["one batch", "small batches"])
async def test_projection_maintains_turn_summaries_and_the_turn_column(trace_db, blobs, max_events):
    trajectory_id, _, user_id = await _project_fixture(blobs, max_events=max_events)
    rows = await _turn_rows(trajectory_id)
    assert len(rows) == 1
    _assert_fixture_turn(rows[0], trajectory_id)
    assert rows[0].user_id == user_id and rows[0].applied_seq == 34
    async with trace_session() as db:
        columns = (await db.execute(select(TrajectoryRecord.record_id, TrajectoryRecord.turn_id).where(
            TrajectoryRecord.trajectory_id == trajectory_id))).all()
    assert columns and all(turn_id == "turn_a" for _, turn_id in columns)


async def test_backfill_fills_the_column_and_rebuilds_the_rows(trace_db, blobs):
    trajectory_id, _, _ = await _project_fixture(blobs)
    async with trace_session() as db:
        await db.execute(update(TrajectoryRecord).where(TrajectoryRecord.trajectory_id == trajectory_id).values(turn_id=None))
        await db.execute(TrajectoryTurnSummary.__table__.delete())
    assert await _turn_rows(trajectory_id) == []
    result = await turns_backfill.backfill_trajectory(trajectory_id, chunk=4)
    assert result == {"trajectory_id": trajectory_id, "skipped": False, "filled": 18, "turns": 1}
    rows = await _turn_rows(trajectory_id)
    _assert_fixture_turn(rows[0], trajectory_id)
    # Rerun: nothing left to fill, the row is recomputed identically.
    assert await turns_backfill.backfill_trajectory(trajectory_id) == {"trajectory_id": trajectory_id, "skipped": False,
                                                                       "filled": 0, "turns": 1}
    assert await turns_backfill.run([trajectory_id], None, 500) == 0


async def test_get_turn_classifies_the_steps(trace_db, blobs):
    trajectory_id, session_id, _ = await _project_fixture(blobs)
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, trajectory_id)
        result = await lite.get_turn(db, trajectory, "turn_a", at=NOW)
        with pytest.raises(LookupError):
            await lite.get_turn(db, trajectory, "turn_missing", at=NOW)
    assert result["summary"]["session_title"] == "日志排查" and result["summary"]["status"] == "completed"
    assert result["through_seq"] == "34" and result["labels_version"] == 1
    by_id = {step["record_id"]: step for step in result["steps"]}
    assert [step["record_id"] for step in result["steps"]] == sorted(by_id, key=lambda key: (int(by_id[key]["start_seq"]), key))
    assert by_id["user:msg_user"]["step_type"] == "user_input"
    tool = by_id["tool:call_a"]
    assert (tool["step_type"], tool["label"], tool["group"], tool["collapsible"]) == ("tool", "处理文件", "files", True)
    assert by_id["request:req_a"]["step_type"] == "hidden" and by_id["step:step_a"]["step_type"] == "hidden"
    assert by_id["agent:agent_child"]["step_type"] == "agent" and by_id["retry:evt_fixture_28"]["step_type"] == "retry"
    assert "data" not in tool and "blocks" not in tool


# -- Reads over seeded summaries --

async def _seed_turn(trajectory_id, session_id, user_id, turn_id, *, started, status="completed", preview=None,
                     assistant=None, tokens=(10, 2), workspace_id="ws_a", last_activity=None):
    moment = NOW - timedelta(hours=started)
    async with trace_session() as db:
        if await db.get(SessionTrajectory, trajectory_id) is None:
            db.add(SessionTrajectory(id=trajectory_id, user_id=user_id, session_id=session_id, workspace_id=workspace_id,
                                     started_at=moment, updated_at=moment, last_activity_at=moment))
            await db.flush()
        db.add(TrajectoryTurnSummary(trajectory_id=trajectory_id, turn_id=turn_id, session_id=session_id, user_id=user_id,
            workspace_id=workspace_id, started_at=moment, finished_at=moment if status not in {"running", "waiting"} else None,
            last_activity_at=last_activity or moment, duration_ms=1000, status=status, status_reason=None,
            waiting_kind="question" if status == "waiting" else None, user_preview=preview, assistant_preview=assistant,
            tool_counts={}, request_count=1, input_tokens=tokens[0], output_tokens=tokens[1], usage_complete=True,
            first_seq=1, last_seq=5, applied_seq=5))


async def _seed_users():
    await add_meta("s_a1", "user_a", username="张店长", email="zhang@example.test", title="国庆视频")
    await add_meta("s_a2", "user_a", title="日常运营", user=False)
    await add_meta("s_b1", "user_b", username="老王", email="wang@example.test")
    await add_meta("s_c1", "user_c", username="旧用户")
    await _seed_turn("t_a1", "s_a1", "user_a", "a1", started=1, status="failed", preview="发视频", assistant="登录过期")
    await _seed_turn("t_a1", "s_a1", "user_a", "a2", started=2, status="waiting", preview="扫好了")
    await _seed_turn("t_a2", "s_a2", "user_a", "a3", started=30, status="completed", preview="看热点", tokens=(5, 1))
    await _seed_turn("t_b1", "s_b1", "user_b", "b1", started=3, status="running", preview="写文案",
                     last_activity=NOW - timedelta(minutes=5))
    await _seed_turn("t_b1", "s_b1", "user_b", "b2", started=4, status="running", preview="早上的任务",
                     last_activity=NOW - timedelta(hours=4))
    await _seed_turn("t_c1", "s_c1", "user_c", "c1", started=24 * 20, status="completed", preview="很久以前")


async def test_list_users_aggregates_orders_filters_and_pages(trace_db):
    await _seed_users()
    week = NOW - timedelta(days=7)
    async with trace_session() as db:
        page = await lite.list_users(db, activity_from=week, activity_to=NOW, at=NOW)
        # user_b's running turn was active 5 minutes ago; user_a's newest activity is an hour old.
        assert [item["user"]["user_id"] for item in page["items"]] == ["user_b", "user_a"] and not page["has_more"]
        b, a = page["items"]
        assert a["user"] == {"user_id": "user_a", "username": "张店长", "email": "zhang@example.test"}
        assert (a["turns"], a["failed_turns"], a["waiting_turns"], a["sessions"]) == (3, 1, 1, 2)
        assert (a["input_tokens"], a["output_tokens"], a["current_status"]) == (25, 5, "failed")
        assert (b["turns"], b["running_turns"], b["current_status"]) == (2, 2, "running")
        assert (await lite.list_users(db, has_failures=True, activity_from=week, at=NOW))["items"][0]["user"]["user_id"] == "user_a"
        assert [item["user"]["user_id"] for item in (await lite.list_users(db, active_only=True, activity_from=week, at=NOW))["items"]] == ["user_b", "user_a"]
        assert [item["user"]["user_id"] for item in (await lite.list_users(db, q="老王", activity_from=week, at=NOW))["items"]] == ["user_b"]
        everyone = await lite.list_users(db, at=NOW)
        assert [item["user"]["user_id"] for item in everyone["items"]] == ["user_b", "user_a", "user_c"]
        first = await lite.list_users(db, limit=1, at=NOW)
        assert [item["user"]["user_id"] for item in first["items"]] == ["user_b"] and first["has_more"]
        second = await lite.list_users(db, limit=1, cursor=first["next_cursor"], at=NOW)
        assert [item["user"]["user_id"] for item in second["items"]] == ["user_a"] and second["has_more"]
        third = await lite.list_users(db, limit=1, cursor=second["next_cursor"], at=NOW)
        assert [item["user"]["user_id"] for item in third["items"]] == ["user_c"] and not third["has_more"]
        with pytest.raises(TrajectoryError):
            await lite.list_users(db, limit=1, cursor=first["next_cursor"], q="x", at=NOW)
        with pytest.raises(TrajectoryError):
            await lite.list_users(db, activity_from=NOW, activity_to=week, at=NOW)


async def test_list_user_turns_filters_statuses_search_and_pages(trace_db, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_LITE_RUNNING_GRACE_MINUTES", "30")
    await _seed_users()
    async with trace_session() as db:
        page = await lite.list_user_turns(db, "user_a", at=NOW)
        assert [item["turn_id"] for item in page["items"]] == ["a1", "a2", "a3"]
        assert page["items"][0]["session_title"] == "国庆视频" and page["items"][0]["status"] == "failed"
        assert (await lite.list_user_turns(db, "user_a", statuses=["waiting"], at=NOW))["items"][0]["turn_id"] == "a2"
        assert [item["turn_id"] for item in (await lite.list_user_turns(db, "user_a", session_id="s_a2", at=NOW))["items"]] == ["a3"]
        assert [item["turn_id"] for item in (await lite.list_user_turns(db, "user_a", q="热点", at=NOW))["items"]] == ["a3"]
        running = await lite.list_user_turns(db, "user_b", statuses=["running"], at=NOW)
        assert [(item["turn_id"], item["status"]) for item in running["items"]] == [("b1", "running")]
        unknown = await lite.list_user_turns(db, "user_b", statuses=["unknown"], at=NOW)
        assert [(item["turn_id"], item["status"]) for item in unknown["items"]] == [("b2", "unknown")]
        both = await lite.list_user_turns(db, "user_b", statuses=["running", "unknown"], at=NOW)
        assert [item["turn_id"] for item in both["items"]] == ["b1", "b2"]
        first = await lite.list_user_turns(db, "user_a", limit=2, at=NOW)
        assert [item["turn_id"] for item in first["items"]] == ["a1", "a2"] and first["has_more"]
        rest = await lite.list_user_turns(db, "user_a", limit=2, cursor=first["next_cursor"], at=NOW)
        assert [item["turn_id"] for item in rest["items"]] == ["a3"] and not rest["has_more"]
        with pytest.raises(TrajectoryError):
            await lite.list_user_turns(db, "user_a", statuses=["done"], at=NOW)
        with pytest.raises(TrajectoryError):
            await lite.list_user_turns(db, "user_a", limit=2, cursor=first["next_cursor"], q="x", at=NOW)
        assert (await lite.list_user_turns(db, "nobody", at=NOW)) == {"items": [], "next_cursor": None, "has_more": False}


async def test_audience_filters_users_and_turns(trace_db, monkeypatch):
    await _seed_users()
    import trajectory.audience as audience

    async def only_b(viewer_id, targets):
        assert viewer_id == "admin"
        return {target["session_id"] for target in targets if target["session_id"] == "s_b1"}
    monkeypatch.setattr(audience, "visible_sessions", only_b)
    async with trace_session() as db:
        users = await lite.list_users(db, viewer_id="admin", at=NOW)
        assert [item["user"]["user_id"] for item in users["items"]] == ["user_b"]
        assert (await lite.list_user_turns(db, "user_a", viewer_id="admin", at=NOW))["items"] == []
        assert len((await lite.list_user_turns(db, "user_b", viewer_id="admin", at=NOW))["items"]) == 2

    async def all_but_a1(viewer_id, targets):
        # user_a's newest session is private to the viewer: the user is listed by what is left, and the page
        # is ordered by that shown activity rather than by the hidden session.
        return {target["session_id"] for target in targets if target["session_id"] != "s_a1"}
    monkeypatch.setattr(audience, "visible_sessions", all_but_a1)
    async with trace_session() as db:
        users = await lite.list_users(db, viewer_id="admin", at=NOW)
        assert [(item["user"]["user_id"], item["turns"]) for item in users["items"]] == [("user_b", 2), ("user_a", 1), ("user_c", 1)]
        assert users["items"][1]["last_activity_at"] == iso(NOW - timedelta(hours=30))


# -- Labels --

def test_every_registered_tool_has_a_label():
    import tool.registry as registry
    registry.register_builtin_tools(load_custom=False)
    document = lite.labels()
    unlabeled = sorted(tool_id for tool_id in registry._tools if lite.resolve_tool(tool_id, document)["source"] == "default")
    assert unlabeled == [], f"add these tools to trajectory/lite_labels.json: {unlabeled}"
    assert lite.resolve_tool("desktop_publish")["label"] == "发布到抖音（云电脑）"
    assert lite.resolve_tool("memory.remember") == {"label": "查阅记忆", "group": "memory", "args": [], "result": None,
                                                    "transparent": False, "source": "prefix"}
    assert lite.resolve_tool("mcp_anything")["source"] == "prefix"
    assert lite.resolve_tool("brand_new_tool") == {"label": "brand_new_tool", "group": None, "args": [], "result": None,
                                                   "transparent": False, "source": "default"}
    assert set(document["statuses"]) == set(turns.STATUSES)


def test_labels_follow_the_configured_file(tmp_path, monkeypatch):
    path = tmp_path / "labels.json"
    path.write_text('{"version": 7, "tools": {"x": {"label": "做 X"}}}', encoding="utf-8")
    monkeypatch.setenv("TRAJECTORY_LITE_LABELS_PATH", str(path))
    assert lite.labels()["version"] == 7 and lite.resolve_tool("x")["label"] == "做 X"
    monkeypatch.setenv("TRAJECTORY_LITE_LABELS_PATH", str(tmp_path / "missing.json"))
    assert lite.labels()["version"] == 0 and lite.resolve_tool("x")["source"] == "default"


def test_tool_counts_use_the_tool_id_not_the_result_title():
    rows = _base(status="completed", end_seq=9, finished=9) + [
        {**_record("tool:c1", "tool", 5, title="exit code: 0"), "tool_id": "bash"},
        {**_record("tool:c2", "tool", 6, title="Found 3 files"), "tool_id": "glob"},
        {**_record("tool:c3", "tool", 7, title="Found 1 files", status="failed", reason="boom"), "tool_id": "glob"},
        _record("tool:c4", "tool", 8, title="read")]  # no tool_id (fixture / pre-M1 rows): the title stands in
    derived = turns.derive(rows)
    assert derived["tool_counts"] == {"bash": {"total": 1, "failed": 0}, "glob": {"total": 2, "failed": 1}, "read": {"total": 1, "failed": 0}}
    assert turns.with_data(_record("tool:x", "tool", 1, title="exit code: 0"), {"data": {"tool": "bash"}})["tool_id"] == "bash"
    assert turns.with_data(_record("user:x", "user", 1), {"data": {"text": "hi", "attachments": [{"$media": {}}]}})["data"]["attachments"]
    assert "tool_id" not in turns.with_data(_record("tool:x", "tool", 1), None)


def test_the_main_agents_own_record_is_not_a_subtask():
    rows = _base(status="completed", end_seq=9, finished=9) + [
        _record("agent:main", "agent", 4, agent_id="agent_main", title="agent"),
        _record("agent:child", "agent", 5, agent_id="agent_child", title="排查登录态")]
    derived = turns.derive(rows)
    assert (derived["main_agent_id"], derived["agent_count"]) == ("agent_main", 1)


def test_classify_hides_policy_permissions_and_the_main_agent():
    document = lite.labels()
    auto = {**_record("permission:p1", "permission", 3), "duration_ms": 0}
    assert lite.classify(auto, document)["step_type"] == "hidden"
    waited = {**_record("permission:p2", "permission", 3), "duration_ms": 65_000}
    assert lite.classify(waited, document)["step_type"] == "permission"
    pending = {**_record("permission:p3", "permission", 3, status="waiting"), "duration_ms": None}
    assert lite.classify(pending, document)["step_type"] == "permission"
    main = _record("agent:main", "agent", 4, agent_id="agent_main")
    assert lite.classify(main, document, "agent_main")["step_type"] == "hidden"
    assert lite.classify(main, document, "agent_other")["step_type"] == "agent"
    tool = {**_record("tool:t", "tool", 5, title="exit code: 0"), "tool_id": "bash"}
    classified = lite.classify(tool, document)
    assert (classified["tool_id"], classified["label"], classified["group"]) == ("bash", "处理文件", "files")
