"""Context-window and vision resolution from a model id.

The heuristics are ordered-substring matching, which has one specific trap: a
narrower id contains a wider one. `gpt-5.4-mini` contains `gpt-5.4`, so a rule
list in the wrong order silently hands a 400k model a 1M budget — compaction
then never fires and every long run dies on a provider error instead.
"""
import pytest

from agent.compaction import _heuristic_context_limit
from agent.vision import supports_vision


@pytest.mark.parametrize(
    "model_id,expected",
    [
        # GPT-5 defaults to the smaller window the common gateways actually
        # serve, not the larger one OpenAI quotes direct.
        ("openai/gpt-5.4-mini", 256_000),
        ("openai/gpt-5.4", 256_000),
        ("openai/gpt-5.3-codex", 256_000),
        ("openai/gpt-5.6-luna", 256_000),
        # Claude: 4.6 and up carry the 1M window, the 4.5-era Haiku does not.
        ("anthropic/claude-opus-5", 1_000_000),
        ("anthropic/claude-sonnet-4-6", 1_000_000),
        ("anthropic/claude-haiku-4-5", 200_000),
        ("anthropic/claude-3-5-sonnet", 200_000),
        # DeepSeek V4 raised the line to 1M; the legacy ids stayed at 128k.
        ("deepseek/deepseek-v4-pro", 1_000_000),
        ("deepseek/deepseek-chat", 128_000),
        ("deepseek/deepseek-reasoner", 128_000),
        ("google/gemini-3.1-pro", 1_000_000),
        ("openai/gpt-4o", 128_000),
    ],
)
def test_heuristic_context_limit(model_id: str, expected: int) -> None:
    assert _heuristic_context_limit(model_id) == expected


def test_unknown_model_gets_the_conservative_default() -> None:
    """Guessing high is the dangerous direction: no compaction, then a 400."""
    assert _heuristic_context_limit("mystery/llama-9") == 200_000


@pytest.mark.parametrize(
    "model_id,expected",
    [
        # DeepSeek ships vision as separate ids, so the base models are text-only.
        ("deepseek/deepseek-v4-flash", False),
        ("deepseek/deepseek-chat", False),
        ("deepseek/deepseek-v4-flash-vision-exp", True),
        ("qwen/qwen3-vl-plus", True),
        ("openai/gpt-5.6-luna", True),
        ("anthropic/claude-opus-5", True),
    ],
)
def test_supports_vision(model_id: str, expected: bool) -> None:
    assert supports_vision(model_id) is expected


def test_unknown_model_is_assumed_multimodal() -> None:
    """Dropping a screenshot silently is worse than a provider error: the model
    then answers confidently about a screen it was never shown."""
    assert supports_vision("mystery/llama-9") is True


@pytest.mark.asyncio
async def test_text_only_model_gets_a_note_instead_of_images() -> None:
    """A conversation outlives the model it started on.

    Screenshots taken on a vision model keep arriving at whatever model is
    selected now; sending them anyway buys a gateway 400 that kills the turn.
    """
    from agent.loop import resolve_images

    messages = [{
        "role": "user",
        "content": "what is on screen?",
        "_images": [{"asset_id": "a1", "key": "k1", "mime": "image/png"}],
    }]
    pixels = {"old": "data:image/png;base64,AAA"}
    out = await resolve_images(messages, "openai/deepseek-v4-flash", image_inputs=pixels)
    assert pixels == {}

    assert "_images" not in out[0], "the reference must not reach a text-only model"
    assert "cannot read images" in out[0]["content"]
    assert "what is on screen?" in out[0]["content"], "the question itself must survive"


@pytest.mark.asyncio
async def test_vision_model_keeps_its_image_references() -> None:
    """The gate must not fire on a model that can actually see."""
    from agent import loop

    messages = [{
        "role": "user",
        "content": "what is on screen?",
        "_images": [{"asset_id": "a2", "key": "k2", "mime": "image/png"}],
    }]
    # Pre-seed the cache so the test never reaches OSS.
    loop._IMAGE_CACHE["a2"] = "data:image/png;base64,AAA"
    pixels = {"old": "data:image/png;base64,AAA"}
    out = await loop.resolve_images(messages, "openai/gpt-5.6-luna", image_inputs=pixels)
    assert pixels == {"a2": "data:image/png;base64,AAA"}

    assert out[0]["_images"] == ["data:image/png;base64,AAA"]
    assert "cannot read images" not in out[0]["content"]


def _noise_png(size: int) -> bytes:
    import io
    import os

    from PIL import Image

    out = io.BytesIO()
    Image.frombytes("RGB", (size, size), os.urandom(size * size * 3)).save(out, format="PNG")
    return out.getvalue()


def test_small_image_is_inlined_untouched() -> None:
    from agent import loop

    data = _noise_png(32)
    assert loop._fit_inline_image(data, "image/png") == (data, "image/png")


@pytest.mark.asyncio
async def test_video_render_uses_small_generated_previews_but_explicit_reads_keep_originals(monkeypatch):
    import base64
    import hashlib
    from types import SimpleNamespace

    import httpx

    from agent import loop
    from core import oss

    data = _noise_png(512)
    part = {"asset_id": "same", "oss_key": "owned/same.png", "mime_type": "image/png"}
    generated = loop._image_ref_for_part({**part, "relation": {"kind": "generated_image"}}, "u")
    source = loop._image_ref_for_part(part, "u")
    client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=data))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=transport, **kw))
    monkeypatch.setattr(oss, "get_oss", lambda: SimpleNamespace(presign_get=lambda *a, **kw: "https://asset.test/i"))
    monkeypatch.setattr(loop, "_IMAGE_CACHE", {})
    full_uri = "data:image/png;base64," + base64.b64encode(data).decode()
    sources, manifest, evidence = {}, {}, {}
    messages = await loop.resolve_images(
        [{"_images": [generated]}, {"_images": [source]}], compact_generated=True,
        media_sources=sources, media_inputs=manifest, image_inputs=evidence)
    preview = messages[0]["_images"][0]
    pixels = base64.b64decode(preview.split(",", 1)[1])
    assert preview.startswith("data:image/jpeg;") and len(pixels) <= loop._GENERATED_PREVIEW_BYTES < len(data)
    assert messages[1]["_images"] == [full_uri] and evidence["same"] == full_uri
    assert sources[hashlib.sha256(pixels).hexdigest()] == "same"
    assert preview not in manifest and manifest[full_uri]["oss_key"] == part["oss_key"]
    regular = await loop.resolve_images([{"_images": [generated]}])
    assert regular[0]["_images"] == [full_uri]


def test_oversized_image_is_recompressed_at_full_resolution(monkeypatch) -> None:
    """Over the provider's per-image cap: lower the JPEG quality, keep the pixels."""
    import io

    from PIL import Image

    from agent import loop

    data = _noise_png(256)
    monkeypatch.setattr(loop, "_INLINE_IMAGE_MAX_BYTES", len(data) // 3)
    fitted = loop._fit_inline_image(data, "image/png")
    assert fitted is not None
    out, mime = fitted
    assert mime == "image/jpeg"
    assert len(out) <= len(data) // 3
    assert Image.open(io.BytesIO(out)).size == (256, 256)


def test_image_that_cannot_fit_is_dropped(monkeypatch) -> None:
    from agent import loop

    monkeypatch.setattr(loop, "_INLINE_IMAGE_MAX_BYTES", 100)
    assert loop._fit_inline_image(_noise_png(256), "image/png") is None
    assert loop._fit_inline_image(b"x" * 200, "image/png") is None
