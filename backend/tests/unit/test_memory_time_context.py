from datetime import datetime, timezone

from memory.index.base import DocumentSnapshot
from memory.time_context import document_matches_time, resolve_query_time


def test_relative_day_uses_user_timezone_at_utc_day_boundary():
    now = datetime(2026, 10, 1, 16, 10, tzinfo=timezone.utc)
    context = resolve_query_time("昨天决定什么？", "Asia/Shanghai", now=now)
    assert context["start_at"] == "2026-10-01T00:00:00+08:00"
    assert context["end_at"] == "2026-10-02T00:00:00+08:00"
    doc = DocumentSnapshot("memory", "m", 1, "fact", "u", "w", None, 1, "h",
                           ({"occurred_at": "2026-10-01T15:59:00+00:00"},))
    assert document_matches_time(doc, context)
    assert not document_matches_time(DocumentSnapshot("memory", "m", 1, "fact", "u", "w", None, 1, "h",
                           ({"occurred_at": "2026-10-01T16:00:00+00:00"},)), context)


def test_dst_calendar_day_and_monday_week_are_calendar_bounds():
    context = resolve_query_time("2026-03-08 的原话", "America/New_York")
    assert context["start_at"].endswith("-05:00") and context["end_at"].endswith("-04:00")
    week = resolve_query_time("上周", "Asia/Shanghai", now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert week["start_at"].startswith("2026-09-21") and week["end_at"].startswith("2026-09-28")


def test_unknown_and_invalid_date_are_not_silent_hard_filters():
    assert not resolve_query_time("以前的决定", "Asia/Shanghai")["hard_filter_applied"]
    assert resolve_query_time("2026-02-30 的决定", "Asia/Shanghai")["reason_code"] == "invalid_calendar_date"
    fallback = resolve_query_time("昨天", "bad-zone")
    assert fallback["basis"] == "invalid_timezone_fallback_utc"
    missing_time = DocumentSnapshot("wiki", "w", 1, "fact", "u", "ws", None, 1, "h")
    assert not document_matches_time(missing_time, fallback)


def test_questions_about_events_on_a_day_do_not_filter_by_when_things_were_said():
    now = datetime(2026, 10, 1, 4, tzinfo=timezone.utc)
    for query in ("这周日有什么安排", "今天几点开会", "明天天气怎么样", "本周有空吗"):
        context = resolve_query_time(query, "Asia/Shanghai", now=now)
        if "start_at" in context:
            assert not context["hard_filter_applied"] and context["reason_code"] == "event_time_question", query
    for query in ("昨天决定什么？", "我上周说的安排是什么", "今天聊过的方案", "昨天"):
        assert resolve_query_time(query, "Asia/Shanghai", now=now)["hard_filter_applied"], query
