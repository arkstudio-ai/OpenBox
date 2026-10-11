"""``/v1/models``, ``/v1/me``, ``/v1/usage`` and ``/v1/usage/summary``: what a partner may buy, what it has, what it spent."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from api.v1.deps import require_scope
from api.v1.errors import ApiError
from api.v1.ids import public_id
from api.v1.public import credits_text, iso_z
from billing import partner
from db.base import get_db_session
from db.models.billing import CreditBalance, UsageEvent
from db.models.partner import PartnerRebate
from db.models.session import Session as SessionRow
from db.models.workspace import Workspace

router = APIRouter(tags=["v1-account"])

_EXTERNAL_KIND = {kind: "chat" for kind in partner.LLM_KINDS}
_USAGE_MAX_DAYS = 92
_USAGE_DEFAULT_DAYS = 7
_USAGE_LIMIT = 100
_USAGE_LIMIT_MAX = 500
_TASK_SCAN_LIMIT = 5000


def _external_kind(kind: str) -> str:
    return _EXTERNAL_KIND.get(kind, kind)


def _external_status(status: str) -> str:
    if status in ("charged", "shadow"):
        return "charged"
    if status == "pending":
        return "pending"
    return "free"


def _models_view(tariff: dict, revision: int | None, summary: dict, config) -> dict:
    declared = {m.id: m for m in getattr(config.video_generation, "models", None) or []}
    composer = {row.tier: row for row in getattr(config.model_tiers, "video", None) or []}
    tiers = []
    for quality, tier in tariff["video"]["tiers"].items():
        row = composer.get(quality)
        entry = declared.get(row.model) if row else None
        offered = list(getattr(row, "resolutions", None) or (entry.resolutions if entry else []) or [])
        multipliers = {res: str(m) for res, m in (tier.get("multipliers") or {}).items() if not offered or res in offered}
        used = summary["by_quality"].get(quality, {}).get("output_seconds", 0)
        ladder = partner.ladder_for(tariff, "video", used)
        max_shot = None
        if entry is not None:
            max_shot = entry.max_duration_seconds or (entry.duration_range[1] if entry.duration_range else None)
        tiers.append({
            "quality": quality, "name": tier.get("name") or (row.label if row else quality),
            "available": row is not None,
            "default_resolution": tier.get("default_resolution"), "max_shot_seconds": max_shot,
            "base_price": tier["base_price"][0], "base_prices": list(tier["base_price"]),
            "multipliers": multipliers, "ladder": ladder, "month_output_seconds": used,
        })
    text = [{"tier": name, "name": t.get("name") or name, "input": t["input"][0], "output": t["output"][0],
             "cache_read": t["cache_read"][0], "prices": {f: list(t[f]) for f in ("input", "output", "cache_read")}}
            for name, t in tariff["llm"]["tiers"].items()]
    compose = tariff.get("compose") or {}
    return {
        "currency": tariff.get("currency", "CNY"), "unit": "credits_per_output_second",
        "tariff_version": tariff.get("version"), "tariff_revision": revision,
        "billing_month": summary["month"],
        "ladder": {"billing": "charged at ladder 1; the ladder difference is rebated after month end",
                   "video_thresholds": list(tariff["video"]["ladder"]["thresholds"]),
                   "llm_thresholds": list(tariff["llm"]["ladder"]["thresholds"]),
                   "llm_ladder": summary["llm"]["ladder"], "month_llm_tokens": summary["llm"]["tokens"]},
        "tiers": tiers,
        "text_models": text,
        "compose": {"per_second": dict(compose.get("per_second") or {}), "min_seconds": compose.get("min_seconds", 60),
                    "round_seconds": compose.get("round_seconds", 60)} if compose else None,
    }


@router.get("/models")
async def list_models(identity: dict = Depends(require_scope("sessions:read"))):
    from core.config import get_config

    workspace_id = identity["workspace_id"]
    tariff, revision = await partner.tariff_for_workspace(workspace_id)
    summary = await partner.month_summary(workspace_id, partner.current_month())
    return _models_view(tariff, revision, summary, get_config())


@router.get("/me")
async def me(identity: dict = Depends(require_scope("sessions:read"))):
    from api.v1.ratelimit import parse_rate
    from core.config import get_config

    workspace_id = identity["workspace_id"]
    month = partner.current_month()
    summary = await partner.month_summary(workspace_id, month)
    async with get_db_session() as db:
        workspace = await db.get(Workspace, workspace_id)
        balance = await db.get(CreditBalance, workspace_id)
    spec = identity.get("rate_limit") or get_config().rate_limit_api
    try:
        rate = parse_rate(spec)
    except ValueError:
        rate = None
    policy = identity.get("policy") or {}
    return {
        "account_id": "acc_" + workspace_id.rsplit("_", 1)[-1],
        "name": workspace.name if workspace else "",
        "balance": credits_text(balance.balance if balance else None),
        "billing_month": month,
        "month_usage": {
            "output_seconds": {q: r["output_seconds"] for q, r in summary["by_quality"].items()},
            "llm_tokens": summary["llm"]["tokens"],
            "credits": credits_text(summary["charged"]),
        },
        "limits": {"requests_per_minute": rate[0] if rate else None,
                   "concurrent_sessions": policy.get("max_concurrent_sessions")},
    }


def _encode_cursor(created_at: datetime, event_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([created_at.isoformat(), event_id]).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created, event_id = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(created), str(event_id)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(400, "INVALID_REQUEST", "cursor is not valid") from exc


def _parse_time(value: str | None, field: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "INVALID_REQUEST", f"{field} must be ISO 8601", details={"field": field}) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def usage_row(event: UsageEvent, task_id: str | None) -> dict:
    pricing = event.pricing or {}
    part = pricing.get("partner") or {}
    tokens = event.tokens or {}
    row = {
        "id": event.id, "created_at": iso_z(event.created_at),
        "session_id": public_id(event.session_id, "session") if event.session_id else None,
        "task_id": task_id, "kind": _external_kind(event.kind), "model": event.model_id,
        "credits": credits_text(event.credits), "status": _external_status(event.status),
    }
    if event.kind in partner.LLM_KINDS:
        row["subkind"] = event.kind
        row["model_tier"] = part.get("tier")
        row["quantity"] = {"input_tokens": tokens.get("input", 0), "output_tokens": tokens.get("output", 0),
                           "cached_tokens": tokens.get("cache_read", 0)}
        row["ladder"] = part.get("ladder")
    elif event.kind == partner.VIDEO_KIND:
        row.update({"quality": part.get("quality"), "resolution": tokens.get("resolution") or part.get("resolution"),
                    "quantity": {"seconds": tokens.get("seconds_billed") or 0},
                    "base_price": part.get("base_price"), "multiplier": part.get("multiplier"),
                    "unit_price": pricing.get("per_second"), "ladder": part.get("ladder")})
    elif event.kind == "video_compose":
        row.update({"resolution": tokens.get("tier"), "quantity": {"seconds": tokens.get("duration_sec"),
                    "billed_seconds": tokens.get("seconds_billed") or tokens.get("minutes_billed")},
                    "unit_price": pricing.get("per_second") or pricing.get("per_minute")})
    else:
        row["quantity"] = {k: v for k, v in tokens.items() if isinstance(v, (int, float))}
    if event.status == "unreported":
        row["note"] = "provider reported no usage"
    return row


@router.get("/usage")
async def list_usage(
    identity: dict = Depends(require_scope("sessions:read")),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=_USAGE_LIMIT, ge=1, le=_USAGE_LIMIT_MAX),
):
    from api.v1.ids import internal_id

    workspace_id = identity["workspace_id"]
    end = _parse_time(to, "to") or datetime.now(timezone.utc).replace(tzinfo=None)
    start = _parse_time(from_, "from") or (end - timedelta(days=_USAGE_DEFAULT_DAYS))
    if end <= start:
        raise ApiError(400, "INVALID_REQUEST", "to must be after from")
    if end - start > timedelta(days=_USAGE_MAX_DAYS):
        raise ApiError(400, "INVALID_REQUEST", f"the range may span at most {_USAGE_MAX_DAYS} days")
    conditions = [UsageEvent.workspace_id == workspace_id, UsageEvent.created_at >= start, UsageEvent.created_at < end]
    if session_id:
        internal = internal_id(session_id, "session")
        if internal is None:
            raise ApiError(400, "INVALID_REQUEST", "session_id is not valid")
        conditions.append(UsageEvent.session_id == internal)
    if kind:
        wanted = [k for k in list(partner.LLM_KINDS) if kind == "chat"] or [kind]
        conditions.append(UsageEvent.kind.in_(wanted))
    if cursor:
        created, last = _decode_cursor(cursor)
        conditions.append((UsageEvent.created_at < created) | ((UsageEvent.created_at == created) & (UsageEvent.id < last)))
    async with get_db_session() as db:
        session_ids: set[str] | None = None
        if task_id:
            # The portable JSON column has no key operator on SQLite, so the
            # (small, per-partner) session list is filtered in Python.
            candidates = await db.execute(select(SessionRow.id, SessionRow.metadata_).where(
                SessionRow.workspace_id == workspace_id, SessionRow.metadata_.isnot(None))
                .order_by(SessionRow.created_at.desc()).limit(_TASK_SCAN_LIMIT))
            session_ids = {sid for sid, m in candidates if (m or {}).get("task_id") == task_id}
            if not session_ids:
                return {"items": [], "next_cursor": None}
            conditions.append(UsageEvent.session_id.in_(session_ids))
        rows = list(await db.scalars(select(UsageEvent).where(*conditions)
                                     .order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc()).limit(limit + 1)))
        has_more = len(rows) > limit
        rows = rows[:limit]
        wanted_sessions = {r.session_id for r in rows if r.session_id}
        meta = {}
        if wanted_sessions:
            for sid, m in await db.execute(select(SessionRow.id, SessionRow.metadata_).where(SessionRow.id.in_(wanted_sessions))):
                meta[sid] = (m or {}).get("task_id")
    items = [usage_row(r, meta.get(r.session_id)) for r in rows]
    next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/usage/summary")
async def usage_summary(
    identity: dict = Depends(require_scope("sessions:read")),
    month: str | None = None,
    group_by: str = Query(default="quality", pattern="^(quality|day)$"),
):
    workspace_id = identity["workspace_id"]
    month = month or partner.current_month()
    try:
        summary = await partner.month_summary(workspace_id, month)
    except partner.TariffError as exc:
        raise ApiError(400, "INVALID_REQUEST", str(exc)) from exc
    rows: list[dict] = []
    if group_by == "quality":
        for quality, r in summary["by_quality"].items():
            rows.append({"quality": quality, "output_seconds": r["output_seconds"], "credits": credits_text(r["charged"]),
                         "ladder": r["ladder"], "ladder_credits": credits_text(r["ladder_credits"])})
        for kind, r in summary["by_kind"].items():
            if kind == partner.VIDEO_KIND:
                continue
            row = {"kind": _external_kind(kind), "credits": credits_text(r["credits"]), "events": r["events"]}
            if kind in partner.LLM_KINDS:
                row["tokens"] = r["quantity"]
            elif kind == "video_compose":
                row["billed_seconds"] = r["quantity"]
            rows.append(row)
    else:
        for day in sorted(summary["by_day"]):
            r = summary["by_day"][day]
            rows.append({"day": day, "events": r["events"], "output_seconds": r["output_seconds"], "credits": credits_text(r["credits"])})
    async with get_db_session() as db:
        settled = await db.scalar(select(PartnerRebate).where(PartnerRebate.workspace_id == workspace_id, PartnerRebate.month == month))
    if settled is not None:
        rebate = {"status": "settled", "credits": str(settled.rebate), "settled_at": iso_z(settled.created_at)}
    else:
        rebate = {"status": "pending", "credits": credits_text(summary["rebate"])}
    return {"month": month, "group_by": group_by, "rows": rows, "total_credits": credits_text(summary["charged"]),
            "ladder_credits": credits_text(summary["ladder_credits"]), "ladder_rebate": rebate}
