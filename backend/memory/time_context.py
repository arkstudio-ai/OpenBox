"""Bounded explicit/relative calendar ranges with an inspectable timezone basis."""
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Wording that asks about what was said or decided at that time ...
_SPOKEN = re.compile(r"说|聊|提到|讲过|讲的|问过|告诉|记下|记住|决定|讨论|原话|对话|消息|发的|写的|定的|约定|商量")
# ... versus wording that asks about events, plans or schedules on that day.
_EVENT = re.compile(r"安排|计划|日程|行程|要做|做什么|干什么|干嘛|去哪|几点|天气|预约|会议|开会|上课|课程|活动"
                    r"|有空|忙不忙|待办|提醒|生日|放假|休息|营业|开门|闭馆|上班|下班|吃什么|穿什么")


def resolve_query_time(query, timezone_name, *, now=None, basis="configured_timezone"):
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        zone, timezone_name, basis = ZoneInfo("UTC"), "UTC", "invalid_timezone_fallback_utc"
    instant = now or datetime.now(timezone.utc)
    local_day = instant.astimezone(zone).date()
    start_day = end_day = expression = None
    match = re.search(r"(?<!\d)(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})日?(?!\d)", query)
    if match:
        try:
            start_day = date(*(int(value) for value in match.groups()))
            end_day, expression = start_day + timedelta(days=1), match.group()
        except ValueError:
            return {"timezone": timezone_name, "basis": basis, "expression": match.group(),
                    "hard_filter_applied": False, "reason_code": "invalid_calendar_date"}
    else:
        recent = re.search(r"(?:最近|过去|近)\s*(\d{1,3})\s*天", query)
        if recent and 1 <= int(recent[1]) <= 365:
            expression = recent.group()
            start_day, end_day = local_day - timedelta(days=int(recent[1]) - 1), local_day + timedelta(days=1)
        else:
            for token, days in (("前天", 2), ("昨天", 1), ("今天", 0)):
                if token in query:
                    expression, start_day = token, local_day - timedelta(days=days)
                    end_day = start_day + timedelta(days=1)
                    break
            if start_day is None and ("上周" in query or "本周" in query or "这周" in query):
                expression = "上周" if "上周" in query else "本周" if "本周" in query else "这周"
                start_day = local_day - timedelta(days=local_day.weekday() + (7 if expression == "上周" else 0))
                end_day = start_day + timedelta(days=7)
            if start_day is None and ("上个月" in query or "上月" in query or "本月" in query):
                this_month = local_day.replace(day=1)
                if "上个月" in query or "上月" in query:
                    expression = "上个月" if "上个月" in query else "上月"
                    start_day, end_day = (this_month - timedelta(days=1)).replace(day=1), this_month
                else:
                    expression, start_day = "本月", this_month
                    end_day = (this_month.replace(day=28) + timedelta(days=4)).replace(day=1)
    if start_day is None:
        return {"timezone": timezone_name, "basis": basis, "reference_time": instant.isoformat(),
                "hard_filter_applied": False, "reason_code": "no_supported_calendar_expression"}
    resolved = {"timezone": timezone_name, "basis": basis, "expression": expression,
            "reference_time": instant.isoformat(), "start_at": datetime.combine(start_day, time.min, zone).isoformat(),
            "end_at": datetime.combine(end_day, time.min, zone).isoformat(), "end_exclusive": True,
            "time_basis": "source_occurred_at", "hard_filter_applied": True, "reason_code": "resolved_calendar_range"}
    if _EVENT.search(query) and not _SPOKEN.search(query):
        # "这周日有什么安排" asks about Sunday, not about what was said this
        # week. Filtering by when things were said would drop every standing
        # fact ("通常周日去看望父母"), so the range stays informational only.
        return {**resolved, "time_basis": "event_time", "hard_filter_applied": False,
                "reason_code": "event_time_question"}
    return resolved


def document_matches_time(document, context):
    if not context.get("hard_filter_applied"):
        return True
    # Reference documents have no authoritative event timestamp. Filtering by
    # their upload date would discard a relevant policy or future schedule.
    if any(source.get("document_id") for source in document.sources):
        return True
    start, end = datetime.fromisoformat(context["start_at"]), datetime.fromisoformat(context["end_at"])
    for source in document.sources:
        value = source.get("occurred_at")
        if not value:
            continue
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is not None and start <= instant < end:
                return True
        except (TypeError, ValueError):
            continue
    return False


async def query_time_context(db, user_id, query, config):
    from sqlalchemy import select
    from db.models.preference import UserPreference
    row = await db.scalar(select(UserPreference).where(UserPreference.user_id == user_id))
    extra = row.extra if row and isinstance(row.extra, dict) else {}
    preference = extra.get("timezone")
    return resolve_query_time(query, preference or config.default_timezone,
                              basis="user_preference_extra_timezone" if preference else "configured_timezone")
