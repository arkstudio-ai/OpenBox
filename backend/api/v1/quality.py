"""``quality`` → composer video tier, for sessions created through ``/v1``.

The three tiers are the web composer's ``model_tiers.video`` presets. The
public contract fixes 1080p for ``high`` and ``medium``; ``low`` takes its
tier's own default (768p on the current preset). This module is the single
place the mapping lives.
"""
from __future__ import annotations

from api.v1.errors import ApiError

QUALITIES = ("high", "medium", "low")
#: 2026-09-22: all three tiers are open. ``low`` renders at 768p (the
#: contract documents it as preview quality); it was held back until the
#: partner test round asked for it.
ACCEPTED_QUALITIES = QUALITIES
DEFAULT_QUALITY = "medium"
FIXED_RESOLUTION = "1080p"


def validate_quality(value: str | None) -> str:
    quality = (value or DEFAULT_QUALITY).strip().lower()
    if quality not in QUALITIES:
        raise ApiError(400, "INVALID_REQUEST", f"quality must be one of {', '.join(QUALITIES)}")
    if quality not in ACCEPTED_QUALITIES:
        raise ApiError(
            400, "INVALID_REQUEST",
            f"quality {quality!r} is not available on this integration; use "
            f"{' or '.join(ACCEPTED_QUALITIES)}",
        )
    return quality


def resolve_quality(quality: str, config) -> tuple[str | None, str | None]:
    """The ``(video_model, video_resolution)`` a tier selects.

    Without tier presets in the config the deployment default model is used
    at the contract's fixed resolution. ``high`` / ``medium`` always pin 1080p
    when the tier offers it; ``low`` takes the tier's own default.
    """
    tiers = {row.tier: row for row in config.model_tiers.video}
    tier = tiers.get(quality)
    if tier is None:
        return None, FIXED_RESOLUTION if quality != "low" else None
    declared = {m.id: m for m in config.video_generation.models}
    entry = declared.get(tier.model)
    offered = list(tier.resolutions) or (list(entry.resolutions) if entry else [])
    if quality == "low":
        resolution = tier.resolution or (
            config.video_generation.default_resolution
            if not offered or config.video_generation.default_resolution in offered
            else offered[0]
        )
    elif not offered or FIXED_RESOLUTION in offered:
        resolution = FIXED_RESOLUTION
    else:
        resolution = tier.resolution or offered[-1]
    return tier.model, resolution
