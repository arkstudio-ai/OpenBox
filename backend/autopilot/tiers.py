"""The three model tiers a template may choose (plan §2.3, decision 2026-09-09).

The template stores a *tier*, never a model id, so pricing or model swaps are a
change here, not a migration of every job. Reference prices come from
`rates.json` through `billing.media.quote_generation`, so they follow ops pricing.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

REFERENCE_SECONDS = 15


@dataclass(frozen=True)
class Tier:
    key: str
    label: str
    model_id: str
    resolution: str
    #: Why a person would pick it.
    pitch: str


TIERS: dict[str, Tier] = {
    "high": Tier("high", "高", "video-sd-720p-proⅠ", "720p", "画面最稳、人物一致性最好；适合要发的成片"),
    "medium": Tier("medium", "中", "wan3.0-video", "720p", "默认档：参数最全（2–30 秒、六种画幅、首尾帧），性价比均衡"),
    "low": Tier("low", "低", "MiniMax-H3", "768p", "最快最便宜；先看效果、跑量试选题"),
}


def tier(key: str) -> Tier:
    try:
        return TIERS[key]
    except KeyError:
        raise KeyError(f"unknown model tier {key!r}; allowed: {', '.join(TIERS)}") from None


def reference_price(key: str, seconds: int = REFERENCE_SECONDS) -> Decimal | None:
    """Credits for `seconds` of generation at this tier, from the live rate card."""
    from billing.media import quote_generation

    t = tier(key)
    return quote_generation(t.model_id, t.resolution, float(seconds)).credits


def minimum_credits_per_video(key: str) -> Decimal:
    """A floor for one video: 15 s of generation + analysis (≈0.15) + composition (0.03).

    Used to reject a template whose cap can never produce a single video.
    Falls back to 1 credit when the tier has no verified price yet.
    """
    gen = reference_price(key) or Decimal("1")
    return (gen + Decimal("0.18")).quantize(Decimal("0.01"))


def describe_tiers() -> list[dict]:
    """For the selection card: label, model, resolution, reference price per 15 s."""
    out = []
    for t in TIERS.values():
        price = reference_price(t.key)
        out.append({"tier": t.key, "label": t.label, "model_id": t.model_id, "resolution": t.resolution,
                    "reference_seconds": REFERENCE_SECONDS, "reference_credits": str(price) if price is not None else None,
                    "pitch": t.pitch})
    return out
