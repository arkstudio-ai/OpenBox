from datetime import datetime, timezone

from memory.index.base import DocumentSnapshot
from memory.time_context import document_matches_time, resolve_query_time


def history(expression):
    return {"expression": expression, "choice": "source_time", "confidence": .99}


def test_relative_day_uses_user_timezone_at_utc_day_boundary():
    now = datetime(2026, 10, 1, 16, 10, tzinfo=timezone.utc)
    context = resolve_query_time("昨天决定什么？", "Asia/Shanghai", now=now, intent=history("昨天"))
    assert context["start_at"] == "2026-10-01T00:00:00+08:00"
    assert context["end_at"] == "2026-10-02T00:00:00+08:00"
    doc = DocumentSnapshot("memory", "m", 1, "fact", "u", "w", None, 1, "h",
                           ({"occurred_at": "2026-10-01T15:59:00+00:00"},))
    assert document_matches_time(doc, context)
    assert not document_matches_time(DocumentSnapshot("memory", "m", 1, "fact", "u", "w", None, 1, "h",
                           ({"occurred_at": "2026-10-01T16:00:00+00:00"},)), context)


def test_dst_calendar_day_and_monday_week_are_calendar_bounds():
    context = resolve_query_time("2026-03-08 的原话", "America/New_York", intent=history("2026-03-08"))
    assert context["start_at"].endswith("-05:00") and context["end_at"].endswith("-04:00")
    week = resolve_query_time("上周", "Asia/Shanghai", now=datetime(2026, 10, 1, tzinfo=timezone.utc), intent=history("上周"))
    assert week["start_at"].startswith("2026-09-21") and week["end_at"].startswith("2026-09-28")


def test_unknown_and_invalid_date_are_not_silent_hard_filters():
    assert not resolve_query_time("以前的决定", "Asia/Shanghai")["hard_filter_applied"]
    assert resolve_query_time("2026-02-30 的决定", "Asia/Shanghai")["reason_code"] == "invalid_calendar_date"
    fallback = resolve_query_time("昨天", "bad-zone", intent=history("昨天"))
    assert fallback["basis"] == "invalid_timezone_fallback_utc"
    missing_time = DocumentSnapshot("wiki", "w", 1, "fact", "u", "ws", None, 1, "h")
    assert not document_matches_time(missing_time, fallback)


def test_questions_about_events_on_a_day_do_not_filter_by_when_things_were_said():
    now = datetime(2026, 10, 1, 4, tzinfo=timezone.utc)
    for query, expression in (("今天不想出门，在家有一个小时空闲，你觉得我做点什么会比较开心？", "今天"),
                              ("今天我决定给自己一个悠闲下午", "今天"), ("本周我想写点什么", "本周")):
        context = resolve_query_time(query, "Asia/Shanghai", now=now,
            intent={"expression": expression, "choice": "event_time", "confidence": .99})
        if "start_at" in context:
            assert not context["hard_filter_applied"] and context["reason_code"] == "event_time_question", query
    for expression in ("昨天", "上周", "今天"):
        assert resolve_query_time(expression, "Asia/Shanghai", now=now, intent=history(expression))["hard_filter_applied"]


def test_missing_uncertain_or_ungrounded_intent_never_discards_older_facts():
    for intent in (None, {}, history("上周"), {**history("今天"), "confidence": .7},
                   {**history("今天"), "confidence": True}, {**history("今天"), "choice": "uncertain"}):
        context = resolve_query_time("今天决定做点什么", "Asia/Shanghai", intent=intent)
        assert not context["hard_filter_applied"]
        assert context["reason_code"] == "time_intent_unresolved"


def test_grounded_history_expression_ignores_unrelated_event_date():
    context = resolve_query_time("今天想回顾上周聊过的安排", "Asia/Shanghai", intent=history("上周"),
        now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert context["expression"] == "上周" and context["hard_filter_applied"]
    assert context["start_at"].startswith("2026-09-21")


def test_ambiguous_multi_date_quote_cannot_silently_select_first_day():
    query = "今天想回顾上周聊过的安排"
    context = resolve_query_time(query, "Asia/Shanghai", intent=history(query))
    assert not context["hard_filter_applied"]
    assert context["reason_code"] == "ambiguous_calendar_range"
