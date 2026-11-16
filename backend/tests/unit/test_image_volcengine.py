"""Volcengine Ark Seedream behind image_gen: request shape, limits, price tier.

No network. The limits and the request shape were measured live on 2026-11-16
against doubao-seedream-5-0-pro-260628 (docs/VOLCENGINE_IMAGE_CHANNEL.md).
"""
import struct
from decimal import Decimal
from types import SimpleNamespace

import pytest

from billing import media
from tool import image_volcengine as volc
from tool.image_gen import ImageGenArgs, InputImage, ProviderTarget, StoredImage, _call_provider, execute
from tool.tool import ToolContext

MODEL = "doubao-seedream-5-0-pro-260628"
TARGET = ProviderTarget("volcengine", MODEL, "secret", "https://ark.test/api/v3", 600)


def _png(width: int, height: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + b"\x00" * 4)


def test_only_seedream_model_ids_take_this_path():
    assert volc.is_model(MODEL) and volc.is_model("Doubao-Seedream-5-5-261028")
    assert not volc.is_model("seedream-v5-pro")  # RunningHub's id for the same model
    assert not volc.is_model("gpt-image-2")


def test_sizes_resolve_to_exact_pixels_inside_arks_area():
    assert volc.plan_request(size="1024x1536") == volc.PlannedRequest("1024x1536", "1k")
    assert volc.plan_request(size="auto") == volc.PlannedRequest("2048x2048", "2k")
    # Editing with "auto" keeps the source's shape at a 2048 long edge.
    assert volc.plan_request(size="auto", input_size=(1792, 2400)).size == "1528x2048"

    big = volc.plan_request(size="2048x2896")  # the A4 poster size that RunningHub refused
    width, height = map(int, big.size.split("x"))
    assert width * height <= volc.MAX_PIXELS and abs(width / height - 2048 / 2896) < 0.01
    assert "above Seedream's limit" in big.note and big.tier == "2k"

    small = volc.plan_request(size="512x512")
    width, height = map(int, small.size.split("x"))
    assert width * height >= volc.MIN_PIXELS and width == height
    assert "below Seedream's minimum" in small.note and small.tier == "1k"

    with pytest.raises(volc.ImageRequestRefused):
        volc.plan_request(size="large")


def test_the_price_tier_follows_output_pixels():
    assert volc.plan_request(size="1600x1600").tier == "1k"   # 2.56 MP
    assert volc.plan_request(size="1624x1624").tier == "2k"   # 2.64 MP


def test_requests_ark_cannot_take_are_refused_before_any_call():
    ok = dict(n=1, mask=False, background=None, input_count=0, output_format="png")
    assert volc.unsupported(**ok) is None
    assert "one image per call" in volc.unsupported(**{**ok, "n": 2})
    assert "no mask" in volc.unsupported(**{**ok, "mask": True, "input_count": 1})
    assert "png or jpeg" in volc.unsupported(**{**ok, "output_format": "webp"})
    assert "exactly one input image" in volc.unsupported(**{**ok, "background": "transparent"})
    assert volc.unsupported(**{**ok, "background": "transparent", "input_count": 1}) is None
    assert "at most 10" in volc.unsupported(**{**ok, "input_count": 11})


def _fake_openai(monkeypatch, observed):
    import openai

    class FakeImages:
        async def generate(self, **kwargs):
            observed["generate"] = kwargs
            return SimpleNamespace(data=[SimpleNamespace(b64_json=None, url="https://tos.test/out.png")])

        async def edit(self, **kwargs):  # pragma: no cover - Ark has no /images/edits
            raise AssertionError(f"unexpected edit call: {kwargs}")

    class FakeClient:
        def __init__(self, **kwargs):
            self.images = FakeImages()

        async def close(self):
            pass

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)

    async def fake_download(url):
        observed["downloaded"] = url
        return _png(1024, 1536)

    import tool.image_gen as image_mod
    monkeypatch.setattr(image_mod, "_download_generated_url", fake_download)


@pytest.mark.asyncio
async def test_generation_sends_only_arks_parameters_and_turns_the_watermark_off(monkeypatch):
    observed = {}
    _fake_openai(monkeypatch, observed)

    outputs = await _call_provider(
        TARGET, ImageGenArgs(prompt="a poster"), size="1024x1536", quality="auto",
        output_format="png", images=[], mask=None, image_urls=[],
    )

    assert outputs == [_png(1024, 1536)] and observed["downloaded"] == "https://tos.test/out.png"
    sent = observed["generate"]
    assert sent == {"model": MODEL, "prompt": "a poster", "size": "1024x1536", "output_format": "png",
                    "extra_body": {"watermark": False}}


@pytest.mark.asyncio
async def test_editing_uses_the_generations_endpoint_with_signed_input_urls(monkeypatch):
    observed = {}
    _fake_openai(monkeypatch, observed)
    source = InputImage("asset_1", "dish.png", "image/png", _png(1792, 2400), "users/u/dish.png")

    await _call_provider(
        TARGET, ImageGenArgs(prompt="night mood", input_images=["asset_1"], background="transparent"),
        size="1528x2048", quality="auto", output_format="png", images=[source], mask=None,
        image_urls=["https://oss.test/signed-dish"],
    )

    sent = observed["generate"]
    assert sent["extra_body"] == {"watermark": False, "image": ["https://oss.test/signed-dish"]}
    assert sent["background"] == "transparent" and sent["size"] == "1528x2048"
    assert "n" not in sent and "quality" not in sent


def _execute_env(monkeypatch, observed):
    import core.oss
    import tool.image_gen as image_mod

    settings = SimpleNamespace(default_size="auto", default_quality="medium", output_format="png")
    monkeypatch.setattr(image_mod, "_configured_target", lambda: (TARGET, settings))
    oss = SimpleNamespace(presign_get=lambda key, expires_sec: f"https://oss.test/{key}?ttl={expires_sec}")
    monkeypatch.setattr(core.oss, "get_oss", lambda: oss)

    async def fake_inputs(refs, mask, ctx, selected_oss):
        return [InputImage(ref, f"{ref}.png", "image/png", _png(1792, 2400), f"users/u/{ref}.png")
                for ref in refs], None

    async def fake_provider(*_args, **kwargs):
        observed["provider"] = kwargs
        return [_png(1024, 1024)]

    async def fake_store(*_args, **_kwargs):
        return StoredImage("asset_out", "out.png", "image/png", 12, "/workspace/generated_images/out.png")

    async def fake_settle(**kwargs):
        observed["settle"] = kwargs
        return Decimal("0.60")

    monkeypatch.setattr(image_mod, "_load_inputs", fake_inputs)
    monkeypatch.setattr(image_mod, "_call_provider", fake_provider)
    monkeypatch.setattr(image_mod, "_store_output", fake_store)
    monkeypatch.setattr(media, "settle_image", fake_settle)


@pytest.mark.asyncio
async def test_execute_plans_the_size_signs_inputs_and_bills_the_pixel_tier(monkeypatch):
    observed = {}
    _execute_env(monkeypatch, observed)
    ctx = ToolContext(user_id="u", session_id="s", workspace_id="w", part_id="part_1")

    result = await execute(ImageGenArgs(prompt="night mood", input_images=["asset_1"]), ctx)

    assert result.metadata["mode"] == "edit", result.output
    assert observed["provider"]["size"] == "1528x2048"
    assert observed["provider"]["image_urls"] == [
        f"https://oss.test/users/u/asset_1.png?ttl={volc.INPUT_URL_TTL_SECONDS}"]
    assert observed["settle"]["tier"] == "2k" and observed["settle"]["model_id"] == MODEL

    observed.clear()
    result = await execute(ImageGenArgs(prompt="a poster", size="1024x1536"), ctx)
    assert observed["provider"]["size"] == "1024x1536" and observed["provider"]["image_urls"] == []
    assert observed["settle"]["tier"] == "1k"


@pytest.mark.asyncio
async def test_execute_refuses_what_seedream_cannot_do_without_calling_it(monkeypatch):
    observed = {}
    _execute_env(monkeypatch, observed)
    ctx = ToolContext(user_id="u", session_id="s", workspace_id="w", part_id="part_2")

    for args, reason in (
        (ImageGenArgs(prompt="two", n=2), "one image per call"),
        (ImageGenArgs(prompt="w", output_format="webp"), "png or jpeg"),
        (ImageGenArgs(prompt="cutout", background="transparent"), "exactly one input image"),
    ):
        result = await execute(args, ctx)
        assert result.metadata["failure_code"] == "image_request_unsupported" and reason in result.output
    assert "provider" not in observed


def test_both_pixel_tiers_are_priced():
    for tier, price in (("1k", "0.30"), ("2k", "0.60")):
        quote = media.quote_image(MODEL, 1, tier=tier)
        assert quote.credits == Decimal(price), quote


@pytest.mark.asyncio
async def test_the_real_sdk_posts_the_body_ark_accepted_in_the_live_runs(monkeypatch):
    """Through the real OpenAI SDK: one JSON POST to /images/generations, never /images/edits."""
    import json

    import httpx
    import openai

    import tool.image_gen as image_mod

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={
            "model": MODEL, "created": 1,
            "data": [{"url": "https://tos.test/out.png", "size": "1528x2048", "output_format": "png"}],
            "usage": {"input_images": 1, "generated_images": 1, "output_tokens": 12224, "total_tokens": 12224},
        })

    real = openai.AsyncOpenAI
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: real(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs))

    async def fake_download(url):
        return _png(1528, 2048)

    monkeypatch.setattr(image_mod, "_download_generated_url", fake_download)
    source = InputImage("asset_1", "dish.png", "image/png", _png(1792, 2400), "users/u/dish.png")

    outputs = await _call_provider(
        TARGET, ImageGenArgs(prompt="night mood", input_images=["asset_1"]), size="1528x2048",
        quality="auto", output_format="png", images=[source], mask=None,
        image_urls=["https://oss.test/signed-dish"],
    )

    assert outputs == [_png(1528, 2048)]
    assert seen == [("POST", "/api/v3/images/generations", {
        "model": MODEL, "prompt": "night mood", "size": "1528x2048", "output_format": "png",
        "watermark": False, "image": ["https://oss.test/signed-dish"],
    })]
