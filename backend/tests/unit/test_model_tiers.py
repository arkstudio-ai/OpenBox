"""Composer tier presets: what the config accepts and what /config serves."""
import pytest
from pydantic import ValidationError

from core.config import (
    ModelConfig,
    ModelTiersConfig,
    OpenBoxConfig,
    VideoGenerationConfig,
    VideoModelConfig,
)


def _config(**overrides) -> OpenBoxConfig:
    base = dict(
        model="openai/gemini-3.8-flash",
        models=[
            ModelConfig(id="openai/gemini-3.8-flash"),
            ModelConfig(id="openai/qwen3.8-max"),
            ModelConfig(id="openai/qwen3.8-flash"),
        ],
        video_generation=VideoGenerationConfig(
            model="wan3.0-video",
            default_resolution="720p",
            models=[
                VideoModelConfig(id="video-sd-1080p-pro", channel="sd2", resolutions=["1080p"]),
                VideoModelConfig(id="wan3.0-video", channel="sd2", resolutions=["480p", "720p", "1080p"]),
                VideoModelConfig(id="MiniMax-H3", channel="sd2", resolutions=["480p", "512p", "768p", "2k"]),
            ],
        ),
    )
    base.update(overrides)
    return OpenBoxConfig(**base)


GOOD_TIERS = {
    "chat": [
        {"tier": "high", "model": "openai/qwen3.8-max", "variant": "xhigh"},
        {"tier": "medium", "model": "openai/gemini-3.8-flash", "variant": "medium"},
        {"tier": "low", "model": "openai/qwen3.8-flash", "variant": "low"},
    ],
    "video": [
        {"tier": "high", "model": "video-sd-1080p-pro", "resolution": "1080p"},
        {"tier": "medium", "model": "wan3.0-video", "resolution": "720p"},
        {"tier": "low", "model": "MiniMax-H3", "resolution": "768p"},
    ],
}


def test_declared_tiers_load():
    config = _config(model_tiers=GOOD_TIERS)
    assert [t.tier for t in config.model_tiers.chat] == ["high", "medium", "low"]
    assert config.model_tiers.video[2].resolution == "768p"


def test_empty_is_the_default_and_means_no_tiers():
    assert _config().model_tiers == ModelTiersConfig()


def test_chat_tier_must_name_a_declared_model():
    """A typo here would be a picker entry the prompt route refuses."""
    with pytest.raises(ValidationError, match="not a declared model"):
        _config(model_tiers={"chat": [{"tier": "high", "model": "openai/gpt-9"}]})


def test_video_tier_must_name_a_declared_model_and_one_of_its_resolutions():
    with pytest.raises(ValidationError, match="not a declared video model"):
        _config(model_tiers={"video": [{"tier": "high", "model": "sora-9"}]})
    with pytest.raises(ValidationError, match="it offers"):
        _config(model_tiers={"video": [{"tier": "high", "model": "video-sd-1080p-pro", "resolution": "480p"}]})


def test_video_tier_respects_allowed_models():
    video = VideoGenerationConfig(
        model="wan3.0-video",
        models=[VideoModelConfig(id="wan3.0-video", channel="sd2"), VideoModelConfig(id="MiniMax-H3", channel="sd2")],
        allowed_models=["wan3.0-video"],
    )
    with pytest.raises(ValidationError, match="allowed_models excludes"):
        _config(video_generation=video, model_tiers={"video": [{"tier": "low", "model": "MiniMax-H3"}]})


def test_a_tier_declared_twice_is_refused():
    with pytest.raises(ValidationError, match="declares a tier twice"):
        _config(model_tiers={"chat": [
            {"tier": "high", "model": "openai/qwen3.8-max"},
            {"tier": "high", "model": "openai/qwen3.8-flash"},
        ]})


def test_undeclared_video_catalogue_only_allows_the_single_default():
    video = VideoGenerationConfig(model="doubao-seedance-2-0-260128")
    ok = _config(video_generation=video, model_tiers={"video": [{"tier": "medium", "model": "doubao-seedance-2-0-260128"}]})
    assert ok.model_tiers.video[0].model == "doubao-seedance-2-0-260128"
    with pytest.raises(ValidationError, match="only 'doubao-seedance-2-0-260128' is configured"):
        _config(video_generation=video, model_tiers={"video": [{"tier": "high", "model": "wan3.0-video"}]})


def test_config_route_serves_resolved_tiers():
    from api.metadata import _model_tiers

    served = _model_tiers(_config(model_tiers=GOOD_TIERS))
    assert served["chat"] == [
        {"tier": "high", "model": "openai/qwen3.8-max", "variant": "xhigh"},
        {"tier": "medium", "model": "openai/gemini-3.8-flash", "variant": "medium"},
        {"tier": "low", "model": "openai/qwen3.8-flash", "variant": "low"},
    ]
    assert served["video"] == [
        {"tier": "high", "model": "video-sd-1080p-pro", "resolution": "1080p"},
        {"tier": "medium", "model": "wan3.0-video", "resolution": "720p"},
        {"tier": "low", "model": "MiniMax-H3", "resolution": "768p"},
    ]


def test_config_route_drops_a_variant_the_model_rejects():
    """Qwen 3.8 has no `high`; advertising it would make the tier unsendable."""
    from api.metadata import _model_tiers

    served = _model_tiers(_config(model_tiers={"chat": [
        {"tier": "high", "model": "openai/qwen3.8-max", "variant": "high"},
    ]}))
    assert served["chat"] == [{"tier": "high", "model": "openai/qwen3.8-max", "variant": None}]


def test_config_route_fills_the_default_resolution():
    from api.metadata import _model_tiers

    served = _model_tiers(_config(model_tiers={"video": [
        {"tier": "medium", "model": "wan3.0-video"},
    ]}))
    assert served["video"] == [{"tier": "medium", "model": "wan3.0-video", "resolution": "720p"}]


def test_get_config_includes_tiers(monkeypatch):
    import asyncio

    from api import metadata
    from core import config as config_mod

    monkeypatch.setattr(config_mod, "_config", _config(model_tiers=GOOD_TIERS))
    payload = asyncio.run(metadata.get_config())
    assert payload["model_tiers"]["chat"][0]["model"] == "openai/qwen3.8-max"
    assert payload["model_tiers"]["video"][1]["resolution"] == "720p"
