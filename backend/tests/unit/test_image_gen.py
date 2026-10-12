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
    assert parts[0][1] == {"is_new": True, "user_id": "user_1", "run_fence": None}


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


async def test_execute_settles_one_image_charge_per_call(monkeypatch):
    import core.oss
    import tool.image_gen as image_mod
    from billing import media

    target = ProviderTarget("openai", "gpt-image-2", "secret", "https://gateway.test/v1", 600)
    settings = SimpleNamespace(default_size="auto", default_quality="medium", output_format="png")
    monkeypatch.setattr(image_mod, "_configured_target", lambda: (target, settings))
    monkeypatch.setattr(core.oss, "get_oss", lambda: object())

    async def fake_inputs(refs, mask, ctx, selected_oss):
        return [], None

    async def fake_provider(*_args, **_kwargs):
        return [b"one", b"two"]

    async def fake_store(ctx, oss, payload, *_args, **_kwargs):
        return StoredImage(f"asset_{payload.decode()}", f"{payload.decode()}.png", "image/png", 3, "/workspace/generated_images/x.png")

    seen = {}

    async def fake_settle(**kwargs):
        seen.update(kwargs)
        from decimal import Decimal
        return Decimal("0.60")

    monkeypatch.setattr(image_mod, "_load_inputs", fake_inputs)
    monkeypatch.setattr(image_mod, "_call_provider", fake_provider)
    monkeypatch.setattr(image_mod, "_store_output", fake_store)
    monkeypatch.setattr(media, "settle_image", fake_settle)

    ctx = ToolContext(user_id="u", session_id="s", workspace_id="w", part_id="part_9")
    result = await execute(ImageGenArgs(prompt="two cubes", n=2), ctx)

    assert seen == {"key": "image:part_9", "workspace_id": "w", "user_id": "u", "session_id": "s", "model_id": "gpt-image-2", "count": 2}
    assert result.metadata["credits"] == "0.6" and "credits=0.6" in result.output


# ── provider refusals versus unknown outcomes (production 2026-10-09/10) ────
# 18 of 19 calls reported "may have been accepted" because every exception
# after the send boundary was treated alike, including definite 4xx refusals.


def test_gpt_image_2_sizes_snap_to_the_16px_grid_keeping_their_shape():
    from tool.image_gen import _align_size

    assert _align_size("gpt-image-2", "1920x1080")[0] == "1920x1088"
    assert _align_size("gpt-image-2", "1080x1920")[0] == "1088x1920"
    assert _align_size("gpt-image-2", "1000x1500")[0] == "992x1488"  # exact 2:3
    assert _align_size("gpt-image-2", "1366x768")[0] == "1360x768"
    for size in ("1920x1080", "1366x768", "1000x1000", "1350x1080"):
        aligned, note = _align_size("gpt-image-2", size)
        width, height = (int(value) for value in aligned.split("x"))
        want_w, want_h = (int(value) for value in size.split("x"))
        assert width % 16 == 0 and height % 16 == 0
        assert abs(width / height - want_w / want_h) / (want_w / want_h) < 0.01
        assert _validate_size("gpt-image-2", aligned) is None
        assert size in note and aligned in note and "multiples of 16" in note
    # Already valid sizes, "auto" and other models are left alone.
    assert _align_size("gpt-image-2", "2048x1152") == ("2048x1152", None)
    assert _align_size("gpt-image-2", "auto") == ("auto", None)
    assert _align_size("gpt-image-1", "1920x1080") == ("1920x1080", None)


async def test_execute_aligns_a_gpt_image_2_size_and_says_so(monkeypatch):
    import core.oss
    import tool.image_gen as image_mod

    target = ProviderTarget("openai", "gpt-image-2", "secret", "https://gateway.test/v1", 600)
    settings = SimpleNamespace(default_size="auto", default_quality="medium", output_format="png")
    monkeypatch.setattr(image_mod, "_configured_target", lambda: (target, settings))
    monkeypatch.setattr(core.oss, "get_oss", lambda: object())
    observed = {}

    async def fake_inputs(*_args):
        return [], None

    async def fake_provider(*_args, **kwargs):
        observed.update(kwargs)
        return [b"\x89PNG\r\n\x1a\nbody"]

    async def fake_store(*_args, **_kwargs):
        return StoredImage("asset_wide", "wide.png", "image/png", 12, "/workspace/wide.png")

    monkeypatch.setattr(image_mod, "_load_inputs", fake_inputs)
    monkeypatch.setattr(image_mod, "_call_provider", fake_provider)
    monkeypatch.setattr(image_mod, "_store_output", fake_store)

    result = await execute(ImageGenArgs(prompt="a banner", size="1920x1080"),
                           ToolContext(user_id="u", session_id="s"))

    assert observed["size"] == "1920x1088"
    assert result.metadata["size"] == "1920x1088"
    assert result.metadata["requested_size"] == "1920x1080"
    assert "1920x1080 was adjusted to 1920x1088" in result.output


def _mock_openai_transport(monkeypatch, handler):
    import httpx
    import openai

    real = openai.AsyncOpenAI

    def client(**kwargs):
        transport = httpx.MockTransport(handler)
        return real(http_client=httpx.AsyncClient(transport=transport), **kwargs)

    monkeypatch.setattr(openai, "AsyncOpenAI", client)


def _raises(error_name):
    import httpx

    def handler(request):
        raise getattr(httpx, error_name)(f"{error_name} fixture", request=request)
    return handler


def _answers(status, **content):
    import httpx

    return lambda _request: httpx.Response(status, **content)


@pytest.mark.parametrize("handler,definite,kind", [
    (_answers(400, json={"error": {"message": "Invalid value for 'size'", "code": "invalid_value"}}),
     True, "invalid_request"),
    (_answers(400, json={"error": {"message": "Rejected by the safety system", "code": "moderation_blocked"}}),
     True, "content_policy"),
    (_answers(403, json={"error": {"message": "this token cannot use gpt-image-2"}}), True, "permission"),
    (_answers(403, text="<html><body><h1>403 Forbidden</h1></body></html>"), True, "permission"),
    (_answers(403, json={"error": {"message": "用户额度不足"}}), True, "quota"),
    (_answers(429, json={"error": {"message": "Rate limit reached for images"}}), True, "rate_limit"),
    (_answers(429, json={"error": {"message": "You exceeded your current quota",
                                    "code": "insufficient_quota"}}), True, "quota"),
    (_raises("ConnectError"), True, "not_sent"),
    (_raises("ConnectTimeout"), True, "not_sent"),
    # Everything after the request may have reached the provider stays unknown.
    (_raises("ReadTimeout"), False, "unknown"),
    (_raises("RemoteProtocolError"), False, "unknown"),
    (_answers(409, json={"error": {"message": "duplicate request in flight"}}), False, "unknown"),
    (_answers(502, json={"error": {"message": "upstream failed"}}), False, "unknown"),
    (_answers(200, json={"created": 1, "error": {"message": "upstream failed"}}), False, "unknown"),
])
async def test_real_sdk_failures_split_into_refusals_and_unknown_outcomes(monkeypatch, handler, definite, kind):
    from tool.image_gen import _classify_provider_failure

    _mock_openai_transport(monkeypatch, handler)
    with pytest.raises(Exception) as caught:
        await _call_provider(
            ProviderTarget("openai", "gpt-image-2", "secret", "https://gateway.test/v1", 600),
            ImageGenArgs(prompt="a blue cube"), size="1024x1024", quality="low",
            output_format="png", images=[], mask=None,
        )

    failure = _classify_provider_failure(caught.value)
    assert (failure.definite, failure.kind) == (definite, kind)
    assert "<html>" not in failure.detail and "secret" not in failure.detail
    if "upstream failed" in str(caught.value):
        # Including the 200 answer whose body was an error instead of images.
        assert "upstream failed" in failure.detail


def test_provider_text_is_redacted_and_bounded():
    from tool.image_gen import _redact_provider_text

    text = _redact_provider_text(
        "size must be <= 3840 and >= 16; see https://cdn.example/x.png?Signature=SECRET "
        "Authorization: Bearer sk-SECRETSECRET api_key=SECRET2 " + "内容审核未通过" * 5 + " " + "Z" * 400
    )
    assert "SECRET" not in text and "cdn.example" not in text
    assert "<= 3840 and >= 16" in text and "内容审核未通过" * 5 in text
    assert len(text) <= 300


async def _seed_image_run(prefix: str):
    from datetime import datetime, timezone
    from uuid import uuid4

    from agent import driver
    from db.base import get_db_session
    from db.models.project import Project
    from db.models.session import Session
    from db.models.user import User

    suffix = uuid4().hex[:12]
    user, project, session = (f"{prefix}-{kind}-{suffix}" for kind in ("user", "project", "session"))
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user, username=user, created_at=now, updated_at=now))
        db.add(Project(id=project, user_id=user, name="Image errors", slug=f"image-{suffix}",
                       created_at=now, updated_at=now))
        db.add(Session(id=session, user_id=user, project_id=project, status="idle",
                       created_at=now, updated_at=now))
    lease = await driver.reserve_run(session, user)

    def context(part_id: str) -> ToolContext:
        return ToolContext(user_id=user, project_id=project, session_id=session,
                           message_id="message-image", part_id=part_id, run_id=lease.run_id,
                           run_generation=lease.generation, _assert_current=lease.assert_current)

    return user, lease, context


@pytest.fixture
def fenced_image_env(monkeypatch):
    import core.oss
    import tool.image_gen as image_mod
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from sqlalchemy import update

    settings = SimpleNamespace(default_size="auto", default_quality="medium",
                               output_format="png", dedupe=False)
    target = ProviderTarget("openai", "gpt-image-2", "secret", "https://gateway.test/v1", 60)
    monkeypatch.setattr(image_mod, "_configured_target", lambda: (target, settings))
    monkeypatch.setattr(core.oss, "get_oss", lambda: object())

    async def no_inputs(*_args):
        return [], None

    async def store(_ctx, _oss, data, _fmt, _name, _prompt, _mode, _index, _total, *, reserved_asset):
        async with get_db_session() as db:
            await db.execute(update(FileAsset).where(FileAsset.id == reserved_asset.id)
                             .values(status="ready", size=len(data), mime="image/png"))
        return StoredImage(reserved_asset.id, reserved_asset.name, "image/png", len(data),
                           f"/workspace/{reserved_asset.name}")

    monkeypatch.setattr(image_mod, "_load_inputs", no_inputs)
    monkeypatch.setattr(image_mod, "_store_output", store)
    return image_mod


def _sdk_status_error(status: int, message: str):
    import httpx
    import openai

    request = httpx.Request("POST", "https://gateway.test/v1/images/generations")
    body = {"message": message, "type": "invalid_request_error"}
    response = httpx.Response(status, request=request, json={"error": body})
    error = {400: openai.BadRequestError, 403: openai.PermissionDeniedError,
             429: openai.RateLimitError}[status]
    return error(f"Error code: {status} - {body}", response=response, body=body)


async def _effect_and_assets(user: str, asset_ids: list[str]):
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.external_effect import ExternalEffect
    from db.models.file_asset import FileAsset

    async with get_db_session() as db:
        effects = (await db.execute(select(ExternalEffect).where(
            ExternalEffect.tenant_id == user, ExternalEffect.adapter == "image_gen",
        ).order_by(ExternalEffect.created_at))).scalars().all()
        assets = (await db.execute(select(FileAsset).where(FileAsset.id.in_(asset_ids)))).scalars().all()
    return effects, {row.status for row in assets}


@pytest.mark.parametrize("status,message,kind,reason", [
    (403, "this token cannot use gpt-image-2", "permission", "权限或额度问题"),
    (429, "Rate limit reached for images per minute", "rate_limit", "频率限制，稍后再试"),
    (400, "Invalid value for quality", "invalid_request", "参数不支持"),
])
async def test_definite_refusal_fails_the_effect_and_allows_a_corrected_retry(
        fenced_image_env, monkeypatch, status, message, kind, reason):
    user, lease, context = await _seed_image_run(f"image-{status}")
    calls = []

    async def provider(*_args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _sdk_status_error(status, message)
        return [b"\x89PNG\r\n\x1a\ncorrected"]

    monkeypatch.setattr(fenced_image_env, "_call_provider", provider)
    try:
        refused = await execute(ImageGenArgs(prompt="a poster"), context("part-refused"))

        assert refused.title.startswith("Image generate rejected")
        assert refused.metadata["failure_code"] == "image_provider_rejected"
        assert refused.metadata["rejection_kind"] == kind
        assert refused.metadata["provider_status"] == status
        assert refused.metadata["charged"] is False and refused.metadata["provider_accepted"] is False
        assert refused.metadata["manual_review"] is False and refused.metadata["outcome_unknown"] is False
        assert "do_not_retry" not in refused.metadata
        assert "未受理，不扣费" in refused.output and reason in refused.output
        assert f"provider_message={message}" in refused.output
        assert "may have been accepted" not in refused.output
        effects, states = await _effect_and_assets(user, refused.metadata["asset_ids"])
        assert [effect.state for effect in effects] == ["failed"]
        assert effects[0].last_error["code"] == "image_provider_rejected"
        assert states == {"rejected"}

        # Replaying the refused call itself is answered locally, not resent.
        replay = await execute(ImageGenArgs(prompt="a poster"), context("part-refused"))
        assert replay.metadata["failure_code"] == "image_provider_rejected"
        assert replay.metadata["manual_review"] is False
        assert len(calls) == 1

        # A new call (the corrected retry) is dispatched normally.
        retried = await execute(ImageGenArgs(prompt="a poster"), context("part-retry"))
        assert not retried.metadata.get("error"), retried.output
        assert len(calls) == 2
        effects, _states = await _effect_and_assets(user, [])
        assert [effect.state for effect in effects] == ["failed", "succeeded"]
    finally:
        await lease.release(session_status="idle")


async def test_timeout_after_send_stays_an_unknown_outcome(fenced_image_env, monkeypatch):
    import httpx
    import openai

    import agent.effect_ledger as effects_module

    user, lease, context = await _seed_image_run("image-timeout")
    request = httpx.Request("POST", "https://gateway.test/v1/images/generations")
    calls = []

    async def provider(*_args, **_kwargs):
        calls.append(1)
        raise openai.APITimeoutError(request=request) from httpx.ReadTimeout("slow", request=request)

    monkeypatch.setattr(fenced_image_env, "_call_provider", provider)
    try:
        result = await execute(ImageGenArgs(prompt="a poster"), context("part-timeout"))

        assert result.metadata["failure_code"] == "image_provider_outcome_unknown"
        assert result.metadata["outcome_unknown"] is True
        assert result.metadata["manual_review"] is True and result.metadata["do_not_retry"] is True
        assert "may have been accepted" in result.output and "timeout" in result.output
        effects, states = await _effect_and_assets(user, result.metadata["asset_ids"])
        assert [effect.state for effect in effects] == ["outcome_unknown"]
        assert states == {"outcome_unknown"}
    finally:
        await lease.release(session_status="idle")

    # Without image bytes, reconciliation ends in review and never resends.
    assert await effects_module.recover_effect_once(effects[0].id) == "manual_review"
    assert len(calls) == 1


async def test_a_refusal_the_worker_cannot_settle_is_reconciled_as_failed(fenced_image_env, monkeypatch):
    import agent.effect_ledger as effects_module
    from agent.driver import LeaseLostError

    user, lease, context = await _seed_image_run("image-unsettled")
    original_settle = effects_module.settle_effect
    settle_calls = []

    async def fenced_out_once(claim, **kwargs):
        settle_calls.append(kwargs.get("state"))
        if len(settle_calls) == 1:
            raise LeaseLostError("the run fence moved before settlement")
        return await original_settle(claim, **kwargs)

    async def provider(*_args, **_kwargs):
        raise _sdk_status_error(403, "forbidden")

    monkeypatch.setattr(effects_module, "settle_effect", fenced_out_once)
    monkeypatch.setattr(fenced_image_env, "_call_provider", provider)
    try:
        result = await execute(ImageGenArgs(prompt="a poster"), context("part-unsettled"))
        assert result.metadata["failure_code"] == "image_provider_rejected"
        effects, states = await _effect_and_assets(user, result.metadata["asset_ids"])
        assert [effect.state for effect in effects] == ["outcome_unknown"]
        assert states == {"rejected"}
    finally:
        await lease.release(session_status="idle")

    assert await effects_module.recover_effect_once(effects[0].id) == "reconciled"
    effects, _states = await _effect_and_assets(user, [])
    assert [effect.state for effect in effects] == ["failed"]
