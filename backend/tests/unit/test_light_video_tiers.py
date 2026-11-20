"""Video tiers preserve the selected upstream model and the templates' frames."""
from types import SimpleNamespace

import pytest

from core.config import OpenBoxConfig
from light import silk_road, soda_story, video_tiers
from light.reference_plan import Shot
from tool import video_production, video_providers


@pytest.fixture
def tier_config(monkeypatch):
    config = OpenBoxConfig.model_validate({
        "provider": {
            "test": {"api_key": "test-only", "base_url": "https://video.invalid",
                     "options": {"wire_format": "bossip_videos"}},
        },
        "video_generation": {
            "provider": "test",
            "channel_providers": {"runninghub": "test", "dashscope": "test"},
            "models": [
                {"id": video_tiers.TURBO, "channel": "runninghub", "resolutions": ["480p", "768p"],
                 "duration_range": [5, 15], "supports_first_last_frame": True},
                {"id": "wan3.0-video", "channel": "dashscope", "resolutions": ["720p"],
                 "duration_range": [2, 30], "supports_first_last_frame": True},
                {"id": video_tiers.SEEDANCE, "channel": "ark", "wire_shape": "metadata",
                 "resolutions": ["1080p"], "duration_range": [4, 15]},
            ],
        },
    })
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(video_tiers, "get_config", lambda: config)
    return config


@pytest.mark.parametrize("tier,model,resolution,min_duration,audio", [
    ("fast", "MiniMax-H3-Max-Turbo", "768p", 5, True),
    ("medium", "wan3.0-video", "720p", 2, False),
    ("high", "doubao-seedance-2-0-260128", "1080p", 4, False),
])
def test_both_templates_respect_the_chosen_tier(monkeypatch, tier_config, tier, model, resolution, min_duration, audio):
    assert video_tiers.available_tiers() == ["fast", "medium", "high"]
    assert video_tiers.resolve_tier(tier) == (model, resolution)
    shot = Shot(scene="餐馆", frame="空桌", action="把菜摆上桌", seconds=3)
    soda = soda_story.shot_request("soda", 1, shot, "head", model=model, resolution=resolution)
    monkeypatch.setattr(silk_road, "animate_endpoints", lambda *_: "在同一机位拍一小段动作")
    silk = silk_road.framed_shot_request(
        "silk", 9, SimpleNamespace(world=None),
        {"first_frame": "head", "last_frame": "tail"}, model=model, resolution=resolution,
    )
    assert (silk.duration, silk.generate_audio, silk.ratio) == (min_duration, audio, "adaptive")
    cuts = [
        {"source": f"asset_{index}", "duration_sec": max(12 if index < 8 else min_duration, 2) + .16,
         "frame_anchored": True}
        for index in range(1, 10)
    ]
    timeline = silk_road.cut_specs(cuts, "asset_black")
    assert timeline and all(0.25 <= section.speed <= 4 for section in timeline)
    for request in (soda, silk):
        assert (request.model, request.resolution) == (model, resolution)
        route = video_providers.resolve_route(model, tier_config)
        roles = tuple(item.role for item in request.input_assets)
        video_providers.validate_request(
            route, resolution=resolution, ratio=request.ratio, duration=request.duration,
            generate_audio=request.generate_audio, input_mimes=["image/jpeg"] * len(roles),
            declared=video_providers.declared_model(model, tier_config), roles=roles, prompt=request.prompt,
        )
    if tier == "high":
        width, height = silk_road.SEEDANCE_FRAME_SIZE
        assert 0.4 <= width / height <= 2.5
        route = video_providers.resolve_route(model, tier_config)
        payload = video_production._bossip_video_payload(route, {
            "resolution": resolution, "ratio": "adaptive", "duration": silk.duration,
            "content": [{"type": "text", "text": silk.prompt}, *[
                {"type": "image_url", "image_url": {"url": f"https://example.invalid/{role}.jpg"}, "role": role}
                for role in ("first_frame", "last_frame")
            ]],
        })
        assert payload["model"] == model
        assert [item["role"] for item in payload["metadata"]["content"]] == ["first_frame", "last_frame"]


def test_an_unavailable_model_cannot_be_substituted(tier_config):
    tier_config.video_generation.allowed_models = ["wan3.0-video"]
    assert video_tiers.available_tiers() == ["medium"]
    with pytest.raises(video_tiers.LightError) as error:
        video_tiers.resolve_tier("high")
    assert error.value.code == "LIGHT_VIDEO_TIER_UNAVAILABLE"
