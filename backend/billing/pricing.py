"""Versioned official prices. All arithmetic stays Decimal until legacy UI boundaries."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

MILLION = Decimal(1_000_000)
PRECISION = Decimal("0.000000000001")


_base_cache: tuple[str, float, dict] | None = None


def base_catalogue() -> dict:
    """The price list that ships with the code (or ``BILLING_RATES_FILE``), without operator rules."""
    global _base_cache
    path = Path(os.environ.get("BILLING_RATES_FILE") or Path(__file__).with_name("rates.json"))
    mtime = path.stat().st_mtime
    if _base_cache is not None and _base_cache[0] == str(path) and _base_cache[1] == mtime:
        return _base_cache[2]
    data = json.loads(path.read_text())
    # Never change the product's credit denomination through a price update.
    if Decimal(data["credits_per_cny"]) != 1:
        raise ValueError("Billing requires 1 CNY = 1 credit")
    _base_cache = (str(path), mtime, data)
    return data


def catalogue(*, at: datetime | None = None) -> dict:
    """The price list in effect: the base file with the operator's current rules laid over it.

    Callers must treat the result as read-only; with no rules it *is* the
    cached base object.
    """
    from billing.rules import current_rules, overlay

    return overlay(base_catalogue(), current_rules(), at=at)


def cost_fx(data: dict, currency: str) -> Decimal:
    """Credits per unit of a cost currency (costs are kept in CNY or USD)."""
    if currency == "CNY":
        return Decimal(1)
    fx = Decimal(data["usd_cny"])
    if not fx.is_finite() or fx <= 0:
        raise ValueError("Invalid billing exchange rate")
    return fx


def cost_snapshot(cost: dict | None, *, extra: dict | None = None) -> dict | None:
    """The part of a ``cost`` block worth keeping on the usage row (never the whole table)."""
    if not isinstance(cost, dict):
        return None
    keep = {k: cost.get(k) for k in ("basis", "channel", "currency", "source", "verified_at", "rule_id") if cost.get(k) is not None}
    return {**keep, **(extra or {})}


def _dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    return vars(value) if hasattr(value, "__dict__") else {}


def normalize_usage(raw: Any) -> dict[str, int]:
    """Inclusive input count; disjoint cache buckets, never add reasoning twice.

    OpenAI/Responses/LiteLLM include cache in input; native Anthropic excludes it.
    `cache_write_1h` and `cache_read_explicit` are subsets of their parent bucket.
    """
    u = _dict(raw)
    details = _dict(u.get("prompt_tokens_details") or u.get("input_tokens_details"))
    creation = _dict(u.get("cache_creation"))

    def number(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, bool) or int(value) != value or int(value) < 0:
            raise ValueError("Invalid token count")
        return int(value)

    read = number(u["cache_read"] if "cache_read" in u else (
        u.get("cache_read_input_tokens") or u.get("prompt_cache_hit_tokens")
        or details.get("cached_tokens") or u.get("cache")))
    write = number(u.get("cache_creation_input_tokens") or details.get("cache_creation_tokens")
                   or details.get("cache_write_tokens") or u.get("cache_write"))
    hour = number(creation.get("ephemeral_1h_input_tokens") or u.get("cache_write_1h"))
    write = max(write, hour + number(creation.get("ephemeral_5m_input_tokens")))
    explicit = number(u.get("cache_read_explicit") or u.get("cache_read_input_tokens"))
    inp = number(u.get("prompt_tokens", u.get("input_tokens", u.get("input", 0))))
    out = number(u.get("completion_tokens", u.get("output_tokens", u.get("output", 0))))
    if "prompt_tokens" not in u and "input" not in u and (
        "cache_read_input_tokens" in u or "cache_creation_input_tokens" in u
    ):
        inp += read + write
    if read + write > inp or hour > write or explicit > read:
        raise ValueError("Cache tokens exceed inclusive input tokens")
    return {"input": inp, "output": out, "total": inp + out, "cache": read + write,
            "cache_read": read, "cache_write": write, "cache_write_1h": hour,
            "cache_read_explicit": explicit}


@dataclass(frozen=True)
class Quote:
    credits: Decimal | None
    snapshot: dict
    #: What the same usage cost us at the recorded basis; None = no basis.
    cost: Decimal | None = None


def quote(model_id: str, usage: dict, *, at: datetime | None = None, rates: dict | None = None) -> Quote:
    data = rates if rates is not None else catalogue()
    model = model_id.rsplit("/", 1)[-1].lower()
    model = data.get("aliases", {}).get(model, model)
    rate = data["models"].get(model)
    snapshot = {"version": data["version"], "verified_at": data["verified_at"], "model": model}
    if rate is None:
        return Quote(None, {**snapshot, "reason": data.get("unpriced", {}).get(model, "Model has no verified price")})
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    if rate.get("valid_until") and at.date().isoformat() > rate["valid_until"]:
        return Quote(None, {**snapshot, "reason": "Published promotional tariff has expired"})
    local = at.astimezone(ZoneInfo("Asia/Shanghai"))
    peak = local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18)
    multiplier = Decimal(rate.get("peak_multiplier", "1")) if peak else Decimal(1)
    long = usage["input"] > rate.get("long_context_above", float("inf"))
    conversion = rate.get("conversion", "official_cny" if rate["currency"] == "CNY" else "usd_cny")
    fx = Decimal(data["usd_cny"]) if conversion == "usd_cny" else Decimal(1)
    if not fx.is_finite() or fx <= 0:
        raise ValueError("Invalid billing exchange rate")
    prices: dict[str, Decimal] = {}
    for key in ("input", "output", "cache_read", "cache_write", "cache_write_1h", "cache_read_explicit"):
        value = rate.get(key)
        if key == "cache_read_explicit":
            value = value or rate.get("cache_read")
        if value is not None:
            price = Decimal(value)
            if not price.is_finite() or price < 0:
                raise ValueError("Invalid model rate")
            prices[key] = price * multiplier * fx * (Decimal("1.5") if key == "output" and long else 2 if long else 1)
    read, write = usage.get("cache_read", 0), usage.get("cache_write", 0)
    hour, explicit = usage.get("cache_write_1h", 0), usage.get("cache_read_explicit", 0)
    counts = {"input": usage["input"] - read - write, "output": usage["output"],
              "cache_read": read - explicit, "cache_write": write - hour,
              "cache_write_1h": hour, "cache_read_explicit": explicit}
    snapshot.update({"source": rate.get("source") or data["sources"][rate["vendor"]],
                     "currency": rate["currency"], "conversion": conversion, "fx": str(fx),
                     "fx_date": data.get("fx_date") if conversion == "usd_cny" else None,
                     "region": rate.get("region"), "long_context": long,
                     "peak": peak if "peak_multiplier" in rate else None,
                     "credits_per_million": {k: str(v) for k, v in prices.items()}})
    if any(count < 0 for count in counts.values()):
        raise ValueError("Invalid usage buckets")
    if rate.get("rule_id"):
        snapshot["rule_id"] = rate["rule_id"]
    cost = _token_cost(rate.get("cost"), counts, data, multiplier=multiplier, long=long)
    if cost is not None:
        snapshot["cost"] = cost[1]
        snapshot["cost_credits"] = str(cost[0])
    if any(count and key not in prices for key, count in counts.items()):
        return Quote(None, {**snapshot, "reason": "Usage contains a token category without a verified price"},
                     cost[0] if cost else None)
    credits = sum((Decimal(n) * prices.get(k, Decimal(0)) for k, n in counts.items()), Decimal(0)) / MILLION
    return Quote(credits.quantize(PRECISION), snapshot, cost[0] if cost else None)


def _token_cost(cost: dict | None, counts: dict[str, int], data: dict, *, multiplier: Decimal,
                long: bool) -> tuple[Decimal, dict] | None:
    """Cost of ``counts`` at the entry's ``cost`` block, with the same peak/long-context rules as the sale."""
    if not isinstance(cost, dict):
        return None
    fx = cost_fx(data, str(cost.get("currency", "CNY")).upper())
    prices: dict[str, Decimal] = {}
    for key in ("input", "output", "cache_read", "cache_write", "cache_write_1h", "cache_read_explicit"):
        value = cost.get(key)
        if key == "cache_read_explicit":
            value = value or cost.get("cache_read")
        if key in ("cache_write", "cache_write_1h") and value is None:
            value = cost.get("cache_write") or cost.get("input")
        if value is not None:
            price = Decimal(str(value))
            if not price.is_finite() or price < 0:
                raise ValueError("Invalid model cost")
            prices[key] = price * multiplier * fx * (Decimal("1.5") if key == "output" and long else 2 if long else 1)
    if any(count and key not in prices for key, count in counts.items()):
        return None
    total = sum((Decimal(n) * prices.get(k, Decimal(0)) for k, n in counts.items()), Decimal(0)) / MILLION
    return total.quantize(PRECISION), cost_snapshot(cost, extra={"credits_per_million": {k: str(v) for k, v in prices.items()}})
