"""The three model choices for guided video production."""
from typing import Literal

from core.config import OpenBoxConfig, get_config
from light.mode import LightError
from tool.video_comfyui import MODEL as COMFYUI_H3
from tool.video_providers import declared_model, resolve_route
from tool.video_runninghub import MODEL as TURBO

VideoTier = Literal["fast", "medium", "high"]
SEEDANCE = "doubao-seedance-2-0-260128"
ROUTES: dict[VideoTier, tuple[str, str]] = {
    "fast": (TURBO, "768p"),
    "medium": ("wan3.0-video", "720p"),
    "high": (SEEDANCE, "1080p"),
}
# These established templates build adaptive or ultra-wide provider requests.
COMFYUI_INCOMPATIBLE_TEMPLATES = {"silk_road", "soda_story"}


def _available(model: str, resolution: str, config: OpenBoxConfig) -> bool:
    entry = declared_model(model, config)
    if not entry or not entry.supports_first_last_frame or not entry.supports_generated_audio:
        return False
    if entry.resolutions and resolution not in entry.resolutions:
        return False
    required_ratios = {"9:16"} if model == COMFYUI_H3 else {"adaptive", "9:16"}
    if entry.ratios and not required_ratios.issubset(entry.ratios):
        return False
    try:
        route = resolve_route(model, config)
    except RuntimeError:
        return False
    if not route.allow_submit:
        return False
    if model == COMFYUI_H3:
        return route.channel == "comfyui"
    if model == TURBO:
        return route.channel == "runninghub"
    if model == SEEDANCE:
        if route.channel == "ark" and route.wire_format != "bossip_videos":
            return True
        return route.channel in {"ark", "sd2"} and entry.wire_shape == "metadata"
    return route.channel in {"dashscope", "task"} or (route.channel == "sd2" and entry.wire_shape == "metadata")


def comfyui_fast_available(config: OpenBoxConfig) -> bool:
    return _comfy_resolution(config) is not None


def _comfy_resolution(config: OpenBoxConfig) -> str | None:
    for row in config.model_tiers.video:
        if row.tier == "fast" and row.model == COMFYUI_H3:
            resolution = row.resolution or config.video_generation.default_resolution
            if _available(COMFYUI_H3, resolution, config):
                return resolution
    return None


def workbench_fast_available(config: OpenBoxConfig) -> bool:
    provider = config.provider.get("comfyui-local")
    return bool(provider and provider.options.get("workers") and comfyui_fast_available(config))


def _choice(tier: VideoTier, config: OpenBoxConfig) -> tuple[str, str]:
    if tier == "fast" and (resolution := _comfy_resolution(config)):
        return COMFYUI_H3, resolution
    return ROUTES[tier]


def available_tiers(config: OpenBoxConfig | None = None, *, template: str = "") -> list[VideoTier]:
    config = config or get_config()
    available = []
    for tier in ROUTES:
        model, resolution = _choice(tier, config)
        if model == COMFYUI_H3 and template in COMFYUI_INCOMPATIBLE_TEMPLATES and not workbench_fast_available(config):
            continue
        if _available(model, resolution, config):
            available.append(tier)
    return available


def resolve_tier(tier: VideoTier) -> tuple[str, str]:
    config = get_config()
    model, resolution = _choice(tier, config)
    if not _available(model, resolution, config):
        raise LightError("LIGHT_VIDEO_TIER_UNAVAILABLE", "The selected video model is unavailable.", 409)
    return model, resolution
