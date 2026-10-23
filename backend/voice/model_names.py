"""Spoken names from deployment presets; execution IDs and stored decisions stay intact."""
import re


def labels(config):
    chat = {row.model: {"low": "快速", "medium": "专业", "high": "深度"}[row.tier]
            for row in config.model_tiers.chat}
    video = {}
    for row in config.model_tiers.video:
        video.setdefault(row.model, {"fast": "快速档", "low": "标准档", "medium": "标准档",
                                     "high": "极致档", "ultra": "极致档"}[row.tier])
    return chat, video


def public_model_names(text: str, config=None) -> str:
    """Sanitize presentation text only, never tool arguments or option identities."""
    if not text:
        return text
    if config is None:
        from core.config import get_config
        config = get_config()
    chat, video = labels(config)
    aliases = {}
    for models, tiers, fallback in ((config.models, chat, "所选任务档位"),
                                    (config.video_generation.models, video, "所选视频档位")):
        for model in models:
            for name in (model.id, model.id.split("/")[-1], model.name):
                if name:
                    aliases[name.casefold()] = tiers.get(model.id, fallback)
    for model, label in ((config.model, chat.get(config.model, "默认任务档位")),
                          (config.video_generation.model, video.get(config.video_generation.model, "默认视频档位"))):
        for name in (model, model.split("/")[-1]):
            if name:
                aliases.setdefault(name.casefold(), label)
    if not aliases:
        return text
    pattern = r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(name) for name in sorted(aliases, key=len, reverse=True)) + r")(?![A-Za-z0-9_])"
    return re.sub(pattern, lambda match: aliases[match.group().casefold()], text, flags=re.IGNORECASE)
