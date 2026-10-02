"""Assemble independent confirmed memory records for the legacy context path.

Only currently authorized, confirmed facts are stable context. Candidates,
including legacy candidates and model-supplied owner claims, stay outside the
prompt. Retrieval remains available separately for original text and details.
"""
import json
from typing import Any

from sqlalchemy import select

from core.log import create_logger
from db.base import get_db_session
from db.models.memory import UserMemory
from memory.service import PENDING_NOTE_TYPE, memory_sources_available, record_hits
from memory.policy import MemoryAccessDenied, active_memory_predicates, resolve_access_scope
from memory.presentation import legacy_memory_item

log = create_logger("memory.context")

STABLE_TYPES = {
    "USER_NOTE", "IDENTITY", "OFFERING", "DIFFERENTIATION", "AUDIENCE_PROFILE",
    "AUDIENCE_PAIN", "SIGNATURE_CASE", "EXPERTISE", "VOICE", "BOUNDARY",
    "GOAL", "ROUTINE", "STANCE", "TAGS",
}
VOLATILE_TYPES = {"IMPRESSION", "APPROVAL_SIGNAL", "REPEAT_REASON", "AUDIENCE_FAQ"}

STABLE_TYPE_ORDER = [
    "USER_NOTE", "IDENTITY", "OFFERING", "DIFFERENTIATION", "AUDIENCE_PROFILE",
    "AUDIENCE_PAIN", "EXPERTISE", "SIGNATURE_CASE", "VOICE", "STANCE",
    "BOUNDARY", "GOAL", "ROUTINE", "TAGS",
]


def extract_summary(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        summary = value.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    return ""


def _render_records(rows: list[UserMemory], title: str) -> str:
    records = [legacy_memory_item(row, extract_summary(row.value)) for row in rows if extract_summary(row.value)]
    if not records:
        return ""
    return title + "\n" + json.dumps(records, ensure_ascii=False, separators=(",", ":"))


async def assemble_user_context(
    *, user_id: str, workspace_id: str | None = None,
    project_id: str | None = None, volatile_limit: int = 5
) -> dict[str, Any]:
    async with get_db_session() as db:
        try:
            access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
        except MemoryAccessDenied:
            return {"user_id": user_id, "project_id": project_id, "context": "",
                    "stats": {"stable": 0, "volatile": 0, "total": 0, "reason": "policy_denied"}}
        stmt = select(UserMemory).where(*access.predicates(UserMemory), *active_memory_predicates(),
            UserMemory.scope.in_(["LONG_TERM", "SHORT_TERM"])).order_by(
            UserMemory.confidence.desc(), UserMemory.updated_at.desc()).limit(100)
        rows = [row for row in (await db.scalars(stmt)).all() if await memory_sources_available(db, access, row)]

    stable: list[UserMemory] = []
    volatile: list[UserMemory] = []
    for row in rows:
        if row.type == PENDING_NOTE_TYPE:
            continue  # invariant: unconfirmed proposals never reach the prompt
        if row.type in STABLE_TYPES:
            stable.append(row)
        else:
            # Unknown types land in the volatile bucket so they surface but
            # stay bounded by the volatile limit.
            volatile.append(row)
    volatile = volatile[:max(0, min(volatile_limit, 20))]
    from core.config import get_config
    budget = get_config().memory.stable_context_max_chars
    admitted_stable, admitted_volatile = [], []
    used = 48  # section titles and JSON delimiters
    for bucket, selected in ((stable, admitted_stable), (volatile, admitted_volatile)):
        for row in bucket:
            if not extract_summary(row.value):
                continue
            cost = len(json.dumps(legacy_memory_item(row, extract_summary(row.value)),
                                  ensure_ascii=False, separators=(",", ":"))) + 1
            if used + cost <= budget:
                selected.append(row)
                used += cost
    stable, volatile = admitted_stable, admitted_volatile

    stable.sort(key=lambda row: STABLE_TYPE_ORDER.index(row.type))
    sections = [s for s in (_render_records(stable, "## 已确认记忆"),
                            _render_records(volatile, "## 最近已确认记忆")) if s]
    context = "\n\n".join(sections)

    if context:
        try:
            await record_hits([row.id for row in stable + volatile], user_id=user_id,
                              workspace_id=access.workspace_id, project_id=project_id)
        except Exception as exc:  # pragma: no cover - metrics only
            log.debug(f"record_hits failed: {exc}")

    return {
        "user_id": user_id,
        "project_id": project_id,
        "context": context,
        "stats": {"stable": len(stable), "volatile": len(volatile), "total": len(rows)},
    }
