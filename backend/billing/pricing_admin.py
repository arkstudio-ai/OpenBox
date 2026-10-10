"""The operator's pricing table: every billable item with its cost, sale price and recent usage.

Reads build the table from four sources so nothing an operator would need to
price is missing: the base ``rates.json``, the deployment's configuration
(models declared there but never priced show up as *unpriced*), the current
rules, and whatever ``usage_events`` recorded in the window. Writes append a
``PricingRule`` revision under the audit receipt, like ``billing.admin``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select

from billing import rules
from billing.pricing import PRECISION, base_catalogue, catalogue, cost_fx, normalize_usage, quote
from billing.rules import ItemKey, LLM_FIELDS, RuleError, RuleView, VOICE_MODALITIES
from core.identifier import ascending
from db.base import get_db_session
from db.models.audit_log import AuditLog
from db.models.billing import PricingRule, UsageEvent
from db.models.user import User

USAGE_WINDOW_DAYS = 30
EXPIRING_DAYS = 30
EFFECTIVE_WITHIN_SECONDS = rules.REFRESH_SECONDS
UNITS = {"llm": "per_million", "video-gen": "per_second", "image-gen": "per_image", "stt": "per_minute",
         "voice-realtime": "per_million", "ims-compose": "per_second", "hot-trends": "per_fetch"}
# usage_events.kind values that are LLM metering (priced through `quote`).
LLM_KINDS = ("chat", "bash_judge", "suggestions", "title", "compaction", "cron_summary", "video_analyze")


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class PricingAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class PricingWrite(PricingAction):
    """The full desired override for one key: what is left out falls back to rates.json."""
    expected_revision: int = Field(ge=0)
    status: Literal["active", "disabled"] = "active"
    sale: dict[str, Any] | None = None
    cost: dict[str, Any] | None = None
    valid_until: AwareDatetime | None = None
    allow_below_cost: bool = False
    confirm_disable: bool = False


class PricingRevert(PricingAction):
    expected_revision: int = Field(ge=1)


class PricingPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=3, max_length=160)
    usage: dict[str, Any] = Field(default_factory=dict)
    sale: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Catalog of items
# ---------------------------------------------------------------------------

def _dec(value) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _config_items(config) -> dict[str, dict]:
    """Items the deployment is configured to use, with display names."""
    items: dict[str, dict] = {}

    def llm(model_id: str | None, label: str = ""):
        if not model_id:
            return
        item = ItemKey("llm", model_id.rsplit("/", 1)[-1].lower())
        items.setdefault(item.key, {"item": item, "label": label or item.model, "configured": True})

    for row in getattr(config, "models", None) or []:
        llm(row.id, row.name or row.id)
    llm(getattr(config, "model", None))
    llm(getattr(config, "mcp_filter_model", None))
    # Side uses (video analysis, call summaries) are the same billable model;
    # they add the item, never rename it.
    analysis = getattr(config, "video_analysis", None)
    llm(getattr(analysis, "model", None))
    voice = getattr(config, "voice", None)
    if voice is not None:
        llm(getattr(voice, "summary_model", None))
        model = getattr(voice, "model", None)
        if model:
            item = ItemKey("voice-realtime", model)
            items.setdefault(item.key, {"item": item, "label": f"语音通话 {model}", "configured": True})
    video = getattr(config, "video_generation", None)
    if video is not None:
        for m in getattr(video, "models", None) or []:
            for res in m.resolutions or []:
                item = ItemKey("video-gen", m.id, res)
                items.setdefault(item.key, {"item": item, "label": f"{m.name or m.id} {res}", "configured": True,
                                            "channel": m.channel, "provider": m.provider})
    image = getattr(config, "image_generation", None)
    if image is not None and getattr(image, "model", None):
        item = ItemKey("image-gen", image.model.rsplit("/", 1)[-1])
        items.setdefault(item.key, {"item": item, "label": f"生图 {image.model}", "configured": True})
    stt = getattr(config, "video_transcription", None)
    if stt is not None and getattr(stt, "model", None):
        item = ItemKey("stt", stt.model)
        items.setdefault(item.key, {"item": item, "label": f"转写 {stt.model}", "configured": True})
    for tier in ("480p", "720p", "1080p", "2k", "4k"):
        item = ItemKey("ims-compose", tier)
        items.setdefault(item.key, {"item": item, "label": f"视频合成 {tier}", "configured": True})
    return items


def _base_items(base: dict) -> dict[str, dict]:
    items: dict[str, dict] = {}
    for model in base.get("models", {}):
        item = ItemKey("llm", model)
        items[item.key] = {"item": item, "label": model}
    for alias, target in base.get("aliases", {}).items():
        item = ItemKey("llm", alias)
        items.setdefault(item.key, {"item": item, "label": alias})["alias_of"] = target
    media = base.get("media", {})
    for name, entry in media.items():
        if name.startswith("ims-compose-"):
            item = ItemKey("ims-compose", name.removeprefix("ims-compose-"))
            items.setdefault(item.key, {"item": item, "label": f"视频合成 {item.model}"})
            continue
        if not isinstance(entry, dict):
            continue
        for model, rate in entry.items():
            if model.startswith("_") or not isinstance(rate, dict):
                continue
            if name == "video-gen":
                for res in (rate.get("per_second") or {}):
                    item = ItemKey("video-gen", model, res)
                    items.setdefault(item.key, {"item": item, "label": f"{model} {res}"})
                for res in ((rate.get("cost") or {}).get("per_second") or {}):
                    item = ItemKey("video-gen", model, res)
                    items.setdefault(item.key, {"item": item, "label": f"{model} {res}"})
            elif name in ("image-gen", "stt", "voice-realtime", "hot-trends"):
                item = ItemKey(name, model)
                items.setdefault(item.key, {"item": item, "label": model})
    return items


async def _usage_by_key(db, since: datetime) -> dict[str, dict]:
    rows = (await db.execute(
        select(UsageEvent.kind, UsageEvent.model_id, UsageEvent.status, func.count(),
               func.coalesce(func.sum(UsageEvent.credits), 0), func.coalesce(func.sum(UsageEvent.cost_credits), 0),
               func.count(UsageEvent.cost_credits), func.coalesce(func.sum(UsageEvent.total_tokens), 0))
        .where(UsageEvent.created_at >= since)
        .group_by(UsageEvent.kind, UsageEvent.model_id, UsageEvent.status)
    )).all()
    out: dict[str, dict] = {}
    for kind, model_id, status, events, credits, cost, costed, tokens in rows:
        item = ItemKey.for_usage(kind, model_id)
        if item is None:
            continue
        bucket = out.setdefault(item.key, {"events": 0, "charged_events": 0, "credits": Decimal(0), "shadow_credits": Decimal(0),
                                           "cost_credits": Decimal(0), "costed_events": 0, "tokens": 0, "unpriced_events": 0})
        bucket["events"] += int(events)
        bucket["tokens"] += int(tokens or 0)
        if status == "charged":
            bucket["charged_events"] += int(events)
            bucket["credits"] += Decimal(credits or 0)
            bucket["cost_credits"] += Decimal(cost or 0)
            bucket["costed_events"] += int(costed or 0)
        elif status == "shadow":
            bucket["shadow_credits"] += Decimal(credits or 0)
        elif status == "unpriced":
            bucket["unpriced_events"] += int(events)
    return out


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(PRECISION).normalize(), "f")


def _sale_cny(item: ItemKey, sale: dict | None, data: dict) -> dict[str, Decimal]:
    """Sale prices in credits per unit, keyed by field (llm: input/output/...; others: the unit)."""
    if not sale:
        return {}
    if item.kind == "llm":
        conversion = sale.get("conversion") or ("official_cny" if sale.get("currency", "CNY") == "CNY" else "usd_cny")
        fx = Decimal(data["usd_cny"]) if conversion == "usd_cny" else Decimal(1)
        return {name: Decimal(str(sale[name])) * fx for name in LLM_FIELDS if sale.get(name) is not None}
    if item.kind == "voice-realtime":
        return {m: Decimal(str(sale["per_million"][m])) for m in VOICE_MODALITIES if m in sale.get("per_million", {})}
    unit = UNITS[item.kind]
    return {unit: Decimal(str(sale[unit]))} if sale.get(unit) is not None else {}


def _cost_cny(item: ItemKey, cost: dict | None, data: dict) -> dict[str, Decimal]:
    if not cost:
        return {}
    fx = cost_fx(data, str(cost.get("currency", "CNY")).upper())
    if item.kind == "llm":
        return {name: Decimal(str(cost[name])) * fx for name in LLM_FIELDS if cost.get(name) is not None}
    if item.kind == "voice-realtime":
        per = cost.get("per_million") or {}
        return {m: Decimal(str(per[m])) * fx for m in VOICE_MODALITIES if m in per}
    unit = UNITS[item.kind]
    if item.kind == "ims-compose" and cost.get("per_minute") is not None and cost.get("per_second") is None:
        return {"per_second": Decimal(str(cost["per_minute"])) * fx / 60}
    return {unit: Decimal(str(cost[unit])) * fx} if cost.get(unit) is not None else {}


def _margins(sale: dict[str, Decimal], cost: dict[str, Decimal]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name, price in sale.items():
        basis = cost.get(name)
        if basis is None:
            out[name] = None
        elif basis == 0:
            out[name] = None if price == 0 else "inf"
        else:
            out[name] = format(((price - basis) / basis * 100).quantize(Decimal("0.1")), "f")
    return out


def _below_cost(sale: dict[str, Decimal], cost: dict[str, Decimal]) -> list[str]:
    return [name for name, price in sale.items() if name in cost and price < cost[name]]


def _expiry(item: ItemKey, base: dict, rule: RuleView | None, at: datetime) -> str | None:
    """The soonest date this item's current price stops applying, if within the warning window."""
    dates: list[datetime] = []
    if rule is not None and rule.valid_until is not None:
        dates.append(rule.valid_until)
    elif item.kind == "llm":
        entry = rules.base_entry(base, item)
        if entry and entry.get("valid_until"):
            dates.append(datetime.fromisoformat(entry["valid_until"]).replace(tzinfo=timezone.utc) + timedelta(days=1))
    soon = [d for d in dates if d <= at + timedelta(days=EXPIRING_DAYS)]
    return min(soon).isoformat() if soon else None


def _row(entry: dict, *, base: dict, effective: dict, rule: RuleView | None, usage: dict | None, at: datetime) -> dict:
    item: ItemKey = entry["item"]
    base_sale = rules.item_sale(base, item)
    sale = rules.item_sale(effective, item)
    cost = rules.item_cost(effective, item)
    sale_cny = _sale_cny(item, sale, effective)
    cost_cny = _cost_cny(item, cost, effective)
    disabled = rule is not None and rule.status == "disabled"
    below = _below_cost(sale_cny, cost_cny)
    flags = []
    if disabled:
        flags.append("disabled")
    if sale is None and not disabled:
        flags.append("unpriced")
    if below:
        flags.append("below_cost")
    if not cost_cny:
        flags.append("no_cost")
    expiry = _expiry(item, base, rule, at)
    if expiry:
        flags.append("expiring")
    if entry.get("alias_of") and not (rule is not None and rule.sale):
        flags.append("alias")
    if rule is not None:
        flags.append("overridden")
    if not usage or usage["events"] == 0:
        flags.append("unused_30d")
    if entry.get("configured"):
        flags.append("configured")
    usage_view = None
    if usage:
        usage_view = {**{k: (int(v) if isinstance(v, int) else _money(v)) for k, v in usage.items()}}
        revenue, spend = usage["credits"], usage["cost_credits"]
        usage_view["gross_margin"] = _money(revenue - spend) if usage["costed_events"] else None
    return {
        "key": item.key, "kind": item.kind, "model": item.model, "resolution": item.resolution or None,
        "label": entry.get("label") or item.key, "unit": UNITS[item.kind],
        "alias_of": entry.get("alias_of"), "channel": entry.get("channel"),
        "base_sale": base_sale, "sale": sale, "cost": cost,
        "sale_credits": {k: _money(v) for k, v in sale_cny.items()},
        "cost_credits": {k: _money(v) for k, v in cost_cny.items()},
        "margin_pct": _margins(sale_cny, cost_cny), "below_cost_fields": below,
        "flags": flags, "expires_at": expiry,
        "rule": rule.public() if rule else None,
        "usage_30d": usage_view,
    }


async def pricing_table(config, *, at: datetime | None = None) -> dict:
    at = at or datetime.now(timezone.utc)
    base = base_catalogue()
    effective = catalogue(at=at)
    current = {r.key: r for r in rules.current_rules()}
    items = _base_items(base)
    for key, entry in _config_items(config).items():
        if key in items:
            items[key].update({k: v for k, v in entry.items() if k != "item"})
        else:
            items[key] = entry
    for key in current:
        items.setdefault(key, {"item": ItemKey.parse(key), "label": key})
    since = at - timedelta(days=USAGE_WINDOW_DAYS)
    async with get_db_session() as db:
        usage = await _usage_by_key(db, since)
    for key in usage:
        try:
            items.setdefault(key, {"item": ItemKey.parse(key), "label": key})
        except RuleError:
            continue
    rows = [_row(entry, base=base, effective=effective, rule=current.get(key), usage=usage.get(key), at=at)
            for key, entry in items.items()]
    order = {kind: i for i, kind in enumerate(rules.KINDS)}
    rows.sort(key=lambda r: (order.get(r["kind"], 99), r["model"], r["resolution"] or ""))
    totals = {"credits": Decimal(0), "cost_credits": Decimal(0), "costed_events": 0, "charged_events": 0}
    for bucket in usage.values():
        totals["credits"] += bucket["credits"]
        totals["cost_credits"] += bucket["cost_credits"]
        totals["costed_events"] += bucket["costed_events"]
        totals["charged_events"] += bucket["charged_events"]
    counts = {flag: sum(1 for r in rows if flag in r["flags"]) for flag in ("below_cost", "unpriced", "expiring", "overridden", "no_cost")}
    return {
        "as_of": at.isoformat(), "window_days": USAGE_WINDOW_DAYS,
        "catalogue_version": effective.get("version"), "base_version": base.get("version"),
        "usd_cny": base.get("usd_cny"), "rules_loaded_at": rules.loaded_at().isoformat() if rules.loaded_at() else None,
        "effective_within_seconds": EFFECTIVE_WITHIN_SECONDS,
        "summary": {"credits": _money(totals["credits"]), "cost_credits": _money(totals["cost_credits"]),
                    "gross_margin": _money(totals["credits"] - totals["cost_credits"]),
                    "costed_events": totals["costed_events"], "charged_events": totals["charged_events"],
                    "flags": counts},
        "items": rows,
    }


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def preview_quote(item: ItemKey, usage: dict, data: dict) -> dict:
    """What ``usage`` would cost in credits under ``data`` (and what it costs us)."""
    from billing import media

    def view(credits, cost, snapshot):
        return {"credits": _money(credits), "cost_credits": _money(cost),
                "reason": snapshot.get("reason"), "version": snapshot.get("version")}

    if item.kind == "llm":
        q = quote(item.model, normalize_usage(usage or {"input": 1_000_000, "output": 1_000_000}), rates=data)
        return view(q.credits, q.cost, q.snapshot)
    if item.kind == "video-gen":
        q = media.quote_generation(item.model, item.resolution, float(usage.get("seconds", 10)), rates=data)
    elif item.kind == "image-gen":
        q = media.quote_image(item.model, int(usage.get("images", 1)), rates=data)
    elif item.kind == "stt":
        q = media.quote_transcription(item.model, float(usage.get("seconds", 60)), rates=data)
    elif item.kind == "ims-compose":
        side = rules._SHORT_SIDE.get(item.model, 720)
        q = media.quote_compose(side, side * 16 // 9, float(usage.get("seconds", 60)), rates=data)
    elif item.kind == "hot-trends":
        q = media.quote_hot_trends(item.model, rates=data)
    else:  # voice-realtime: tokens by modality
        entry = (data.get("media", {}).get("voice-realtime") or {}).get(item.model)
        if not isinstance(entry, dict):
            return {"credits": None, "cost_credits": None, "reason": "No verified price for this voice model", "version": data.get("version")}
        tokens = {m: int(usage.get(m, 0)) for m in VOICE_MODALITIES}
        credits = sum((Decimal(tokens[m]) * Decimal(str(entry["per_million"][m])) for m in VOICE_MODALITIES), Decimal(0)) / Decimal(1_000_000)
        cost_cny = _cost_cny(item, entry.get("cost"), data)
        cost = sum((Decimal(tokens[m]) * cost_cny[m] for m in VOICE_MODALITIES), Decimal(0)) / Decimal(1_000_000) if len(cost_cny) == 4 else None
        return {"credits": _money(credits.quantize(PRECISION)), "cost_credits": _money(cost.quantize(PRECISION)) if cost is not None else None,
                "reason": None, "version": data.get("version")}
    return view(q.credits, q.cost, q.snapshot)


def preview(body: PricingPreview) -> dict:
    item = ItemKey.parse(body.key)
    data = catalogue()
    current = preview_quote(item, body.usage, data)
    draft = None
    if body.sale is not None:
        sale = rules.validate_fragment(item, body.sale, side="sale")
        draft_rule = RuleView(id="draft", key=item.key, revision=0, status="active", sale=sale, cost=None,
                              valid_from=None, valid_until=None, reason="", actor_user_id="", audit_id=None,
                              created_at=datetime.now(timezone.utc))
        draft = preview_quote(item, body.usage, rules.overlay(data, [*rules.current_rules(), draft_rule]))
    return {"key": item.key, "usage": body.usage, "current": current, "draft": draft}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _reject(status: int, code: str, detail: dict | None = None) -> None:
    raise HTTPException(status, detail={"code": code, **(detail or {})})


def _known_item(item: ItemKey, config) -> bool:
    base = base_catalogue()
    if rules.base_entry(base, item) is not None and (item.kind != "video-gen" or rules.item_sale(base, item) is not None
                                                     or rules.item_cost(base, item) is not None):
        return True
    return item.key in _config_items(config)


async def _current(db, key: str) -> PricingRule | None:
    return (await db.scalars(select(PricingRule).where(PricingRule.key == key, PricingRule.superseded_at.is_(None))
                             .order_by(PricingRule.revision.desc()).limit(1))).first()


async def _actor(db, actor_id: str) -> User:
    actor = await db.get(User, actor_id)
    if actor is None or actor.role != "admin" or not actor.is_active or actor.is_deleted:
        _reject(403, "ADMIN_REQUIRED")
    return actor


def _receipt(actor_id: str, key: str, request_key: str) -> str:
    return "audit_" + hashlib.sha256(f"admin-pricing:{actor_id}:{key}:{request_key}".encode()).hexdigest()[:58]


async def _replay(db, receipt_id: str, intent: dict) -> dict | None:
    previous = await db.get(AuditLog, receipt_id)
    if previous is None:
        return None
    if (previous.details or {}).get("request") != intent:
        _reject(409, "IDEMPOTENCY_CONFLICT")
    return {**previous.details["result"], "replayed": True}


async def write_rule(key: str, actor_id: str, body: PricingWrite, request: Request, config) -> dict:
    try:
        item = ItemKey.parse(key)
        sale = rules.validate_fragment(item, body.sale, side="sale")
        cost = rules.validate_fragment(item, body.cost, side="cost")
    except RuleError as exc:
        _reject(422, exc.code, {"message": str(exc)})
    if not _known_item(item, config):
        _reject(404, "UNKNOWN_ITEM")
    if body.status == "disabled" and not body.confirm_disable:
        _reject(422, "CONFIRM_DISABLE")
    if body.status == "active" and sale is None and cost is None:
        _reject(422, "NOTHING_TO_WRITE")
    if body.valid_until is not None and body.valid_until <= datetime.now(timezone.utc):
        _reject(422, "VALID_UNTIL_PAST")
    intent = {"action": "write", "key": item.key, **body.model_dump(mode="json", exclude={"request_key"})}
    receipt_id = _receipt(actor_id, item.key, body.request_key)
    async with get_db_session() as db:
        await _actor(db, actor_id)
        replayed = await _replay(db, receipt_id, intent)
        if replayed is not None:
            return replayed
        current = await _current(db, item.key)
        if (current.revision if current else 0) != body.expected_revision:
            _reject(409, "REVISION_MISMATCH", {"current_revision": current.revision if current else 0})
        # The price that will be in effect after this write, for the below-cost check.
        at = datetime.now(timezone.utc)
        draft = RuleView(id="draft", key=item.key, revision=0, status=body.status, sale=sale, cost=cost,
                         valid_from=None, valid_until=body.valid_until, reason="", actor_user_id="", audit_id=None, created_at=at)
        others = [r for r in rules.current_rules() if r.key != item.key]
        effective = rules.overlay(base_catalogue(), [*others, draft], at=at)
        below = _below_cost(_sale_cny(item, rules.item_sale(effective, item), effective),
                            _cost_cny(item, rules.item_cost(effective, item), effective))
        if below and not body.allow_below_cost:
            _reject(422, "BELOW_COST", {"fields": below})
        rule = PricingRule(
            id=ascending("pricing"), key=item.key, revision=(current.revision if current else 0) + 1,
            status=body.status, sale=sale, cost=cost, valid_from=None, valid_until=body.valid_until,
            reason=body.reason, actor_user_id=actor_id, audit_id=receipt_id, created_at=at,
        )
        if current is not None:
            current.superseded_at = at
        db.add(rule)
        before = RuleView.from_row(current).public() if current else None
        result = {"key": item.key, "rule": RuleView.from_row(rule).public(), "below_cost_fields": below,
                  "effective_within_seconds": EFFECTIVE_WITHIN_SECONDS}
        db.add(AuditLog(id=receipt_id, user_id=actor_id, workspace_id=None, action="admin.pricing.write",
                        resource_type="pricing_rule", resource_id=item.key,
                        details={"request": intent, "before": before, "result": result},
                        ip_address=request.client.host if request.client else None,
                        user_agent=request.headers.get("user-agent"), created_at=at))
    await rules.refresh()
    return result


async def revert_rule(key: str, actor_id: str, body: PricingRevert, request: Request) -> dict:
    try:
        item = ItemKey.parse(key)
    except RuleError as exc:
        _reject(422, exc.code, {"message": str(exc)})
    intent = {"action": "revert", "key": item.key, **body.model_dump(mode="json", exclude={"request_key"})}
    receipt_id = _receipt(actor_id, item.key, body.request_key)
    async with get_db_session() as db:
        await _actor(db, actor_id)
        replayed = await _replay(db, receipt_id, intent)
        if replayed is not None:
            return replayed
        current = await _current(db, item.key)
        if current is None:
            _reject(404, "NO_RULE")
        if current.revision != body.expected_revision:
            _reject(409, "REVISION_MISMATCH", {"current_revision": current.revision})
        at = datetime.now(timezone.utc)
        current.superseded_at = at
        before = RuleView.from_row(current).public()
        result = {"key": item.key, "rule": None, "reverted": before, "effective_within_seconds": EFFECTIVE_WITHIN_SECONDS}
        db.add(AuditLog(id=receipt_id, user_id=actor_id, workspace_id=None, action="admin.pricing.revert",
                        resource_type="pricing_rule", resource_id=item.key,
                        details={"request": intent, "before": before, "result": result},
                        ip_address=request.client.host if request.client else None,
                        user_agent=request.headers.get("user-agent"), created_at=at))
    await rules.refresh()
    return result


async def history(key: str) -> dict:
    try:
        item = ItemKey.parse(key)
    except RuleError as exc:
        _reject(422, exc.code, {"message": str(exc)})
    async with get_db_session() as db:
        rows = (await db.scalars(select(PricingRule).where(PricingRule.key == item.key)
                                 .order_by(PricingRule.revision.desc()))).all()
        audits = (await db.execute(
            select(AuditLog, User).outerjoin(User, User.id == AuditLog.user_id)
            .where(AuditLog.resource_type == "pricing_rule", AuditLog.resource_id == item.key)
            .order_by(AuditLog.created_at.desc()).limit(200))).all()
    return {
        "key": item.key,
        "revisions": [RuleView.from_row(r).public() for r in rows],
        "operations": [{"id": a.id, "action": a.action.removeprefix("admin.pricing."),
                        "actor": {"id": u.id, "username": u.username} if u else {"id": a.user_id},
                        "created_at": a.created_at.replace(tzinfo=timezone.utc).isoformat() if a.created_at.tzinfo is None else a.created_at.isoformat(),
                        "reason": (a.details or {}).get("request", {}).get("reason", ""),
                        "before": (a.details or {}).get("before"), "result": (a.details or {}).get("result")}
                       for a, u in audits],
    }


def export_catalogue() -> dict:
    """The effective price list, loadable through ``BILLING_RATES_FILE``."""
    data = json.loads(json.dumps(catalogue()))
    data.pop("rules_applied", None)
    return data
