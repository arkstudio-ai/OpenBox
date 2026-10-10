"""Keep a person's memories tidy: the same fact said twice becomes one memory.

Measured 2026-10-08 on the QA account: "用户周末一般会去游泳" was active twice (179 and 0 recalls),
and the copies took two of the twelve core-memory slots every turn (memory/orchestrator.py) and
twice the room in the 1200 characters the phone front desk gets. Exact copies come from different
paths (a remembered note and an extracted fact, two turns saying it again) that the per-candidate
identity checks do not join.

Copies are matched on their wording with spacing, punctuation and case ignored, within one project
(or the personal scope) and one family of types. Only copies whose evidence still stands count (a
copy whose chat was deleted must not outlive the live one). The one people rely on most is kept
(confirmed by the user first, then the most recalled, then the newest); the others are retired
(memory/service.py ``retire_in_session``): out of use, never forgotten, so a later correction of
the same fact is still learned.

memory/reconcile.py ``consolidate_duplicates`` sweeps every account in the background for the exact
same text; this runs right after a turn's memories are saved and also joins "…游泳。" with "…游泳".
"""
import re
import unicodedata

from core.log import create_logger

log = create_logger("memory.curator")

# Facts about the person in any of these types are the same kind of statement.
_FAMILIES = {"USER_PROFILE": "person", "USER_NOTE": "person", "PREFERENCE": "person", "CONSTRAINT": "person",
             "FEEDBACK": "person"}
_OWNER_RANK = {"USER_CONFIRMED": 0, "SYSTEM_VERIFIED": 1}
_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


def same_wording(summary: str) -> str:
    """The wording a duplicate shares: no spacing, punctuation or case."""
    return _PUNCT.sub("", unicodedata.normalize("NFKC", summary or "")).casefold()


def _kept_first(row) -> tuple:
    return (_OWNER_RANK.get(row.owner, 2), -(row.hit_count or 0), -(row.updated_at.timestamp() if row.updated_at else 0),
            row.id)


async def merge_duplicates(user_id: str, workspace_id: str) -> int:
    """Retire every active copy of a fact the person already has; returns how many were retired."""
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.memory import UserMemory
    from memory.policy import MemoryAccessDenied, active_memory_predicates, resolve_access_scope
    from memory.service import lock_memory_authority, memory_sources_available, retire_in_session
    retired = 0
    async with get_db_session() as db:
        await lock_memory_authority(db, user_id=user_id)
        rows = (await db.scalars(select(UserMemory).where(
            UserMemory.user_id == user_id, UserMemory.workspace_id == workspace_id,
            *active_memory_predicates()))).all()
        groups: dict[tuple, list] = {}
        for row in rows:
            wording = same_wording(str((row.value or {}).get("summary") or ""))
            if wording:
                key = (row.project_id, _FAMILIES.get(row.type, row.type), wording)
                groups.setdefault(key, []).append(row)
        scopes: dict = {}
        for (project_id, _, _), copies in groups.items():
            if len(copies) < 2:
                continue
            if project_id not in scopes:
                try:
                    scopes[project_id] = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id,
                                                                    project_id=project_id)
                except MemoryAccessDenied:
                    scopes[project_id] = None
            if scopes[project_id] is None:
                continue
            standing = [row for row in copies if await memory_sources_available(db, scopes[project_id], row)]
            for row in sorted(standing, key=_kept_first)[1:]:
                retired += await retire_in_session(db, row, reason="merged_duplicate")
    if retired:
        log.info("memory duplicates merged user=%s workspace=%s retired=%s", user_id, workspace_id, retired)
    return retired
