"""A person's memory controls: automatic saving, and chats kept out of memory.

Stored in the existing per-user preferences row (``UserPreference.extra``)
rather than a table of its own. Pausing never touches what is already
remembered; it only stops new memories from being made.
"""
from sqlalchemy import select

from db.base import get_db_session
from db.models.preference import UserPreference
from db.models.session import Session

KEY = "memory"
#: Paused chats are kept as a bounded list of ids, newest last.
MAX_PAUSED_SESSIONS = 500


def _prefs(extra) -> dict:
    value = (extra or {}).get(KEY) if isinstance(extra, dict) else None
    return value if isinstance(value, dict) else {}


def _paused(prefs: dict, session_id: str | None) -> bool:
    return bool(session_id) and session_id in (prefs.get("paused_sessions") or [])


async def saving_paused_locked(db, user_id: str, session_id: str | None) -> bool:
    """True when nothing said in this chat may become a memory."""
    prefs = _prefs(await db.scalar(select(UserPreference.extra).where(UserPreference.user_id == user_id)))
    return prefs.get("auto_save") is False or _paused(prefs, session_id)


async def saving_paused(user_id: str, session_id: str | None = None) -> bool:
    async with get_db_session() as db:
        return await saving_paused_locked(db, user_id, session_id)


async def paused_scope(user_id: str, session_id: str | None = None) -> str | None:
    """Which pause applies: "account" when automatic saving is off, "chat" for this chat only."""
    async with get_db_session() as db:
        prefs = _prefs(await db.scalar(select(UserPreference.extra).where(UserPreference.user_id == user_id)))
    return "account" if prefs.get("auto_save") is False else "chat" if _paused(prefs, session_id) else None


async def get_settings(user_id: str, session_id: str | None = None) -> dict:
    async with get_db_session() as db:
        prefs = _prefs(await db.scalar(select(UserPreference.extra).where(UserPreference.user_id == user_id)))
    return {"auto_save": prefs.get("auto_save") is not False, "session_paused": _paused(prefs, session_id)}


async def update_settings(user_id: str, *, auto_save: bool | None = None, session_id: str | None = None,
                          session_paused: bool | None = None) -> dict:
    from db.repository.preference_repo import PgPreferenceRepo
    if session_id is not None:
        async with get_db_session() as db:
            owned = await db.scalar(select(Session.id).where(Session.id == session_id, Session.user_id == user_id,
                                                             Session.is_deleted.is_(False)))
        if owned is None:
            raise LookupError("chat not found")
    repo = PgPreferenceRepo()
    current = await repo.get(user_id)
    # Merge rather than replace: `extra` is a shared bag other settings use.
    extra = dict((current or {}).get("extra") or {})
    prefs = dict(_prefs(extra))
    if auto_save is not None:
        prefs["auto_save"] = bool(auto_save)
    if session_id is not None and session_paused is not None:
        paused = [item for item in prefs.get("paused_sessions") or [] if item != session_id]
        if session_paused:
            paused.append(session_id)
        prefs["paused_sessions"] = paused[-MAX_PAUSED_SESSIONS:]
    extra[KEY] = prefs
    await repo.upsert(user_id, extra=extra)
    return await get_settings(user_id, session_id)
