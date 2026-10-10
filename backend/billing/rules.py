"""Operator pricing rules: a database layer over ``rates.json``.

``rates.json`` stays the base price list that ships with the code. A
``PricingRule`` row replaces (or disables) one billable item's price without a
release. ``overlay()`` merges the current rules into the base catalogue so
every caller of ``billing.pricing.catalogue()`` keeps reading the shape it
always read — the rules only change the numbers.

Keys name one billable item each and match ``usage_events.model_id`` so that
history aggregates by the same key the operator prices:

    llm:<model>                    data["models"][model]
    video-gen:<model>:<res>        media["video-gen"][model]["per_second"][res]
    image-gen:<model>              media["image-gen"][model]
    stt:<model>                    media["stt"][model]
    voice-realtime:<model>         media["voice-realtime"][model]
    ims-compose:<tier>             media["ims-compose-<tier>"]
    hot-trends:<source>            media["hot-trends"][source]

``catalogue()`` is synchronous and called on every quote, so the rules it
sees live in a process-local cache: loaded at startup, replaced right after an
operator write in this process, and re-read every ``REFRESH_SECONDS`` by a
background task so other processes converge within that window.
"""
from __future__ import annotations

import asyncio
import copy
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

REFRESH_SECONDS = 30
LLM_FIELDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h", "cache_read_explicit")
VOICE_MODALITIES = ("input_text", "input_audio", "output_text", "output_audio")
KINDS = ("llm", "video-gen", "image-gen", "stt", "voice-realtime", "ims-compose", "hot-trends")
_KEY = re.compile(r"^(llm|image-gen|stt|voice-realtime|ims-compose|hot-trends):([^:]{1,120})$|^(video-gen):([^:]{1,120}):([^:]{1,16})$")


class RuleError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ItemKey:
    kind: str
    model: str
    resolution: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.model}:{self.resolution}" if self.resolution else f"{self.kind}:{self.model}"

    @classmethod
    def parse(cls, key: str) -> "ItemKey":
        m = _KEY.match(key or "")
        if m is None:
            raise RuleError("INVALID_KEY", f"Unknown pricing key {key!r}")
        if m.group(3):
            return cls(m.group(3), m.group(4), m.group(5))
        return cls(m.group(1), m.group(2))

    @classmethod
    def for_usage(cls, kind: str, model_id: str) -> "ItemKey | None":
        """The key a ``usage_events`` row aggregates under, or None when it has none."""
        if kind == "voice_call":
            return cls("voice-realtime", model_id)
        if kind == "video_compose":
            return cls("ims-compose", model_id.removeprefix("ims-compose-"))
        if ":" in model_id and model_id.split(":", 1)[0] in ("video-gen", "image-gen", "stt", "hot-trends"):
            try:
                return cls.parse(model_id)
            except RuleError:
                return None
        return cls("llm", model_id.rsplit("/", 1)[-1].lower())


# ---------------------------------------------------------------------------
# Price fragments: validation and the place each key lives in the catalogue
# ---------------------------------------------------------------------------

def _money(value: Any, *, field_name: str) -> str:
    if isinstance(value, (float, bool)) or value is None:
        raise RuleError("INVALID_PRICE", f"{field_name}: send prices as decimal strings")
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        raise RuleError("INVALID_PRICE", f"{field_name}: not a number") from None
    if not price.is_finite() or price < 0 or price > Decimal("1000000"):
        raise RuleError("INVALID_PRICE", f"{field_name}: out of range")
    return format(price.normalize(), "f")


def validate_fragment(item: ItemKey, fragment: dict | None, *, side: str) -> dict | None:
    """Normalize a sale/cost fragment for ``item``; prices become canonical decimal strings.

    Sale fragments carry only prices (credits = CNY). Cost fragments may add
    ``basis``, ``channel``, ``source``, ``verified_at``, ``currency`` (CNY or
    USD) and, for video, ``duration_bands``.
    """
    if fragment is None:
        return None
    if not isinstance(fragment, dict):
        raise RuleError("INVALID_PRICE", f"{side} must be an object")
    out: dict[str, Any] = {}
    kind = item.kind
    if kind == "llm":
        for name in LLM_FIELDS:
            if name in fragment and fragment[name] is not None:
                out[name] = _money(fragment[name], field_name=name)
        if "input" not in out or "output" not in out:
            raise RuleError("INVALID_PRICE", "llm prices need at least input and output")
    elif kind == "video-gen":
        out["per_second"] = _money(fragment.get("per_second"), field_name="per_second")
    elif kind == "image-gen":
        out["per_image"] = _money(fragment.get("per_image"), field_name="per_image")
    elif kind in ("stt", "ims-compose"):
        out["per_minute"] = _money(fragment.get("per_minute"), field_name="per_minute")
        minutes = fragment.get("min_minutes", 1)
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0 or minutes > 60:
            raise RuleError("INVALID_PRICE", "min_minutes must be a whole number of minutes")
        out["min_minutes"] = minutes
    elif kind == "voice-realtime":
        per = fragment.get("per_million")
        if not isinstance(per, dict):
            raise RuleError("INVALID_PRICE", "voice prices need per_million by modality")
        out["per_million"] = {m: _money(per.get(m), field_name=m) for m in VOICE_MODALITIES}
    elif kind == "hot-trends":
        out["per_fetch"] = _money(fragment.get("per_fetch"), field_name="per_fetch")
    if side == "cost":
        currency = str(fragment.get("currency") or "CNY").upper()
        if currency not in ("CNY", "USD"):
            raise RuleError("INVALID_PRICE", "cost currency must be CNY or USD")
        out["currency"] = currency
        for name in ("basis", "channel", "source", "verified_at"):
            value = fragment.get(name)
            if value is not None:
                if not isinstance(value, str) or len(value) > 500:
                    raise RuleError("INVALID_PRICE", f"{name} must be a short string")
                out[name] = value
        bands = fragment.get("duration_bands")
        if bands is not None:
            if kind != "video-gen" or not isinstance(bands, list):
                raise RuleError("INVALID_PRICE", "duration_bands only apply to video-gen cost")
            out["duration_bands"] = [
                {"above_seconds": int(b["above_seconds"]), "multiplier": _money(b["multiplier"], field_name="multiplier")}
                for b in bands
            ]
    return out


def _media_entry(data: dict, item: ItemKey, *, create: bool) -> dict | None:
    media = data.setdefault("media", {}) if create else data.get("media", {})
    if item.kind == "ims-compose":
        name = f"ims-compose-{item.model}"
        if create:
            return media.setdefault(name, {"vendor": "admin", "currency": "CNY", "min_minutes": 1,
                                           "short_side_max": _SHORT_SIDE.get(item.model, 0)})
        return media.get(name)
    table = media.setdefault(item.kind, {}) if create else media.get(item.kind, {})
    if create:
        return table.setdefault(item.model, {"vendor": "admin", "currency": "CNY"})
    entry = table.get(item.model)
    return entry if isinstance(entry, dict) else None


_SHORT_SIDE = {"480p": 480, "720p": 720, "1080p": 1080, "2k": 1440, "4k": 2160}


def base_entry(data: dict, item: ItemKey) -> dict | None:
    """The rates.json entry an item lives in (after aliases, for LLMs)."""
    if item.kind == "llm":
        model = data.get("aliases", {}).get(item.model, item.model)
        return data.get("models", {}).get(model)
    return _media_entry(data, item, create=False)


def item_sale(data: dict, item: ItemKey) -> dict | None:
    """The sale fragment currently in effect for ``item`` inside ``data`` (None = unpriced)."""
    entry = base_entry(data, item)
    if entry is None:
        return None
    if item.kind == "llm":
        return {name: entry[name] for name in LLM_FIELDS if entry.get(name) is not None} | {
            "currency": entry.get("currency", "CNY"), "conversion": entry.get("conversion"),
            "valid_until": entry.get("valid_until"), "peak_multiplier": entry.get("peak_multiplier"),
            "long_context_above": entry.get("long_context_above")}
    if item.kind == "video-gen":
        per = (entry.get("per_second") or {}).get(item.resolution)
        return None if per is None else {"per_second": per}
    if item.kind == "image-gen":
        return {"per_image": entry["per_image"]}
    if item.kind in ("stt", "ims-compose"):
        return {"per_minute": entry["per_minute"], "min_minutes": entry.get("min_minutes", 1)}
    if item.kind == "voice-realtime":
        return {"per_million": dict(entry["per_million"])}
    if item.kind == "hot-trends":
        return {"per_fetch": entry["per_fetch"]}
    return None


def item_cost(data: dict, item: ItemKey) -> dict | None:
    """The cost fragment for ``item``: from the entry's ``cost`` block (per resolution for video)."""
    entry = base_entry(data, item)
    cost = (entry or {}).get("cost")
    if not isinstance(cost, dict):
        return None
    if item.kind == "video-gen":
        per = (cost.get("per_second") or {}).get(item.resolution)
        if per is None:
            return None
        return {**{k: v for k, v in cost.items() if k != "per_second"}, "per_second": per}
    return dict(cost)


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------

def _apply_sale(data: dict, item: ItemKey, sale: dict, rule_id: str) -> None:
    if item.kind == "llm":
        models = data.setdefault("models", {})
        # Price the alias itself: the row must stop following its target.
        data.get("aliases", {}).pop(item.model, None)
        base = models.get(item.model) or {"vendor": "admin"}
        entry = {k: v for k, v in base.items() if k not in LLM_FIELDS and k not in ("valid_until", "conversion")}
        entry.update({name: sale[name] for name in LLM_FIELDS if name in sale})
        entry.update({"currency": "CNY", "conversion": "official_cny",
                      "source": base.get("source") or f"admin:{rule_id}", "rule_id": rule_id})
        models[item.model] = entry
        data.get("unpriced", {}).pop(item.model, None)
        return
    entry = _media_entry(data, item, create=True)
    if item.kind == "video-gen":
        entry.setdefault("per_second", {})[item.resolution] = sale["per_second"]
        entry.setdefault("rule_ids", {})[item.resolution] = rule_id
    else:
        entry.update({k: v for k, v in sale.items()})
        entry["rule_id"] = rule_id
    entry["currency"] = "CNY"
    entry.setdefault("source", f"admin:{rule_id}")


def _apply_cost(data: dict, item: ItemKey, cost: dict, rule_id: str) -> None:
    if item.kind == "llm":
        entry = data.setdefault("models", {}).get(data.get("aliases", {}).get(item.model, item.model))
        if entry is None:
            return
        entry["cost"] = {**cost, "rule_id": rule_id}
        return
    entry = _media_entry(data, item, create=False)
    if entry is None:
        return
    if item.kind == "video-gen":
        block = entry.setdefault("cost", {"currency": cost.get("currency", "CNY")})
        for name in ("basis", "channel", "source", "verified_at", "currency", "duration_bands"):
            if name in cost:
                block[name] = cost[name]
        block.setdefault("per_second", {})[item.resolution] = cost["per_second"]
        block.setdefault("rule_ids", {})[item.resolution] = rule_id
    else:
        entry["cost"] = {**cost, "rule_id": rule_id}


def _apply_disable(data: dict, item: ItemKey, rule_id: str) -> None:
    reason = f"Disabled by operator rule {rule_id}"
    if item.kind == "llm":
        data.get("aliases", {}).pop(item.model, None)
        data.get("models", {}).pop(item.model, None)
        data.setdefault("unpriced", {})[item.model] = reason
        return
    entry = _media_entry(data, item, create=False)
    if entry is None:
        return
    if item.kind == "video-gen":
        entry.get("per_second", {}).pop(item.resolution, None)
        if not entry.get("per_second"):
            data["media"]["video-gen"].pop(item.model, None)
        return
    if item.kind == "ims-compose":
        data["media"].pop(f"ims-compose-{item.model}", None)
    else:
        data["media"][item.kind].pop(item.model, None)


def _in_effect(rule: "RuleView", at: datetime) -> bool:
    if rule.valid_from and at < rule.valid_from:
        return False
    if rule.valid_until and at >= rule.valid_until:
        return False
    return True


def overlay(base: dict, rules: list["RuleView"], *, at: datetime | None = None) -> dict:
    """``base`` with every in-effect rule applied. ``base`` is never mutated."""
    if not rules:
        return base
    at = at or datetime.now(timezone.utc)
    data = copy.deepcopy(base)
    applied: list[str] = []
    for rule in rules:
        if not _in_effect(rule, at):
            continue
        item = ItemKey.parse(rule.key)
        if rule.status == "disabled":
            _apply_disable(data, item, rule.id)
        else:
            if rule.sale is not None:
                _apply_sale(data, item, rule.sale, rule.id)
            if rule.cost is not None:
                _apply_cost(data, item, rule.cost, rule.id)
        applied.append(rule.id)
    if applied:
        data["version"] = f"{base['version']}+db{max(applied)[-8:]}"
        data["rules_applied"] = applied
    return data


# ---------------------------------------------------------------------------
# Process-local cache of the current rules
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuleView:
    id: str
    key: str
    revision: int
    status: str
    sale: dict | None
    cost: dict | None
    valid_from: datetime | None
    valid_until: datetime | None
    reason: str
    actor_user_id: str
    audit_id: str | None
    created_at: datetime
    superseded_at: datetime | None = None

    @classmethod
    def from_row(cls, row) -> "RuleView":
        def aware(value):
            return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value
        return cls(id=row.id, key=row.key, revision=row.revision, status=row.status,
                   sale=row.sale, cost=row.cost, valid_from=aware(row.valid_from), valid_until=aware(row.valid_until),
                   reason=row.reason, actor_user_id=row.actor_user_id, audit_id=row.audit_id,
                   created_at=aware(row.created_at), superseded_at=aware(row.superseded_at))

    def public(self) -> dict:
        return {
            "id": self.id, "key": self.key, "revision": self.revision, "status": self.status,
            "sale": self.sale, "cost": self.cost,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "reason": self.reason, "actor_user_id": self.actor_user_id, "audit_id": self.audit_id,
            "created_at": self.created_at.isoformat(),
            "superseded_at": self.superseded_at.isoformat() if self.superseded_at else None,
        }


@dataclass
class _Cache:
    rules: list[RuleView] = field(default_factory=list)
    loaded_at: datetime | None = None
    task: asyncio.Task | None = None


_cache = _Cache()


def current_rules() -> list[RuleView]:
    """The rules ``catalogue()`` overlays right now (empty until ``refresh()`` ran)."""
    return list(_cache.rules)


def loaded_at() -> datetime | None:
    return _cache.loaded_at


def _set(rules: list[RuleView]) -> None:
    _cache.rules = sorted(rules, key=lambda r: (r.key, r.revision))
    _cache.loaded_at = datetime.now(timezone.utc)


def clear() -> None:
    _cache.rules, _cache.loaded_at = [], None


async def refresh() -> list[RuleView]:
    """Re-read the current (un-superseded) rules from the database into the cache."""
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.billing import PricingRule

    async with get_db_session() as db:
        rows = (await db.scalars(select(PricingRule).where(PricingRule.superseded_at.is_(None)))).all()
    _set([RuleView.from_row(row) for row in rows])
    return current_rules()


async def _refresh_forever(interval: float) -> None:
    from core.log import create_logger
    log = create_logger("billing.rules")
    while True:
        try:
            await asyncio.sleep(interval)
            await refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let a transient DB error stop the loop
            log.warning("pricing rules refresh failed: %s", type(exc).__name__)


async def start(interval: float = REFRESH_SECONDS) -> None:
    """Load once now and keep the cache fresh in the background."""
    await refresh()
    if _cache.task is None or _cache.task.done():
        _cache.task = asyncio.create_task(_refresh_forever(interval))


async def stop() -> None:
    if _cache.task is not None:
        _cache.task.cancel()
        try:
            await _cache.task
        except (asyncio.CancelledError, Exception):
            pass
        _cache.task = None
