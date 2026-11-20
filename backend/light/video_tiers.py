"""The three model choices for guided video production."""
from typing import Literal

from core.config import OpenBoxConfig, get_config
from light.mode import LightError
from tool.video_providers import declared_model, resolve_route
from tool.video_runninghub import MODEL as TURBO

VideoTier = Literal["fast", "medium", "high"]
SEEDANCE = "doubao-seedance-2-0-260128"
ROUTES: dict[VideoTier, tuple[str, str]] = {
    "fast": (TURBO, "768p"),
    "medium": ("wan3.0-video", "720p"),
    "high": (SEEDANCE, "1080p"),
}


def _available(model: str, resolution: str, config: OpenBoxConfig) -> bool:
    entry = declared_model(model, config)
    if not entry or not entry.supports_first_last_frame or not entry.supports_generated_audio:
        return False
    if entry.resolutions and resolution not in entry.resolutions:
        return False
    if entry.ratios and not {"adaptive", "9:16"}.issubset(entry.ratios):
        return False
    try:
        route = resolve_route(model, config)
    except RuntimeError:
        return False
    if model == TURBO:
        return route.channel == "runninghub"
    if model == SEEDANCE:
        if route.channel == "ark" and route.wire_format != "bossip_videos":
            return True
        return route.channel in {"ark", "sd2"} and entry.wire_shape == "metadata"
    return route.channel in {"dashscope", "task"} or (route.channel == "sd2" and entry.wire_shape == "metadata")


def available_tiers(config: OpenBoxConfig | None = None) -> list[VideoTier]:
    config = config or get_config()
    return [tier for tier, (model, resolution) in ROUTES.items() if _available(model, resolution, config)]


def resolve_tier(tier: VideoTier) -> tuple[str, str]:
    model, resolution = ROUTES[tier]
    if not _available(model, resolution, get_config()):
        raise LightError("LIGHT_VIDEO_TIER_UNAVAILABLE", "The selected video model is unavailable.", 409)
    return model, resolution
