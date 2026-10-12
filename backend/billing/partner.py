"""Partner (API-key) tariffs: ladder pricing by tier, default resolution and multipliers.

A session created through ``/v1`` carries ``api_key_id``; every charge on such
a session is priced from the workspace's *tariff* instead of the platform
rate card. The tariff says, per video tier, a base price per output second at
a default resolution (three ladder steps) and a multiplier per other
resolution; per text tier, prices per million tokens (three steps); and a
per-second compose price. Thresholds are cumulative per calendar month.

Runtime always charges ladder 1 (the partner contract: 先按第一阶梯预扣, 月末
对账返还). ``month_summary`` recomputes what the month would cost at the
ladder and ``settle_rebate`` posts the difference as a credit once, so a
partner never pays less than its own upstream cost mid-month and the rebate
is an auditable ledger row rather than a moving price.

Cost (what we pay) still comes from the platform rate card, so margin
reports work for partners too.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from billing.pricing import MILLION, PRECISION, Quote
from core.identifier import ascending as generate_id

LADDER_STEPS = 3
VIDEO_KIND = "video_generate"
LLM_KINDS = frozenset({"chat", "compaction", "title", "suggestions", "bash_judge", "cron_summary", "video_analyze"})
_TARIFF_TTL = 30.0
_SESSION_TTL = 120.0


class TariffError(ValueError):
    pass


# ─── Tariff document ───

def default_tariff() -> dict:
    path = Path(__file__).with_name("partner_tariff.default.json")
    return json.loads(path.read_text())


def _money(value: Any, field: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise TariffError(f"{field} must be a decimal string") from exc
    if not amount.is_finite() or amount < 0:
        raise TariffError(f"{field} must be a finite non-negative amount")
    return amount


def _steps(values: Any, field: str) -> list[Decimal]:
    if not isinstance(values, list) or len(values) != LADDER_STEPS:
        raise TariffError(f"{field} needs exactly {LADDER_STEPS} ladder prices")
    return [_money(v, f"{field}[{i}]") for i, v in enumerate(values)]


def validate_tariff(doc: dict) -> dict:
    """Shape-check a tariff document; returns it unchanged (strings stay strings)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("video"), dict) or not isinstance(doc.get("llm"), dict):
        raise TariffError("tariff needs video and llm sections")
    for section in ("video", "llm"):
        ladder = doc[section].get("ladder") or {}
        thresholds = ladder.get("thresholds")
        if (not isinstance(thresholds, list) or len(thresholds) != LADDER_STEPS - 1
                or any(not isinstance(t, int) or t <= 0 for t in thresholds) or thresholds != sorted(thresholds)):
            raise TariffError(f"{section}.ladder.thresholds must be {LADDER_STEPS - 1} ascending positive integers")
        if not isinstance(doc[section].get("tiers"), dict) or not doc[section]["tiers"]:
            raise TariffError(f"{section}.tiers must not be empty")
    for quality, tier in doc["video"]["tiers"].items():
        if not isinstance(tier, dict):
            raise TariffError(f"video tier {quality!r} must be an object")
        _steps(tier.get("base_price"), f"video.{quality}.base_price")
        default = tier.get("default_resolution")
        multipliers = tier.get("multipliers")
        if not default or not isinstance(multipliers, dict) or default not in multipliers:
            raise TariffError(f"video tier {quality!r} needs default_resolution listed in multipliers")
        for res, mult in multipliers.items():
            _money(mult, f"video.{quality}.multipliers[{res}]")
        if Decimal(str(multipliers[default])) != 1:
            raise TariffError(f"video tier {quality!r}: the default resolution's multiplier must be 1")
    for name, tier in doc["llm"]["tiers"].items():
        for field in ("input", "output", "cache_read"):
            _steps((tier or {}).get(field), f"llm.{name}.{field}")
    models = doc["llm"].get("models") or {}
    unknown = [m for m, t in models.items() if t not in doc["llm"]["tiers"]]
    if unknown:
        raise TariffError(f"llm.models map to undeclared tiers: {', '.join(unknown)}")
    compose = doc.get("compose")
    if compose is not None:
        if not isinstance(compose.get("per_second"), dict):
            raise TariffError("compose.per_second must map resolution tiers to prices")
        for res, price in compose["per_second"].items():
            _money(price, f"compose.per_second[{res}]")
        for field in ("min_seconds", "round_seconds"):
            if field in compose and (not isinstance(compose[field], int) or compose[field] < 1):
                raise TariffError(f"compose.{field} must be a positive integer")
    return doc


# ─── Lookup with small caches ───

_tariff_cache: dict[str, tuple[float, dict, int | None]] = {}
_session_cache: dict[str, tuple[float, "PartnerContext | None"]] = {}


def clear_caches() -> None:
    _tariff_cache.clear()
    _session_cache.clear()


async def tariff_for_workspace(workspace_id: str) -> tuple[dict, int | None]:
    """(tariff, revision); revision None = the built-in default applies."""
    hit = _tariff_cache.get(workspace_id)
    if hit and time.monotonic() - hit[0] < _TARIFF_TTL:
        return hit[1], hit[2]
    from db.base import get_db_session
    from db.models.partner import PartnerTariff

    async with get_db_session() as db:
        row = await db.get(PartnerTariff, workspace_id)
    tariff, revision = (row.tariff, row.revision) if row is not None else (default_tariff(), None)
    _tariff_cache[workspace_id] = (time.monotonic(), tariff, revision)
    return tariff, revision


@dataclass(frozen=True)
class PartnerContext:
    workspace_id: str
    api_key_id: str
    quality: str | None
    tariff: dict
    revision: int | None


async def context_for_session(session_id: str | None) -> PartnerContext | None:
    """The partner tariff a session bills against, or None for ordinary sessions."""
    if not session_id:
        return None
    hit = _session_cache.get(session_id)
    if hit and time.monotonic() - hit[0] < _SESSION_TTL:
        return hit[1]
    from db.base import get_db_session
    from db.models.session import Session

    async with get_db_session() as db:
        row = await db.get(Session, session_id)
    context = None
    if row is not None and getattr(row, "api_key_id", None):
        tariff, revision = await tariff_for_workspace(row.workspace_id)
        context = PartnerContext(row.workspace_id, row.api_key_id, getattr(row, "quality", None), tariff, revision)
    _session_cache[session_id] = (time.monotonic(), context)
    return context


# ─── Tier resolution ───

def video_tier(tariff: dict, quality: str | None, model: str | None, config=None) -> tuple[str, dict] | None:
    """Which tariff tier prices this shot: the session's quality, else the
    composer tier that declares the model (a per-shot override)."""
    tiers = tariff.get("video", {}).get("tiers", {})
    if quality and quality in tiers:
        return quality, tiers[quality]
    if model and config is not None:
        for row in getattr(config.model_tiers, "video", None) or []:
            if row.model == model and row.tier in tiers:
                return row.tier, tiers[row.tier]
    return None


def llm_tier(tariff: dict, model_id: str) -> tuple[str, dict] | None:
    models = tariff.get("llm", {}).get("models") or {}
    name = models.get(model_id) or models.get(model_id.rsplit("/", 1)[-1])
    tier = tariff.get("llm", {}).get("tiers", {}).get(name) if name else None
    return (name, tier) if tier else None


# ─── Quotes (ladder 1 at runtime) ───

def quote_video(tariff: dict, quality: str, tier: dict, model: str, resolution: str | None,
                duration_sec: float | None, *, platform=None, ladder: int = 1):
    """A MediaQuote priced from the tariff; cost copied from the platform quote."""
    from billing.media import GENERATION_KIND, MediaQuote

    key = f"video-gen:{model.rsplit('/', 1)[-1]}:{resolution or '?'}"
    base = {"version": f"partner:{tariff.get('version', '?')}", "verified_at": tariff.get("version", ""), "kind": GENERATION_KIND,
            "model": key, "source": "partner tariff"}
    multiplier = (tier.get("multipliers") or {}).get(resolution or "")
    if multiplier is None:
        return MediaQuote(key, resolution or "unpriced", 0, None,
                          {**base, "reason": f"Resolution {resolution or '?'} is not offered by the {quality} tier"})
    if duration_sec is None or duration_sec <= 0:
        return MediaQuote(key, resolution, 0, None, {**base, "reason": "Duration not explicit (smart duration is not priced)"})
    prices = _steps(tier["base_price"], "base_price")
    step = prices[max(0, min(LADDER_STEPS, ladder) - 1)]
    per_second = (step * Decimal(str(multiplier))).quantize(PRECISION)
    seconds = math.ceil(float(duration_sec) - 1e-9)
    credits = (per_second * seconds).quantize(PRECISION)
    snapshot = {**base, "currency": tariff.get("currency", "CNY"), "per_second": str(per_second),
                "seconds_billed": seconds, "duration_sec": float(duration_sec),
                "partner": {"tariff_version": tariff.get("version"), "quality": quality, "resolution": resolution,
                            "default_resolution": tier.get("default_resolution"), "multiplier": str(multiplier),
                            "base_price": str(step), "base_prices": [str(p) for p in prices], "ladder": ladder,
                            "thresholds": list(tariff["video"]["ladder"]["thresholds"])}}
    cost = None
    if platform is not None and platform.snapshot.get("cost") is not None:
        snapshot["cost"] = platform.snapshot["cost"]
        snapshot["cost_credits"] = platform.snapshot.get("cost_credits")
        cost = platform.cost
    return MediaQuote(key, resolution, seconds, credits, snapshot, cost)


async def quote_generation_for_session(session_id: str | None, model: str, resolution: str | None,
                                       duration_sec: float | None):
    """Partner-aware video quote: the tariff for ``/v1`` sessions, else the platform rate card."""
    from billing.media import quote_generation

    platform = quote_generation(model, resolution, duration_sec)
    context = await context_for_session(session_id)
    if context is None:
        return platform
    from core.config import get_config

    picked = video_tier(context.tariff, context.quality, model, get_config())
    if picked is None:
        return platform
    quality, tier = picked
    return quote_video(context.tariff, quality, tier, model, resolution, duration_sec, platform=platform)


def quote_llm(tariff: dict, tier_name: str, tier: dict, usage: dict, *, platform: Quote | None = None,
              ladder: int = 1) -> Quote:
    """Credits for one text call from the tariff's per-million prices (ladder 1 at runtime)."""
    index = max(0, min(LADDER_STEPS, ladder) - 1)
    prices = {field: _steps(tier[field], field)[index] for field in ("input", "output", "cache_read")}
    read = int(usage.get("cache_read", 0))
    counts = {"input": int(usage["input"]) - read, "output": int(usage["output"]), "cache_read": read}
    if any(n < 0 for n in counts.values()):
        raise ValueError("Invalid usage buckets")
    credits = sum((Decimal(n) * prices[k] for k, n in counts.items()), Decimal(0)) / MILLION
    snapshot = {"version": f"partner:{tariff.get('version', '?')}", "verified_at": tariff.get("version", ""),
                "source": "partner tariff", "currency": tariff.get("currency", "CNY"),
                "credits_per_million": {k: str(v) for k, v in prices.items()},
                "partner": {"tariff_version": tariff.get("version"), "tier": tier_name, "ladder": ladder,
                            "thresholds": list(tariff["llm"]["ladder"]["thresholds"])}}
    if platform is not None:
        snapshot["model"] = platform.snapshot.get("model")
        if platform.snapshot.get("cost") is not None:
            snapshot["cost"] = platform.snapshot["cost"]
            snapshot["cost_credits"] = platform.snapshot.get("cost_credits")
    return Quote(credits.quantize(PRECISION), snapshot, platform.cost if platform is not None else None)


def quote_compose_partner(tariff: dict, width: int, height: int, duration_sec: float | None, *, platform=None):
    """Compose at the tariff's per-second price, rounded like the platform (whole minutes by default)."""
    from billing.media import USAGE_KIND, MediaQuote, compose_tier

    compose = tariff.get("compose")
    picked = compose_tier(width, height)
    if compose is None or picked is None:
        return platform
    key, _rate = picked
    tier = key.rsplit("-", 1)[-1]
    per = (compose.get("per_second") or {}).get(tier)
    base = {"version": f"partner:{tariff.get('version', '?')}", "verified_at": tariff.get("version", ""),
            "kind": USAGE_KIND, "model": key, "source": "partner tariff"}
    if per is None:
        return platform
    if duration_sec is None:
        return MediaQuote(key, tier, 0, None, {**base, "reason": "Duration unknown until the shots are trimmed"})
    per_second = _money(per, "compose.per_second")
    step = max(1, int(compose.get("round_seconds", 60)))
    seconds = math.ceil(float(duration_sec) - 1e-9)
    billed = max(int(compose.get("min_seconds", 60)), math.ceil(seconds / step) * step)
    credits = (per_second * billed).quantize(PRECISION)
    snapshot = {**base, "currency": tariff.get("currency", "CNY"), "per_second": str(per_second),
                "min_seconds": int(compose.get("min_seconds", 60)), "round_seconds": step,
                "seconds_billed": billed, "duration_sec": float(duration_sec),
                "partner": {"tariff_version": tariff.get("version"), "tier": tier}}
    cost = None
    if platform is not None and platform.snapshot.get("cost") is not None:
        snapshot["cost"] = platform.snapshot["cost"]
        snapshot["cost_credits"] = platform.snapshot.get("cost_credits")
        cost = platform.cost
    return MediaQuote(key, tier, billed, credits, snapshot, cost)


async def quote_compose_for_session(session_id: str | None, width: int, height: int, duration_sec: float | None):
    from billing.media import quote_compose

    platform = quote_compose(width, height, duration_sec)
    context = await context_for_session(session_id)
    if context is None:
        return platform
    return quote_compose_partner(context.tariff, width, height, duration_sec, platform=platform)


# ─── Monthly ladder settlement ───

def month_bounds(month: str) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise TariffError("month must be YYYY-MM") from exc
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def current_month(at: datetime | None = None) -> str:
    return (at or datetime.now(timezone.utc)).strftime("%Y-%m")


def _naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


async def month_events(workspace_id: str, month: str) -> list:
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.billing import UsageEvent

    start, end = month_bounds(month)
    async with get_db_session() as db:
        rows = list(await db.scalars(
            select(UsageEvent).where(
                UsageEvent.workspace_id == workspace_id,
                UsageEvent.created_at >= _naive(start), UsageEvent.created_at < _naive(end),
                UsageEvent.status.in_(("charged", "shadow")),
            ).order_by(UsageEvent.created_at, UsageEvent.id)
        ))
    return rows


def _split(cumulative: int, quantity: int, thresholds: list[int]) -> list[tuple[int, int]]:
    """(ladder index, units) pieces of ``quantity`` placed after ``cumulative`` units."""
    bounds = [*thresholds, None]
    pieces: list[tuple[int, int]] = []
    position, remaining = cumulative, quantity
    for index, upper in enumerate(bounds):
        if remaining <= 0:
            break
        room = remaining if upper is None else max(0, upper - position)
        take = min(remaining, room)
        if take > 0:
            pieces.append((index, take))
            position += take
            remaining -= take
    return pieces


def summarize(tariff: dict, events: list, month: str) -> dict:
    """What the month cost at ladder 1 (charged) versus at the ladder, per tier and per kind."""
    video_thresholds = [int(t) for t in tariff["video"]["ladder"]["thresholds"]]
    llm_thresholds = [int(t) for t in tariff["llm"]["ladder"]["thresholds"]]
    video_tiers = tariff["video"]["tiers"]
    llm_tiers = tariff["llm"]["tiers"]
    by_quality: dict[str, dict] = {}
    llm = {"tokens": 0, "charged": Decimal(0), "ladder_credits": Decimal(0), "events": 0, "ladder": 1}
    by_kind: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    charged_total = Decimal(0)
    llm_cumulative = 0
    for event in events:
        credits = Decimal(event.credits or 0)
        charged_total += credits
        pricing = event.pricing or {}
        partner = pricing.get("partner") or {}
        day = event.created_at.strftime("%Y-%m-%d")
        external = "chat" if event.kind in LLM_KINDS else event.kind
        kind_row = by_kind.setdefault(external, {"events": 0, "credits": Decimal(0), "quantity": 0})
        day_row = by_day.setdefault(day, {"events": 0, "credits": Decimal(0), "output_seconds": 0})
        kind_row["events"] += 1; kind_row["credits"] += credits
        day_row["events"] += 1; day_row["credits"] += credits
        if event.kind in LLM_KINDS:
            # Every text call counts toward the month's token volume, tariff-priced or not.
            kind_row["quantity"] += int(event.total_tokens or 0)
        elif event.kind == "video_compose":
            kind_row["quantity"] += int((event.tokens or {}).get("seconds_billed") or 0)
        if event.kind == VIDEO_KIND and partner.get("quality") in video_tiers:
            quality = partner["quality"]; tier = video_tiers[quality]
            seconds = int((event.tokens or {}).get("seconds_billed") or 0)
            row = by_quality.setdefault(quality, {"output_seconds": 0, "charged": Decimal(0), "ladder_credits": Decimal(0), "events": 0})
            prices = _steps(tier["base_price"], "base_price")
            multiplier = Decimal(str(partner.get("multiplier", "1")))
            for index, take in _split(row["output_seconds"], seconds, video_thresholds):
                row["ladder_credits"] += (prices[index] * multiplier * take).quantize(PRECISION)
            row["output_seconds"] += seconds; row["charged"] += credits; row["events"] += 1
            kind_row["quantity"] += seconds; day_row["output_seconds"] += seconds
        elif event.kind in LLM_KINDS and partner.get("tier") in llm_tiers:
            tier = llm_tiers[partner["tier"]]
            tokens = event.tokens or {}
            total = int(tokens.get("total") or 0)
            read = int(tokens.get("cache_read") or 0)
            buckets = {"input": int(tokens.get("input", 0)) - read, "output": int(tokens.get("output", 0)), "cache_read": read}
            for index, take in _split(llm_cumulative, total, llm_thresholds):
                share = Decimal(take) / Decimal(total) if total else Decimal(0)
                for field, n in buckets.items():
                    price = _steps(tier[field], field)[index]
                    llm["ladder_credits"] += (Decimal(n) * share * price / MILLION)
            llm_cumulative += total
            llm["tokens"] += total; llm["charged"] += credits; llm["events"] += 1
    llm["ladder_credits"] = llm["ladder_credits"].quantize(PRECISION)
    llm["ladder"] = 1 + sum(1 for t in llm_thresholds if llm_cumulative > t)
    ladder_total = llm["ladder_credits"] + sum((r["ladder_credits"] for r in by_quality.values()), Decimal(0))
    counted = llm["charged"] + sum((r["charged"] for r in by_quality.values()), Decimal(0))
    rebate = max(Decimal(0), (counted - ladder_total).quantize(PRECISION))
    for quality, row in by_quality.items():
        row["ladder"] = 1 + sum(1 for t in video_thresholds if row["output_seconds"] > t)
    return {"month": month, "charged": charged_total, "ladder_eligible_charged": counted, "ladder_credits": ladder_total,
            "rebate": rebate, "by_quality": by_quality, "llm": llm, "by_kind": by_kind, "by_day": by_day}


def ladder_for(tariff: dict, section: str, cumulative: int) -> int:
    thresholds = [int(t) for t in tariff[section]["ladder"]["thresholds"]]
    return 1 + sum(1 for t in thresholds if cumulative > t)


async def month_summary(workspace_id: str, month: str) -> dict:
    tariff, _revision = await tariff_for_workspace(workspace_id)
    return summarize(tariff, await month_events(workspace_id, month), month)


async def settle_rebate(workspace_id: str, month: str, actor_user_id: str) -> dict:
    """Post the month's ladder rebate once; replaying returns the existing settlement."""
    from sqlalchemy import select

    from billing.service import lock_balance, now, post_ledger
    from db.base import get_db_session
    from db.models.partner import PartnerRebate

    if month >= current_month():
        raise TariffError("a month can only be settled after it has ended")
    summary = await month_summary(workspace_id, month)
    async with get_db_session() as db:
        account = await lock_balance(db, workspace_id)
        existing = await db.scalar(select(PartnerRebate).where(
            PartnerRebate.workspace_id == workspace_id, PartnerRebate.month == month))
        if existing is not None:
            return rebate_view(existing, replayed=True)
        row = PartnerRebate(id=generate_id("rebate"), workspace_id=workspace_id, month=month, status="settled",
                            charged=summary["ladder_eligible_charged"], ladder_credits=summary["ladder_credits"],
                            rebate=summary["rebate"], detail=_jsonable(summary), actor_user_id=actor_user_id,
                            created_at=now())
        if summary["rebate"] > 0:
            post_ledger(db, account, amount=summary["rebate"], kind="rebate", reference_id=row.id,
                        key=f"rebate:{workspace_id}:{month}")
            row.ledger_id = f"rebate:{workspace_id}:{month}"
        db.add(row)
    return rebate_view(row)


def rebate_view(row, *, replayed: bool = False) -> dict:
    return {"id": row.id, "month": row.month, "status": row.status, "charged": _jsonable(Decimal(row.charged)),
            "ladder_credits": _jsonable(Decimal(row.ladder_credits)), "rebate": _jsonable(Decimal(row.rebate)),
            "settled_at": row.created_at.isoformat(), "replayed": replayed}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value else "0"
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value
