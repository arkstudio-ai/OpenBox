"""Assemble a creator-persona context block from stored memories.

Direct port of bossip's context.service.ts. CANDIDATE rows are included on
purpose (promotion rules are not implemented yet; excluding them would leave
the assembler with nothing). PENDING_NOTE is in neither type bucket, so an
unconfirmed proposal can never appear in an assembled prompt — that is the
one invariant this module must never lose.
"""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from core.log import create_logger
from db.base import get_db_session
from db.models.memory import UserMemory
from memory.service import PENDING_NOTE_TYPE, record_hits

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

TYPE_LABELS = {
    "USER_NOTE": "你让记住的",
    "IDENTITY": "身份",
    "OFFERING": "内容定位",
    "DIFFERENTIATION": "差异化",
    "AUDIENCE_PROFILE": "受众画像",
    "AUDIENCE_PAIN": "受众痛点",
    "SIGNATURE_CASE": "招牌案例",
    "EXPERTISE": "专业领域",
    "VOICE": "表达风格",
    "BOUNDARY": "边界",
    "GOAL": "目标",
    "ROUTINE": "日常",
    "STANCE": "立场",
    "TAGS": "标签",
}


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


def _render_stable(stable: list[UserMemory]) -> str:
    if not stable:
        return ""
    by_type: dict[str, list[UserMemory]] = {}
    for row in stable:
        by_type.setdefault(row.type, []).append(row)
    lines = ["## 创作者人设(已知)"]
    for type_name in STABLE_TYPE_ORDER:
        items = by_type.get(type_name)
        if not items:
            continue
        label = TYPE_LABELS.get(type_name, type_name)
        summary = "; ".join(s for s in (extract_summary(m.value) for m in items) if s)
        if summary:
            lines.append(f"- **{label}**: {summary}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _render_volatile(volatile: list[UserMemory]) -> str:
    if not volatile:
        return ""
    lines = ["## 最近对话印象"]
    for row in volatile:
        summary = extract_summary(row.value)
        if summary:
            lines.append(f"- {summary}")
    return "\n".join(lines) if len(lines) > 1 else ""


async def assemble_user_context(
    *, user_id: str, workspace_id: str | None = None,
    project_id: str | None = None, volatile_limit: int = 5
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserMemory)
        .where(
            UserMemory.user_id == user_id,
            UserMemory.status.in_(["CANDIDATE", "ACTIVE"]),
            UserMemory.scope.in_(["LONG_TERM", "SHORT_TERM"]),
            (UserMemory.ttl.is_(None)) | (UserMemory.ttl > now),
        )
        .order_by(UserMemory.confidence.desc(), UserMemory.updated_at.desc())
    )
    if workspace_id:
        stmt = stmt.where(UserMemory.workspace_id == workspace_id)
    if project_id is not None:
        stmt = stmt.where(
            (UserMemory.project_id == project_id) | (UserMemory.project_id.is_(None))
        )
    async with get_db_session() as db:
        rows = (await db.execute(stmt)).scalars().all()

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
    volatile = volatile[:volatile_limit]

    sections = [s for s in (_render_stable(stable), _render_volatile(volatile)) if s]
    context = "\n\n".join(sections)

    if context:
        try:
            await record_hits([row.id for row in stable + volatile], user_id=user_id)
        except Exception as exc:  # pragma: no cover - metrics only
            log.debug(f"record_hits failed: {exc}")

    return {
        "user_id": user_id,
        "project_id": project_id,
        "context": context,
        "stats": {"stable": len(stable), "volatile": len(volatile), "total": len(rows)},
    }
