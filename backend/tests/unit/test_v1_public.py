"""Public projections: ids, folded turns, parts, rate-limit parsing."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from api.v1 import ids, public, ratelimit
from api.v1.errors import ApiError
from cache.memory_cache import MemoryCache
from core.identifier import ascending


def test_public_ids_rename_prefixes_both_ways():
    assert ids.public_id("session_01ABC") == "ses_01ABC"
    assert ids.public_id("message_01ABC") == "msg_01ABC"
    assert ids.public_id("part_01ABC") == "prt_01ABC"
    assert ids.public_id("asset_01ABC") == "fil_01ABC"
    assert ids.public_id("01ABC", "question") == "qst_01ABC"
    assert ids.public_id(None) is None
    assert ids.internal_id("ses_01ABC", "session") == "session_01ABC"
    assert ids.internal_id("fil_01ABC", "asset") == "asset_01ABC"
    assert ids.internal_id("qst_01ABC", "question") == "01ABC"
    # Internal ids are tolerated so JWT debugging can paste them.
    assert ids.internal_id("session_01ABC", "session") == "session_01ABC"


def test_assistant_turn_id_follows_the_user_message():
    user = ascending("message")
    turn = ids.assistant_turn_id(user)
    assert turn.startswith("msg_") and turn != ids.public_id(user)
    assert turn > ids.public_id(user)


def _msg(role, *parts, mid=None, finish=None, error=None):
    mid = mid or ascending("message")
    return SimpleNamespace(
        id=mid, role=role, finish=finish, error=error,
        created_at=datetime.now(timezone.utc).isoformat(),
        parts=[dict(p, id=p.get("id") or ascending("part")) for p in parts],
    )


def test_turns_fold_steps_and_hide_internal_parts():
    user = _msg("user", {"type": "text", "text": "make a video"},
                {"type": "file", "asset_id": "asset_1", "oss_key": "k/road.mp4", "path": "/x/road.mp4",
                 "mime_type": "video/mp4", "size": 10, "relation": {"role": "input"}})
    step1 = _msg("assistant", {"type": "step-start", "step": 1},
                 {"type": "text", "text": "Drafting…", "channel": "commentary"},
                 {"type": "tool", "tool": "bash", "status": "completed", "output": "secret"},
                 finish="tool_calls")
    step2 = _msg("assistant", {"type": "text", "text": "Done.", "channel": "final"},
                 {"type": "file", "asset_id": "asset_2", "oss_key": "k/final.mp4", "path": "/out/final_1080p.mp4",
                  "mime_type": "video/mp4", "size": 20, "relation": {"role": "final", "label": "final_1080p.mp4"}},
                 {"type": "file", "path": "/tmp/screen.png", "transient": True},
                 finish="stop")
    out = public.public_messages(
        [user, step1, step2], session_status_value="idle", checkpoints={},
        presign=lambda key, name: f"https://oss/{key}?name={name}",
    )
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert out[0]["id"] == ids.public_id(user.id)
    assert [p["type"] for p in out[0]["parts"]] == ["text", "file"]
    assert out[0]["parts"][1]["file"]["role"] == "input"
    turn = out[1]
    assert turn["id"] == ids.assistant_turn_id(user.id)
    assert turn["finish"] == "stop" and turn["error"] is None
    assert [p["type"] for p in turn["parts"]] == ["text", "text", "file"]
    assert turn["parts"][1]["text"] == "Done."
    final = turn["parts"][2]["file"]
    assert final == {
        "id": "fil_2", "filename": "final_1080p.mp4", "mime_type": "video/mp4", "size": 20,
        "duration_s": None, "width": None, "height": None,
        "url": "https://oss/k/final.mp4?name=final_1080p.mp4", "role": "final",
    }
    assert "secret" not in str(turn)


def test_unstarted_turn_is_visible_and_unfinished_while_busy():
    user = _msg("user", {"type": "text", "text": "hi"})
    busy = public.public_messages([user], session_status_value="busy", checkpoints={}, presign=None)
    assert busy[1]["finish"] is None and busy[1]["parts"] == []
    assert busy[1]["created_at"] == busy[0]["created_at"]
    idle = public.public_messages([user], session_status_value="error", checkpoints={}, presign=None)
    assert idle[1]["finish"] == "error" and idle[1]["error"]["code"] == "INTERNAL_ERROR"
    settled = public.public_messages([user], session_status_value="idle", checkpoints={}, presign=None)
    assert settled[1]["finish"] == "stop"


def test_finish_and_error_come_from_the_newest_step():
    user = _msg("user", {"type": "text", "text": "hi"})
    ok = _msg("assistant", {"type": "text", "text": "a"}, finish="tool_calls")
    failed = _msg("assistant", finish="error", error={"code": "INSUFFICIENT_CREDITS", "message": "积分不足"})
    out = public.public_messages([user, ok, failed], session_status_value="error", checkpoints={}, presign=None)
    assert out[1]["finish"] == "error"
    assert out[1]["error"] == {"code": "INSUFFICIENT_CREDITS", "message": "积分不足"}
    aborted = _msg("assistant", finish="aborted")
    out = public.public_messages([user, ok, aborted], session_status_value="idle", checkpoints={}, presign=None)
    assert out[1]["finish"] == "aborted" and out[1]["error"] is None
    # An older turn that never got a terminal step still reads as done.
    later = _msg("user", {"type": "text", "text": "again"})
    out = public.public_messages([user, ok, later], session_status_value="busy", checkpoints={}, presign=None)
    assert out[1]["finish"] == "stop" and out[3]["finish"] is None


def test_synthetic_user_messages_are_carriers_not_turns():
    user = _msg("user", {"type": "text", "text": "plan it"})
    ask = _msg("assistant", {"type": "text", "text": "ok"}, finish="tool_calls")
    carrier = _msg("user", {"type": "text", "text": "User has requested to enter plan mode.", "synthetic": True})
    resumed = _msg("assistant", {"type": "text", "text": "planning"}, finish="stop")
    out = public.public_messages([user, ask, carrier, resumed], session_status_value="idle", checkpoints={}, presign=None)
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert [p["text"] for p in out[1]["parts"]] == ["ok", "planning"]


def test_question_part_is_synthesised_from_the_checkpoint():
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    checkpoint = SimpleNamespace(
        id="01QSTULID", status="pending", expires_at=expires,
        questions=[{"question": "讲稿可以吗？", "header": "成稿", "multiple": False, "custom": True,
                    "options": [{"label": "可以", "description": ""}, {"label": "需要修改"}],
                    "detail": {"kind": "internal"}}],
    )
    user = _msg("user", {"type": "text", "text": "go"})
    step = _msg("assistant", {"type": "text", "text": "讲稿如下…"},
                {"type": "tool", "tool": "question", "status": "waiting_input",
                 "metadata": {"question_id": "01QSTULID", "question_status": "pending", "questions": ["讲稿可以吗？"]}},
                finish="waiting_input")
    out = public.public_messages([user, step], session_status_value="waiting_input",
                                 checkpoints={"01QSTULID": checkpoint}, presign=None)
    turn = out[1]
    assert turn["finish"] is None
    card = turn["parts"][1]
    assert card["type"] == "question" and card["question_id"] == "qst_01QSTULID"
    assert card["status"] == "pending"
    assert card["expires_at"] == public.iso_z(expires)
    assert card["questions"] == [{
        "header": "成稿", "question": "讲稿可以吗？", "multiple": False, "custom": True,
        "options": [{"label": "可以", "description": ""}, {"label": "需要修改", "description": ""}],
    }]

    checkpoint.status = "expired"
    out = public.public_messages([user, step], session_status_value="idle",
                                 checkpoints={"01QSTULID": checkpoint}, presign=None)
    assert out[1]["parts"][1]["status"] == "timeout"

    checkpoint.status = "pending"
    checkpoint.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    out = public.public_messages([user, step], session_status_value="idle",
                                 checkpoints={"01QSTULID": checkpoint}, presign=None)
    assert out[1]["parts"][1]["status"] == "timeout"


def test_native_question_part_wins_over_the_tool_part():
    user = _msg("user", {"type": "text", "text": "go"})
    step = _msg("assistant",
                {"type": "tool", "tool": "question", "metadata": {"question_id": "01Q", "question_status": "pending"}},
                {"type": "question", "question_id": "01Q", "status": "answered", "expires_at": "2026-09-21T08:10:00+00:00",
                 "questions": [{"question": "x", "options": []}]},
                finish="stop")
    out = public.public_messages([user, step], session_status_value="idle", checkpoints={}, presign=None)
    cards = [p for p in out[1]["parts"] if p["type"] == "question"]
    assert len(cards) == 1
    assert cards[0]["status"] == "answered" and cards[0]["expires_at"] == "2026-09-21T08:10:00Z"


def test_session_view_and_status_mapping():
    now = datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc)
    row = SimpleNamespace(id="session_1", title=None, status="waiting_input", quality="high",
                          metadata_={"task_id": "t_1"}, created_at=now, updated_at=now)
    view = public.session_view(row, Decimal("12.500000"))
    assert view == {
        "id": "ses_1", "title": "", "status": "busy", "quality": "high", "metadata": {"task_id": "t_1"},
        "credits_used": "12.5", "created_at": "2026-09-21T08:00:00Z", "updated_at": "2026-09-21T08:00:00Z",
    }
    assert public.session_status("idle") == "idle"
    assert public.session_status("compacting") == "busy"
    assert public.session_status("error") == "error"
    assert public.credits_text(None) == "0"


def test_rate_limit_parsing_and_fixed_window():
    assert ratelimit.parse_rate("60/minute") == (60, 60)
    assert ratelimit.parse_rate("5/second") == (5, 1)
    assert ratelimit.parse_rate("100/hours") == (100, 3600)
    assert ratelimit.parse_rate("") is None and ratelimit.parse_rate("0/minute") is None
    with pytest.raises(ValueError):
        ratelimit.parse_rate("many/minute")


async def test_rate_limit_counts_per_subject_and_answers_429():
    cache = MemoryCache()
    first = await ratelimit.check(cache, "key_a", "2/minute", now=120.0)
    assert (first.limit, first.remaining, first.reset) == (2, 1, 180)
    second = await ratelimit.check(cache, "key_a", "2/minute", now=121.0)
    assert second.remaining == 0
    with pytest.raises(ApiError) as exc:
        await ratelimit.check(cache, "key_a", "2/minute", now=130.0)
    assert exc.value.status_code == 429 and exc.value.code == "RATE_LIMITED"
    assert exc.value.headers["Retry-After"] == "50"
    other = await ratelimit.check(cache, "key_b", "2/minute", now=130.0)
    assert other.remaining == 1
    fresh = await ratelimit.check(cache, "key_a", "2/minute", now=181.0)
    assert fresh.remaining == 1
    assert await ratelimit.check(None, "key_a", "2/minute") is None


def test_pending_work_keeps_the_latest_turn_open():
    user = _msg("user", {"type": "text", "text": "make a video"})
    step = _msg("assistant", {"type": "text", "text": "任务仍在处理"}, finish="stop")
    out = public.public_messages([user, step], session_status_value="idle", checkpoints={}, presign=None,
                                 work_pending=True)
    assert out[1]["finish"] is None
    # An older turn is never held open by later work.
    later = _msg("user", {"type": "text", "text": "and another"})
    out = public.public_messages([user, step, later], session_status_value="idle", checkpoints={}, presign=None,
                                 work_pending=True)
    assert out[1]["finish"] == "stop" and out[3]["finish"] is None
    # An error is still an error.
    failed = _msg("assistant", finish="error", error={"code": "X", "message": "y"})
    out = public.public_messages([user, failed], session_status_value="idle", checkpoints={}, presign=None,
                                 work_pending=True)
    assert out[1]["finish"] == "error"


def test_aborted_before_any_step_reads_as_aborted():
    user = _msg("user", {"type": "text", "text": "go"})
    out = public.public_messages([user], session_status_value="idle", checkpoints={}, presign=None,
                                 aborted_user_message_ids={user.id})
    assert out[1]["finish"] == "aborted" and out[1]["parts"] == []
    # A claimed turn that is still running is not aborted yet.
    out = public.public_messages([user], session_status_value="busy", checkpoints={}, presign=None,
                                 aborted_user_message_ids={user.id})
    assert out[1]["finish"] is None


def test_platform_continuations_fold_into_the_previous_turn():
    user = _msg("user", {"type": "text", "text": "make a video"})
    paused = _msg("assistant", {"type": "text", "text": "任务仍在处理，稍后继续"}, finish="stop")
    resume = _msg("user", {"type": "text", "text": "系统提示：视频任务已完成", "synthetic": True})
    resume.client_message_id = "vjob:video_1"
    delivered = _msg("assistant", {"type": "text", "text": "成片如下"},
                     {"type": "file", "asset_id": "asset_9", "oss_key": "k/take.mp4", "path": "/out/take.mp4",
                      "mime_type": "video/mp4", "size": 5, "relation": {"role": "intermediate"}},
                     finish="stop")
    out = public.public_messages([user, paused, resume, delivered], session_status_value="idle",
                                 checkpoints={}, presign=None)
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert [p["type"] for p in out[1]["parts"]] == ["text", "text", "file"]
    assert out[1]["finish"] == "stop"


def test_final_role_mapping_and_promotion():
    user = _msg("user", {"type": "text", "text": "go"})
    shared = _msg("assistant",
                  {"type": "file", "asset_id": "a1", "oss_key": "k/1.mp4", "path": "/1.mp4", "mime_type": "video/mp4",
                   "size": 1, "relation": {"role": "intermediate"}},
                  {"type": "file", "asset_id": "a2", "oss_key": "k/2.mp4", "path": "/2.mp4", "mime_type": "video/mp4",
                   "size": 1, "relation": {"role": "result"}},
                  finish="stop")
    out = public.public_messages([user, shared], session_status_value="idle", checkpoints={}, presign=None)
    assert [p["file"]["role"] for p in out[1]["parts"]] == ["intermediate", "final"]

    takes = _msg("assistant",
                 {"type": "file", "asset_id": "a1", "oss_key": "k/1.mp4", "path": "/1.mp4", "mime_type": "video/mp4",
                  "size": 1, "relation": {"role": "intermediate"}},
                 {"type": "file", "asset_id": "a2", "oss_key": "k/2.mp4", "path": "/2.mp4", "mime_type": "video/mp4",
                  "size": 1, "relation": {"role": "intermediate"}},
                 {"type": "file", "asset_id": "a3", "oss_key": "k/s.png", "path": "/s.png", "mime_type": "image/png",
                  "size": 1, "relation": {"role": "evidence"}},
                 finish="stop")
    out = public.public_messages([user, takes], session_status_value="idle", checkpoints={}, presign=None)
    assert [p["file"]["role"] for p in out[1]["parts"]] == ["intermediate", "final", "intermediate"]
    # Not while the turn is still open.
    out = public.public_messages([user, takes], session_status_value="busy", checkpoints={}, presign=None,
                                 work_pending=True)
    assert [p["file"]["role"] for p in out[1]["parts"]] == ["intermediate", "intermediate", "intermediate"]
