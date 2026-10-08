"""The name the user gives their assistant, shown in the app and used on the phone.

The user names the assistant in Settings or by telling it ("以后叫你 Mary"), in
chat or in a call; the assistant then calls ``assistant.rename``. The name is a
preference (``user_preferences.extra["assistant_name"]``, like the call
voice), never a memory: a memory is recalled sometimes, a name has to hold
every time, in the sidebar, the page title, the assistant's own replies and the
front desk's greeting. Empty means the default name the UI translates (个人助理).
"""
import re
import unicodedata

from core.log import create_logger

log = create_logger("assistant.identity")

PREFERENCE_KEY = "assistant_name"
MAX_NAME = 20
# Quotes people type around a name ("叫你“Mary”"), not part of it.
_QUOTES = "\"'“”‘’「」『』《》"
_FORBIDDEN = re.compile(r"[<>{}\[\]\\]")


def clean_name(name: str | None) -> str:
    """The name as stored: one line, trimmed, unquoted; "" clears it. Raises ValueError otherwise."""
    text = " ".join((name or "").split()).strip(_QUOTES + " ")
    if not text:
        return ""
    if len(text) > MAX_NAME or _FORBIDDEN.search(text) \
            or any(unicodedata.category(char).startswith("C") for char in text):
        raise ValueError("ASSISTANT_NAME_INVALID")
    return text


async def assistant_name(user_id: str) -> str:
    """The saved name, or "" for the default; a read failure is the default too."""
    from db.repository.preference_repo import PgPreferenceRepo
    try:
        extra = ((await PgPreferenceRepo().get(user_id)) or {}).get("extra") or {}
    except Exception as exc:
        log.warning("assistant name unread user=%s error=%s", user_id, type(exc).__name__)
        return ""
    value = extra.get(PREFERENCE_KEY)
    return value if isinstance(value, str) else ""


async def save_name(user_id: str, name: str | None) -> str:
    """Store the cleaned name (or clear it) in the user's preferences and tell every open client;
    returns what was stored."""
    from bus import bus
    from bus.events import ASSISTANT_RENAMED
    from db.repository.preference_repo import PgPreferenceRepo
    cleaned = clean_name(name)
    await PgPreferenceRepo().upsert(user_id, extra={PREFERENCE_KEY: cleaned})
    # The sidebar, the page title, the chat's speaker label and the phone app all show it right away.
    bus.publish(ASSISTANT_RENAMED, {"userId": user_id, "name": cleaned})
    return cleaned


async def rename(*, user_id, workspace_id, main_id, name, source=None) -> dict:
    """The assistant's own tool: the user named it in this conversation (the quoted human message)."""
    from assistant.commands import _authority, _tool_source_locked
    from db.base import get_db_session
    from session.internal_parts import begin_session_write
    cleaned = clean_name(name)  # before any write: an invalid name fails as the API would
    async with get_db_session() as db:
        await begin_session_write(db)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if source is not None:
            await _tool_source_locked(db, main, source, "assistant_rename")
    await save_name(user_id, cleaned)
    return {"name": cleaned, "state": "renamed" if cleaned else "default"}


def prompt_section(name: str) -> str:
    """What the assistant's system prompt says once it has a name; "" without one."""
    if not name:
        return ""
    return (f"# Your name\nThe user calls you「{name}」. Use it when you introduce yourself or when they ask who "
            "you are or what to call you; it changes nothing else about who you are or whom you work for. "
            "This is your current name: a memory that names you otherwise is out of date.")
