"""Plans of the user's that just passed, for a word of interest: "周六的陶艺课怎么样？".

A one-off plan is remembered with an end (extraction's ``valid_until``, kept as the memory's
``ttl``), so it leaves recall once it is over (memory/policy.py). For a couple of days after
that, the assistant may ask how it went if it fits (assistant/profile.py ``prompt_section``),
and the phone greeting may, once: a plan offered in a call is marked
(``user_preferences.extra["followups_offered"]``) and never offered again.
"""
from datetime import datetime, timedelta, timezone

from core.log import create_logger

log = create_logger("assistant.followups")

WINDOW = timedelta(days=2)
LIMIT, ITEM_CHARS, OFFERED_KEPT = 2, 60, 50
OFFERED_KEY = "followups_offered"
PLAN_TYPES = ("USER_PROFILE", "USER_NOTE", "PREFERENCE", "CONSTRAINT")


def _bounded(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= ITEM_CHARS else text[:ITEM_CHARS - 1] + "…"


async def due(user_id: str, workspace_id: str | None, *, now: datetime | None = None,
              skip_offered: bool = False) -> list[tuple[str, str]]:
    """(memory id, summary) of plans that ended within the window, newest first; [] when unreadable."""
    if not workspace_id:
        return []
    try:
        return await _due(user_id, workspace_id, now or datetime.now(timezone.utc), skip_offered)
    except Exception as exc:
        log.warning("followups unread user=%s error=%s", user_id, type(exc).__name__)
        return []


async def _due(user_id, workspace_id, now, skip_offered):
    from sqlalchemy import and_, or_, select
    from db.base import get_db_session
    from db.models.memory import UserMemory
    from memory.policy import resolve_access_scope
    async with get_db_session() as db:
        access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id)
        ended = lambda column: and_(column.is_not(None), column <= now, column > now - WINDOW)  # noqa: E731
        rows = (await db.scalars(select(UserMemory).where(
            *access.predicates(UserMemory), UserMemory.status == "ACTIVE", UserMemory.deleted_at.is_(None),
            UserMemory.confirmation_status == "CONFIRMED", UserMemory.type.in_(PLAN_TYPES),
            or_(ended(UserMemory.ttl), ended(UserMemory.valid_to)))
            .order_by(UserMemory.updated_at.desc()).limit(LIMIT * 4))).all()
    offered = set(await _offered(user_id)) if skip_offered else set()
    items = [(row.id, _bounded(str((row.value or {}).get("summary") or ""))) for row in rows if row.id not in offered]
    return [item for item in items if item[1]][:LIMIT]


async def _offered(user_id: str) -> list[str]:
    from db.repository.preference_repo import PgPreferenceRepo
    extra = ((await PgPreferenceRepo().get(user_id)) or {}).get("extra") or {}
    value = extra.get(OFFERED_KEY)
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


async def mark_offered(user_id: str, memory_ids: list[str]) -> None:
    """Never offered again in a call; best-effort."""
    if not memory_ids:
        return
    from db.base import get_db_session
    from db.repository.preference_repo import locked_preference
    try:
        async with get_db_session() as db:
            row = await locked_preference(db, user_id)
            current = (row.extra or {}).get(OFFERED_KEY)
            kept = [item for item in (current if isinstance(current, list) else []) if item not in memory_ids]
            row.extra = {**(row.extra or {}), OFFERED_KEY: (kept + list(memory_ids))[-OFFERED_KEPT:]}
    except Exception as exc:
        log.warning("followups not marked user=%s error=%s", user_id, type(exc).__name__)
