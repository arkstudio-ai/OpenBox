"""Built-in image generation: provider routing, validation, and OSS ledger."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tool.image_gen import (
    ImageGenArgs,
    InputImage,
    ProviderTarget,
    StoredImage,
    _call_provider,
    _safe_filename,
    _store_output,
    _validate_size,
    execute,
)
from tool.tool import ToolContext


def test_gpt_image_2_accepts_flexible_sizes_within_the_documented_bounds():
    for size in (
        "auto",
        "1024x1024",
        "1536x1024",
        "2048x1152",
        "2048x2048",
        "3840x2160",
        "2160x3840",
    ):
        assert _validate_size("gpt-image-2", size) is None

    assert "multiples of 16" in _validate_size("gpt-image-2", "1000x1000")
    assert "maximum edge" in _validate_size("gpt-image-2", "4096x1024")
    assert "aspect ratio" in _validate_size("gpt-image-2", "3072x768")
    assert "total pixels" in _validate_size("gpt-image-2", "640x640")


def test_edit_mask_requires_an_input_and_transparency_rejects_jpeg():
    with pytest.raises(ValidationError, match="mask_image requires"):
        ImageGenArgs(prompt="change it", mask_image="asset_mask")
    with pytest.raises(ValidationError, match="requires png or webp"):
        ImageGenArgs(prompt="cutout", background="transparent", output_format="jpeg")


def test_output_names_are_safe_unique_variants():
    assert _safe_filename("../Hero image.PNG", "asset_1", 1, 1, "png") == "Hero_image.png"
    assert _safe_filename("hero.png", "asset_1", 2, 3, "webp") == "hero-2.webp"


def test_image_gen_is_a_build_only_agent_tool():
    from agent.agent import AGENTS

    assert "image_gen" in AGENTS["build"].tools
    assert all(
        "image_gen" not in AGENTS[name].tools
        for name in ("plan", "explore", "general")
    )


def test_image_gen_is_registered_and_not_parallel_safe():
    from tool.image_gen import image_gen_tool

    assert image_gen_tool.parallel_safe is False


@pytest.mark.asyncio
async def test_provider_call_uses_no_automatic_retries_and_selects_generate(monkeypatch):
    import base64
    import openai

    observed = {}

    class FakeImages:
        async def generate(self, **kwargs):
            observed["generate"] = kwargs
            return SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"\x89PNG\r\n\x1a\nbody").decode(), url=None)]
            )

        async def edit(self, **kwargs):  # pragma: no cover - wrong route guard
            raise AssertionError(f"unexpected edit call: {kwargs}")

    class FakeClient:
        def __init__(self, **kwargs):
            observed["client"] = kwargs
            self.images = FakeImages()

        async def close(self):
            observed["closed"] = True

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)
    target = ProviderTarget("openai", "gpt-image-2", "secret", "https://gateway.test/v1", 600)
    args = ImageGenArgs(prompt="a blue cube", n=1)
    outputs = await _call_provider(
        target,
        args,
        size="1024x1024",
        quality="medium",
        output_format="png",
        images=[],
        mask=None,
    )

    assert outputs[0].startswith(b"\x89PNG")
    assert observed["client"]["max_retries"] == 0
    assert observed["client"]["base_url"] == "https://gateway.test/v1"
    assert observed["generate"]["model"] == "gpt-image-2"
    assert observed["closed"] is True


@pytest.mark.asyncio
async def test_provider_call_selects_edits_and_preserves_input_order(monkeypatch):
    import base64
    import openai

    observed = {}

    class FakeImages:
        async def edit(self, **kwargs):
            observed.update(kwargs)
            return SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(b"\x89PNG\r\n\x1a\nedit").decode(), url=None)]
            )

    class FakeClient:
        def __init__(self, **_kwargs):
            self.images = FakeImages()

        async def close(self):
            pass

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeClient)
    inputs = [
        InputImage("asset_1", "base.png", "image/png", b"base"),
        InputImage("asset_2", "style.webp", "image/webp", b"style"),
    ]
    await _call_provider(
        ProviderTarget("openai", "gpt-image-2", "secret", None, 600),
        ImageGenArgs(prompt="put Image 2's style on Image 1", input_images=["a", "b"]),
        size="auto",
        quality="high",
        output_format="png",
        images=inputs,
        mask=None,
    )

    assert [item[0] for item in observed["image"]] == ["base.png", "style.webp"]
    assert observed["prompt"].startswith("put Image 2")


@pytest.mark.asyncio
async def test_storing_output_creates_agent_resource_and_chat_file_part(monkeypatch):
    import db.base
    import session.session
    import tool.image_gen as image_mod

    rows = []
    parts = []

    class FakeDb:
        def add(self, row):
            rows.append(row)

        async def commit(self):
            pass

    @asynccontextmanager
    async def fake_db_session():
        yield FakeDb()

    async def fake_upload(_oss, key, mime, data):
        assert key.startswith("assets/user_1/asset")
        assert mime == "image/png"
        return len(data)

    async def fake_save_part(part, **kwargs):
        parts.append((part, kwargs))

    monkeypatch.setattr(db.base, "get_db_session", fake_db_session)
    monkeypatch.setattr(session.session, "save_part", fake_save_part)
    monkeypatch.setattr(image_mod, "_upload_bytes", fake_upload)

    result = await _store_output(
        ToolContext(
            session_id="session_1",
            user_id="user_1",
            project_id="project_1",
            message_id="message_1",
            sandbox=None,
        ),
        SimpleNamespace(delete=None),
        b"\x89PNG\r\n\x1a\nbody",
        "png",
        "hero.png",
        "a studio hero shot",
        "generate",
        1,
        1,
    )

    assert result.name == "hero.png"
    assert result.materialized is False
    assert len(rows) == 1
    assert rows[0].source == "agent"
    assert rows[0].project_id == "project_1"
    assert rows[0].status == "ready"
    assert rows[0].transient is False
    assert len(parts) == 1
    assert parts[0][0].asset_id == result.asset_id
    assert parts[0][0].oss_key == rows[0].oss_key
    assert parts[0][1] == {"is_new": True, "user_id": "user_1"}


@pytest.mark.asyncio
async def test_execute_returns_oss_asset_ids_for_generation(monkeypatch):
    import core.oss
    import tool.image_gen as image_mod

    target = ProviderTarget("openai", "gpt-image-2", "secret", "https://gateway.test/v1", 600)
    settings = SimpleNamespace(default_size="auto", default_quality="medium", output_format="png")
    oss = object()
    observed = {}

    monkeypatch.setattr(image_mod, "_configured_target", lambda: (target, settings))
    monkeypatch.setattr(core.oss, "get_oss", lambda: oss)

    async def fake_inputs(refs, mask, ctx, selected_oss):
        assert refs == [] and mask is None and selected_oss is oss
        return [], None

    async def fake_provider(*_args, **kwargs):
        observed.update(kwargs)
        return [b"\x89PNG\r\n\x1a\nbody"]

    async def fake_store(*_args, **_kwargs):
        return StoredImage("asset_generated", "generated.png", "image/png", 12, "/workspace/generated_images/generated.png")

    monkeypatch.setattr(image_mod, "_load_inputs", fake_inputs)
    monkeypatch.setattr(image_mod, "_call_provider", fake_provider)
    monkeypatch.setattr(image_mod, "_store_output", fake_store)

    result = await execute(ImageGenArgs(prompt="a blue cube"), ToolContext(user_id="u", session_id="s"))

    assert result.metadata["mode"] == "generate"
    assert result.metadata["asset_ids"] == ["asset_generated"]
    assert observed["images"] == []
    assert "resource centre" in result.output


def test_fingerprint_is_content_addressed():
    from tool.image_gen import _fingerprint

    base = dict(
        op="edit", model="gpt-image-2", prompt="p", size="auto", quality="medium",
        output_format="png", background=None, output_compression=None, n=1,
        source_digests=["digest-a"], mask_digest=None,
    )
    a = _fingerprint(**base)
    assert len(a) == 64
    # Same request, same fingerprint — but different source BYTES never hit.
    assert _fingerprint(**base) == a
    assert _fingerprint(**{**base, "source_digests": ["digest-b"]}) != a
    # n participates, so n>1 entries can never collide with n==1 lookups.
    assert _fingerprint(**{**base, "n": 2}) != a
