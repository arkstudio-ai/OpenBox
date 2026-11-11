"""你拍我剪 prices: by finished cut, not by IMS minutes (docs/LIGHT_MODE_PLAN.md §5.3, §8 #6).

Two lines on the bill, both quoted before anything is spent:

- **素材分析** — 1 credit per started minute of footage, the whole batch rounded
  once (5 clips of 15 s = 75 s = 2 credits). Charged when the analysis is
  done, before the plan card; it is not refunded if the owner does not cut
  (上游扣了就收).
- **成片** — a ladder on the cut's length: ≤60 s 5, ≤180 s 9, then +2 for
  every further started minute. Replaces the per-minute IMS price for a
  composition made in a 你拍我剪 conversation.

Prices come from ``rates.json`` ``media.footage`` when present; a deployment
that mounts an older rates file (light-test mounts gw2's) gets these defaults.
"""
from __future__ import annotations

import math
from decimal import Decimal

from billing.media import MediaQuote, settle
from billing.pricing import PRECISION, catalogue

ANALYZE_KIND = "footage_analyze"
COMPOSE_KIND = "video_compose"
ENTRY_ID = "footage_edit"
DEFAULTS = {
    "analyze_per_minute": "1",
    "compose_ladder": [{"up_to_sec": 60, "credits": "5"}, {"up_to_sec": 180, "credits": "9"}],
    "compose_per_extra_minute": "2",
    "source": "docs/LIGHT_MODE_PLAN.md §8 #6（2026-10-31 拍板）",
}


def _rates(data: dict | None = None) -> tuple[dict, dict]:
    data = data if data is not None else catalogue()
    configured = (data.get("media") or {}).get("footage")
    return data, ({**DEFAULTS, **configured} if isinstance(configured, dict) else DEFAULTS)


def quote_analysis(total_seconds: float, *, rates: dict | None = None) -> MediaQuote:
    data, footage = _rates(rates)
    minutes = max(1, math.ceil(float(total_seconds) / 60 - 1e-9))
    per_minute = Decimal(str(footage["analyze_per_minute"]))
    credits = (per_minute * minutes).quantize(PRECISION)
    return MediaQuote("footage-analyze", "footage", minutes, credits, {
        "version": data.get("version"), "kind": ANALYZE_KIND, "model": "footage-analyze",
        "per_minute": str(per_minute), "minutes_billed": minutes, "duration_sec": round(float(total_seconds), 2),
        "source": footage.get("source"),
    })


def quote_compose(duration_sec: float, *, rates: dict | None = None) -> MediaQuote:
    data, footage = _rates(rates)
    seconds = max(0.0, float(duration_sec))
    ladder = sorted(footage["compose_ladder"], key=lambda step: step["up_to_sec"])
    credits = None
    for step in ladder:
        if seconds <= float(step["up_to_sec"]) + 1e-9:
            credits = Decimal(str(step["credits"]))
            break
    if credits is None:
        top = ladder[-1]
        extra = math.ceil((seconds - float(top["up_to_sec"])) / 60 - 1e-9)
        credits = Decimal(str(top["credits"])) + Decimal(str(footage["compose_per_extra_minute"])) * extra
    credits = credits.quantize(PRECISION)
    return MediaQuote("footage-compose", "footage", math.ceil(seconds), credits, {
        "version": data.get("version"), "kind": COMPOSE_KIND, "model": "footage-compose",
        "duration_sec": round(seconds, 2), "ladder": ladder,
        "per_extra_minute": str(footage["compose_per_extra_minute"]), "source": footage.get("source"),
    })


async def is_footage_session(session_id: str | None) -> bool:
    if not session_id:
        return False
    from db.base import get_db_session
    from db.models.session import Session
    async with get_db_session() as db:
        session = await db.get(Session, session_id)
    return bool(session is not None and getattr(session, "light_entry", None) == ENTRY_ID)


async def settle_analysis(*, key: str, workspace_id: str, user_id: str, session_id: str | None,
                          total_seconds: float, clips: int):
    price = quote_analysis(total_seconds)
    return await settle(key=key, workspace_id=workspace_id, user_id=user_id, session_id=session_id, price=price,
                        kind=ANALYZE_KIND, quantity_known=total_seconds > 0,
                        tokens={"duration_sec": round(total_seconds, 2), "minutes_billed": price.minutes_billed,
                                "clips": clips},
                        default_title="素材分析", title="素材分析")
