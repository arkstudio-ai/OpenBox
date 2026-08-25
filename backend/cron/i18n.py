"""Locale-aware text for cron-generated messages.

Everything cron injects into a user's conversation (scaffolding labels, the
temp-session title, the silence instruction) goes through here, so a zh-CN
user is not handed English system prose. The locale comes from the user's
persisted preference (extra.language, written by the web client's language
switch) and falls back to the deployment default.
"""
from __future__ import annotations

from core.log import create_logger

log = create_logger("cron.i18n")

SUPPORTED_LOCALES = ("zh-CN", "en-US")

# The agent replies with this exact token when the run produced nothing worth
# telling the user; it suppresses injection and delivery (kept in run history).
SILENT_SENTINEL = "NO_REPLY"

_TEXTS: dict[str, dict[str, str]] = {
    "en-US": {
        "scheduled_task": "Scheduled Task",
        "context_summary": "Session Context Summary",
        "temp_title": "{name}",
        "silent_instruction": (
            "If there is nothing that needs the user's attention, reply with "
            f"exactly {SILENT_SENTINEL} and nothing else."
        ),
        "no_output": "(No output)",
        "runlog_hint": (
            "Recent run records for this project's scheduled tasks live in the "
            "cron/ directory (one markdown file per day); consult them when "
            "earlier results matter."
        ),
        "runlog_title": "Scheduled task log · {date}",
        "runlog_silent": "Nothing to report.",
    },
    "zh-CN": {
        "scheduled_task": "定时任务",
        "context_summary": "会话上下文摘要",
        "temp_title": "{name}",
        "silent_instruction": (
            f"如果没有需要用户关注的内容,请只回复 {SILENT_SENTINEL},不要输出其他任何文字。"
        ),
        "no_output": "(无输出)",
        "runlog_hint": (
            "本项目定时任务的近期运行记录在 cron/ 目录下(按日期一个 markdown 文件),"
            "需要参考此前结果时请查阅。"
        ),
        "runlog_title": "定时任务日志 · {date}",
        "runlog_silent": "无事可报。",
    },
}


async def resolve_locale(user_id: str) -> str:
    """The user's UI language, falling back to the deployment default."""
    try:
        from db.repository.preference_repo import PgPreferenceRepo

        prefs = await PgPreferenceRepo().get(user_id)
        if prefs:
            extra = prefs.get("extra") or {}
            # The web client persists its language switch as extra.locale
            # (frontend-v2 appearance store); older shapes are tolerated.
            lang = extra.get("locale") or extra.get("language") or prefs.get("language")
            if lang in SUPPORTED_LOCALES:
                return lang
    except Exception as e:
        log.debug(f"Locale lookup failed for {user_id}: {e}")

    from core.config import get_config

    default = get_config().cron_default_locale
    return default if default in SUPPORTED_LOCALES else "zh-CN"


def text(locale: str, key: str, **fmt) -> str:
    table = _TEXTS.get(locale) or _TEXTS["zh-CN"]
    value = table.get(key) or _TEXTS["en-US"].get(key, key)
    return value.format(**fmt) if fmt else value


def is_silent(result_text: str | None) -> bool:
    """A run counts as silent when the agent said NO_REPLY or nothing at all."""
    if result_text is None:
        return True
    stripped = result_text.strip()
    if not stripped:
        return True
    if stripped == SILENT_SENTINEL:
        return True
    # Tolerate models that wrap the sentinel in emphasis or trailing punctuation.
    head = stripped.split("\n", 1)[0].strip().strip("*_`.。!! ")
    return head == SILENT_SENTINEL and len(stripped) <= len(SILENT_SENTINEL) + 8
