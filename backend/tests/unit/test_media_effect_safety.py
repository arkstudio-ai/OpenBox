"""Fault-injection coverage for paid media and third-party side effects."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.hooks import PreparedToolExecution, ToolHooks
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.user import User
from db.models.video_material import VideoMaterialAsset, VideoMaterialGroup
from db.models.video_job import VideoJob
from tool.tool import ToolContext, ToolResult
import tool.image_gen as image_mod
import tool.video_identity as identity_mod
import tool.video_production as video_mod
from tool.video_production import VideoGenerateArgs, VideoTranscriptionTarget
from tool.video_providers import provider_route_fingerprint
import video.materials as material_mod


async def _user(prefix: str) -> str:
    suffix = uuid4().hex[:12]
    user_id = f"user_{prefix}_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            User(
                id=user_id,
                username=f"{prefix}-{suffix}",
                created_at=now,
                updated_at=now,
            )
        )
    return user_id


@pytest.mark.asyncio
async def test_stale_generation_cannot_send_provider_cancel_after_claim(monkeypatch):
    from agent.driver import LeaseLostError

    target = video_mod.VideoProviderTarget(
        provider="doubao",
        model="video-model",
        api_key="secret",
        base_url="https://video.example.test",
        submit_timeout_seconds=30,
        status_timeout_seconds=10,
    )
    job = SimpleNamespace(
        id="video_stale_cancel",
        user_id="user_1",
        kind="segment",
        status="in_progress",
        production_id=None,
        segment_id=None,
        request_data={
            "provider_route_fingerprint": provider_route_fingerprint(target),
            "provider_wire_format": "tokenspace_contents",
        },
        result_data={},
        provider_task_id="provider-task-1",
        output_asset_id=None,
        error=None,
        model="video-model",
        updated_at=datetime.now(timezone.utc),
    )
    guards = 0
    provider_calls = 0

    async def assert_current():
        nonlocal guards
        guards += 1
        if guards == 2:
            raise LeaseLostError("replacement generation owns the session")

    async def fake_update(*_args, **_kwargs):
        return True

    async def forbidden_cancel(*_args):
        nonlocal provider_calls
        provider_calls += 1

    monkeypatch.setattr(video_mod, "_owned_job", lambda *_args: _return(job))
    monkeypatch.setattr(
        video_mod,
        "_configured_target",
        lambda _model: (target, SimpleNamespace(poll_interval_seconds=5)),
    )
    monkeypatch.setattr(video_mod, "_update_job", fake_update)
    monkeypatch.setattr(video_mod, "_provider_cancel", forbidden_cancel)

    ctx = ToolContext(user_id="user_1", _assert_current=assert_current)
    with pytest.raises(LeaseLostError):
        await video_mod.execute_generate(
            VideoGenerateArgs(action="cancel", job_id=job.id), ctx
        )

    assert guards == 2
    assert provider_calls == 0


@pytest.mark.asyncio
async def test_late_video_receipt_never_overwrites_newer_job_state():
    user_id = await _user("video_late_receipt")
    suffix = uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    job = VideoJob(
        id=f"video_{suffix}",
        user_id=user_id,
        kind="segment",
        idempotency_key=f"segment-{suffix}",
        request_hash="b" * 64,
        status="outcome_unknown",
        provider_task_id=None,
        result_data={},
        request_data={},
        attempt=0,
        created_at=now,
        updated_at=now,
    )
    async with get_db_session() as db:
        db.add(job)

    await video_mod._persist_video_submit_receipt(
        job,
        user_id=user_id,
        task_id="provider-late-receipt",
        stored_state="in_progress",
    )

    async with get_db_session() as db:
        refreshed = await db.get(VideoJob, job.id)
        assert refreshed.provider_task_id == "provider-late-receipt"
        assert refreshed.status == "outcome_unknown"


@pytest.mark.asyncio
async def test_cancel_without_submit_receipt_quarantines_instead_of_guessing_delete():
    user_id = await _user("video_cancel_no_receipt")
    suffix = uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    job = VideoJob(
        id=f"video_{suffix}",
        user_id=user_id,
        kind="segment",
        idempotency_key=f"segment-{suffix}",
        request_hash="c" * 64,
        status="submitting",
        provider_task_id=None,
        result_data={},
        request_data={},
        attempt=0,
        created_at=now,
        updated_at=now,
    )
    async with get_db_session() as db:
        db.add(job)

    result = await video_mod.execute_generate(
        VideoGenerateArgs(action="cancel", job_id=job.id),
        ToolContext(user_id=user_id),
    )

    assert result.metadata["failure_code"] == "video_cancel_without_receipt"
    assert result.metadata["outcome_unknown"] is True
    async with get_db_session() as db:
        refreshed = await db.get(VideoJob, job.id)
        assert refreshed.status == "outcome_unknown"


async def _return(value):
    return value


async def _stt_job(*, status: str, provider_task_id: str | None):
    user_id = await _user("stt_effect")
    suffix = uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    asset = FileAsset(
        id=f"asset_stt_{suffix}",
        user_id=user_id,
        session_id=None,
        project_id=None,
        name="speech.mp3",
        oss_key=f"assets/{user_id}/speech-{suffix}.mp3",
        mime="audio/mpeg",
        size=12,
        status="ready",
        source="agent",
        transient=True,
        created_at=now,
    )
    effect = {
        "operation_key": f"openbox:stt:video_{suffix}:hash",
        "provider": "dashscope",
        "state": "receipt_persisted" if provider_task_id else "submitting",
        **({"task_id": provider_task_id} if provider_task_id else {}),
    }
    job = VideoJob(
        id=f"video_stt_{suffix}",
        user_id=user_id,
        session_id=None,
        project_id=None,
        kind="stt",
        production_id=None,
        segment_id=None,
        idempotency_key=f"stt-{suffix}",
        request_hash="a" * 64,
        prompt_hash=None,
        status=status,
        model="fun-asr",
        provider_task_id=provider_task_id,
        sandbox_job_id="media-1",
        prompt=None,
        request_data={},
        result_data={"extraction": {"ok": True}, "stt_effect": effect},
        output_asset_id=asset.id,
        error=None,
        attempt=1,
        attached_message_id=None,
        created_at=now,
        updated_at=now,
        started_at=now,
        completed_at=None,
    )
    async with get_db_session() as db:
        db.add(asset)
        db.add(job)
    return user_id, job


@pytest.mark.asyncio
async def test_stt_restart_polls_persisted_receipt_without_resubmit(monkeypatch):
    user_id, seeded = await _stt_job(
        status="transcribing", provider_task_id="dashscope-task-1"
    )
    calls = []

    async def provider(_target, _url, **kwargs):
        calls.append(kwargs)
        assert kwargs["task_id"] == "dashscope-task-1"
        return {"text": "你好", "provider": "dashscope", "model": "fun-asr"}

    async def record(*_args, **_kwargs):
        return {"similarity": 1.0, "verdict": "pass", "notes": []}

    monkeypatch.setattr(video_mod, "_provider_transcribe", provider)
    monkeypatch.setattr("tool.video_workflow.record_segment_transcript", record)
    target = VideoTranscriptionTarget(
        engine="dashscope",
        model="fun-asr",
        api_key="secret",
        base_url="https://dashscope.example.test",
        timeout_seconds=30,
        poll_interval_seconds=0.01,
        similarity_threshold=0.9,
    )
    oss = SimpleNamespace(
        head=lambda _key: _return({"size": 12}),
        presign_get=lambda *_args, **_kwargs: "https://oss.example.test/speech.mp3",
    )

    refreshed = await video_mod._finalize_transcription(
        seeded,
        ToolContext(user_id=user_id),
        target,
        oss,
        {"ok": True},
        durable_recovery=True,
    )

    assert len(calls) == 1
    assert refreshed.status == "completed"
    assert refreshed.provider_task_id == "dashscope-task-1"


@pytest.mark.asyncio
async def test_stt_restart_without_receipt_becomes_outcome_unknown(monkeypatch):
    user_id, seeded = await _stt_job(status="transcribing", provider_task_id=None)

    async def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("receipt-less restart must not POST again")

    monkeypatch.setattr(video_mod, "_provider_transcribe", forbidden_provider)
    target = VideoTranscriptionTarget(
        engine="dashscope",
        model="fun-asr",
        api_key="secret",
        base_url="https://dashscope.example.test",
        timeout_seconds=30,
        poll_interval_seconds=0.01,
        similarity_threshold=0.9,
    )
    oss = SimpleNamespace(head=lambda _key: _return({"size": 12}))

    refreshed = await video_mod._finalize_transcription(
        seeded,
        ToolContext(user_id=user_id),
        target,
        oss,
        {"ok": True},
        durable_recovery=True,
    )

    assert refreshed.status == "outcome_unknown"
    assert "manual review" in refreshed.error.casefold()


@pytest.mark.asyncio
async def test_late_stt_receipt_recovers_no_receipt_quarantine_without_resubmit():
    user_id, seeded = await _stt_job(
        status="outcome_unknown", provider_task_id=None
    )

    await video_mod._persist_stt_receipt(
        seeded,
        task_id="dashscope-late-receipt",
        operation_key="stable-stt-operation",
        extraction_result={"ok": True},
    )

    async with get_db_session() as db:
        refreshed = await db.get(VideoJob, seeded.id)
        assert refreshed.status == "transcribing"
        assert refreshed.provider_task_id == "dashscope-late-receipt"
        assert refreshed.result_data["stt_effect"] == {
            "operation_key": "stable-stt-operation",
            "provider": "dashscope",
            "state": "receipt_persisted",
            "task_id": "dashscope-late-receipt",
        }


@pytest.mark.asyncio
async def test_dashscope_receipt_callback_precedes_any_poll(monkeypatch):
    import httpx

    events = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"output": {"task_id": "task-receipt"}}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            events.append("post_returned")
            return Response()

        async def get(self, *_args, **_kwargs):
            events.append("poll")
            raise AssertionError("poll must wait for durable receipt")

    async def receipt(task_id):
        events.append(f"receipt:{task_id}")
        raise RuntimeError("simulated crash before receipt DB commit")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())
    target = VideoTranscriptionTarget(
        engine="dashscope",
        model="fun-asr",
        api_key="secret",
        base_url="https://dashscope.example.test",
        timeout_seconds=30,
        poll_interval_seconds=0.01,
        similarity_threshold=0.9,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        await video_mod._dashscope_transcribe(
            target,
            "https://oss.example.test/speech.mp3",
            operation_key="stable-operation",
            receipt_callback=receipt,
        )

    assert events == ["post_returned", "receipt:task-receipt"]


@pytest.mark.asyncio
async def test_image_oss_lost_response_reconciles_by_head_and_digest(monkeypatch):
    import hashlib
    import httpx

    data = b"image payload"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def put(self, url, **_kwargs):
            raise httpx.ReadError("response lost", request=httpx.Request("PUT", url))

    class Oss:
        def presign_put(self, *_args, **_kwargs):
            return "https://oss.example.test/output.png"

        async def head(self, _key):
            return {
                "size": len(data),
                "etag": hashlib.md5(data, usedforsecurity=False).hexdigest(),
            }

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: Client())

    assert await image_mod._upload_bytes(Oss(), "stable/key.png", "image/png", data) == len(data)


@pytest.mark.asyncio
async def test_image_oss_copy_lost_response_reconciles_against_source(monkeypatch):
    user_id = await _user("image_copy_effect")
    copied_key = ""

    class Oss:
        async def copy(self, _source, destination):
            nonlocal copied_key
            copied_key = destination
            raise RuntimeError("copy response lost after commit")

        async def head(self, key):
            if key in {"source/image.png", copied_key}:
                return {"size": 12, "etag": "same-etag"}
            return None

    cached = SimpleNamespace(
        name="image.png",
        mime="image/png",
        size=12,
        oss_key="source/image.png",
    )
    stored = await image_mod._store_reused(
        ToolContext(user_id=user_id),
        Oss(),
        cached,
        None,
        "same prompt",
        "generate",
    )

    assert stored is not None
    assert stored.size == 12


@pytest.mark.asyncio
async def test_duplicate_image_operation_never_makes_second_paid_call(monkeypatch):
    import core.oss

    user_id = await _user("image_effect")
    provider_calls = 0
    settings = SimpleNamespace(
        default_size="auto",
        default_quality="medium",
        output_format="png",
        dedupe=False,
    )
    target = image_mod.ProviderTarget(
        "openai", "gpt-image-2", "secret", "https://images.example.test/v1", 30
    )

    async def inputs(*_args, **_kwargs):
        return [], None

    async def provider(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise RuntimeError("response disappeared after send")

    monkeypatch.setattr(image_mod, "_configured_target", lambda: (target, settings))
    monkeypatch.setattr(image_mod, "_load_inputs", inputs)
    monkeypatch.setattr(image_mod, "_call_provider", provider)
    monkeypatch.setattr(core.oss, "get_oss", lambda: object())
    ctx = ToolContext(user_id=user_id, session_id="", part_id="tool-call-stable")
    args = image_mod.ImageGenArgs(prompt="one paid image")

    first = await image_mod.execute(args, ctx)
    second = await image_mod.execute(args, ctx)

    assert provider_calls == 1
    assert first.metadata["failure_code"] == "image_provider_outcome_unknown"
    assert second.metadata["failure_code"] == "image_outcome_unknown"
    assert first.metadata["do_not_retry"] is True
    assert second.metadata["do_not_retry"] is True


@pytest.mark.asyncio
async def test_image_pre_send_lease_loss_is_retryable_without_hidden_provider_call(monkeypatch):
    import core.oss
    from agent.driver import LeaseLostError
    from sqlalchemy import select

    user_id = await _user("image_pre_send")
    provider_calls = 0
    settings = SimpleNamespace(
        default_size="auto",
        default_quality="medium",
        output_format="png",
        dedupe=False,
    )
    target = image_mod.ProviderTarget(
        "openai", "gpt-image-2", "secret", "https://images.example.test/v1", 30
    )

    async def inputs(*_args, **_kwargs):
        return [], None

    async def provider(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise RuntimeError("response disappeared after send")

    async def stale():
        raise LeaseLostError("replacement generation owns the call")

    monkeypatch.setattr(image_mod, "_configured_target", lambda: (target, settings))
    monkeypatch.setattr(image_mod, "_load_inputs", inputs)
    monkeypatch.setattr(image_mod, "_call_provider", provider)
    monkeypatch.setattr(core.oss, "get_oss", lambda: object())
    args = image_mod.ImageGenArgs(prompt="pre-send fence")
    stale_ctx = ToolContext(
        user_id=user_id,
        session_id="",
        part_id="stable-pre-send-call",
        _assert_current=stale,
    )

    with pytest.raises(LeaseLostError):
        await image_mod.execute(args, stale_ctx)
    assert provider_calls == 0
    async with get_db_session() as db:
        rows = list(
            (
                await db.execute(
                    select(FileAsset).where(FileAsset.user_id == user_id)
                )
            ).scalars()
        )
        assert [row.status for row in rows] == ["pending"]

    retry = await image_mod.execute(
        args,
        ToolContext(
            user_id=user_id,
            session_id="",
            part_id="stable-pre-send-call",
        ),
    )
    assert provider_calls == 1
    assert retry.metadata["failure_code"] == "image_provider_outcome_unknown"


@pytest.mark.asyncio
async def test_material_unknown_create_is_quarantined_without_second_post(monkeypatch):
    user_id = await _user("material_effect")
    calls = 0

    async def provider(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("response lost")

    monkeypatch.setattr(material_mod, "configured_material_target", _material_target)
    monkeypatch.setattr(material_mod, "call_material_api", provider)

    with pytest.raises(material_mod.MaterialProviderError) as first:
        await material_mod.create_liveness_session(user_id, "本人")
    with pytest.raises(material_mod.MaterialProviderError) as second:
        await material_mod.create_liveness_session(user_id, "本人")

    assert calls == 1
    assert first.value.code == "material_outcome_unknown"
    assert second.value.code == "material_outcome_unknown"


@pytest.mark.asyncio
async def test_material_manual_review_surfaces_structured_tool_failure(monkeypatch):
    async def unknown_create(*_args, **_kwargs):
        raise material_mod._manual_review_error("真人认证会话")

    monkeypatch.setattr(identity_mod, "create_liveness_session", unknown_create)
    result = await identity_mod.execute_video_identity(
        identity_mod.VideoIdentityArgs(action="create", label="本人"),
        ToolContext(user_id="user-material"),
    )

    assert result.metadata["error"] is True
    assert result.metadata["failure_code"] == "material_outcome_unknown"
    assert result.metadata["manual_review"] is True
    assert result.metadata["do_not_retry"] is True


@pytest.mark.asyncio
async def test_stale_liveness_send_boundary_becomes_manual_review_without_post(monkeypatch):
    user_id = await _user("material_liveness_stale")
    target = _material_target()
    label = "本人"
    operation_key = material_mod._operation_key(
        target.provider, user_id, "LivenessFace", label
    )
    now = datetime.now(timezone.utc)
    row_id = material_mod._stable_row_id("identity", operation_key)
    async with get_db_session() as db:
        db.add(
            VideoMaterialGroup(
                id=row_id,
                user_id=user_id,
                provider=target.provider,
                project_name=target.project_name,
                group_type="LivenessFace",
                label=label,
                provider_group_id=None,
                status="session_creating",
                created_at=now - timedelta(minutes=10),
                updated_at=now - timedelta(minutes=10),
            )
        )

    calls = 0

    async def forbidden_provider(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("stale ambiguous create must never POST again")

    monkeypatch.setattr(material_mod, "configured_material_target", lambda: target)
    monkeypatch.setattr(material_mod, "call_material_api", forbidden_provider)
    with pytest.raises(material_mod.MaterialProviderError) as caught:
        await material_mod.create_liveness_session(user_id, label)

    assert caught.value.code == "material_outcome_unknown"
    assert calls == 0
    async with get_db_session() as db:
        refreshed = await db.get(VideoMaterialGroup, row_id)
        assert refreshed.status == "manual_review"


@pytest.mark.asyncio
async def test_material_group_concurrent_resolver_never_issues_second_create(monkeypatch):
    user_id = await _user("material_group_effect")
    target = _material_target()
    label = material_mod._aigc_label(user_id)
    operation_key = material_mod._operation_key(
        target.provider, user_id, "AIGC", label
    )
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            VideoMaterialGroup(
                id=material_mod._stable_row_id("material_group", operation_key),
                user_id=user_id,
                provider=target.provider,
                project_name=target.project_name,
                group_type="AIGC",
                label=label,
                provider_group_id=None,
                status="resolving",
                created_at=now,
                updated_at=now,
            )
        )

    calls = []

    async def provider(_target, action, _body, **_kwargs):
        calls.append(action)
        if action == "ListAssetGroups":
            return {"Items": []}
        raise AssertionError("a concurrent resolver must not issue CreateAssetGroup")

    monkeypatch.setattr(material_mod, "call_material_api", provider)
    with pytest.raises(material_mod.MaterialProviderError) as caught:
        await material_mod._ensure_aigc_group(user_id, target)

    assert caught.value.code == "material_operation_in_progress"
    assert calls == ["ListAssetGroups"]


@pytest.mark.asyncio
async def test_material_receipt_db_failure_quarantines_without_second_create(monkeypatch):
    import core.oss
    from sqlalchemy import select

    user_id = await _user("material_receipt_commit")
    suffix = uuid4().hex[:12]
    source_id = f"asset_material_{suffix}"
    identity_id = f"identity_material_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            FileAsset(
                id=source_id,
                user_id=user_id,
                session_id=None,
                project_id=None,
                name="portrait.png",
                oss_key=f"assets/{user_id}/{source_id}/portrait.png",
                mime="image/png",
                size=12,
                status="ready",
                source="user",
                transient=False,
                created_at=now,
            )
        )
        db.add(
            VideoMaterialGroup(
                id=identity_id,
                user_id=user_id,
                provider="doubao",
                project_name="default",
                group_type="LivenessFace",
                label="本人",
                provider_group_id=f"group-{suffix}",
                status="active",
                created_at=now,
                updated_at=now,
            )
        )

    calls = 0

    async def provider(_target, action, _body, **_kwargs):
        nonlocal calls
        assert action == "CreateAsset"
        calls += 1
        return {"Id": f"asset-provider-{suffix}"}

    async def lost_receipt_commit(*_args, **_kwargs):
        raise RuntimeError("simulated DB failure after provider receipt")

    oss = SimpleNamespace(
        presign_get=lambda *_args, **_kwargs: "https://oss.example.test/portrait.png"
    )
    monkeypatch.setattr(material_mod, "configured_material_target", _material_target)
    monkeypatch.setattr(material_mod, "call_material_api", provider)
    monkeypatch.setattr(
        material_mod, "_persist_material_asset_receipt", lost_receipt_commit
    )
    monkeypatch.setattr(core.oss, "get_oss", lambda: oss)

    for _attempt in range(2):
        with pytest.raises(material_mod.MaterialProviderError) as caught:
            await material_mod.ensure_material_asset(
                user_id, source_id, identity_id=identity_id
            )
        assert caught.value.code == "material_outcome_unknown"

    assert calls == 1
    async with get_db_session() as db:
        binding = (
            await db.execute(
                select(VideoMaterialAsset).where(
                    VideoMaterialAsset.user_id == user_id,
                    VideoMaterialAsset.source_asset_id == source_id,
                )
            )
        ).scalar_one()
        assert binding.status == "manual_review"


def _material_target():
    return material_mod.MaterialTarget(
        provider="doubao",
        api_key="secret",
        base_url="https://materials.example.test",
        project_name="default",
        request_timeout_seconds=30,
        poll_interval_seconds=0.01,
        liveness_session_ttl_seconds=300,
        input_url_ttl_seconds=600,
    )


@pytest.mark.asyncio
async def test_tool_hooks_classify_third_party_by_request_origin_not_error_text():
    import httpx

    async def external_failure(_args, _ctx):
        raise httpx.ConnectError(
            "container unavailable (attacker-controlled provider text)",
            request=httpx.Request("POST", "https://provider.example.test/v1/images"),
        )

    ctx = ToolContext(
        sandbox=SimpleNamespace(base_url="http://127.0.0.1:18888", request_context=None)
    )
    prepared = PreparedToolExecution(
        tool_id="image_gen",
        execute_fn=external_failure,
        args={},
        shared_ctx=ctx,
        run_ctx=ctx,
        part_id="part-1",
        start_time=0,
        isolated=False,
    )

    outcome = await ToolHooks("session").dispatch_execute(prepared)

    assert outcome.terminal_event == "error"
    assert outcome.result.title == "External Service Error"
    assert outcome.result.metadata["failure_code"] == "external_transport_error"
    assert outcome.result.metadata.get("container_error") is not True


@pytest.mark.asyncio
async def test_tool_hooks_classify_sandbox_by_normalized_request_origin():
    import httpx

    async def sandbox_failure(_args, _ctx):
        raise httpx.ConnectError(
            "provider outage (untrusted text)",
            request=httpx.Request("POST", "http://sandbox.example.test/action"),
        )

    ctx = ToolContext(
        sandbox=SimpleNamespace(
            base_url="http://SANDBOX.example.test:80/api",
            request_context=None,
        )
    )
    prepared = PreparedToolExecution(
        tool_id="bash",
        execute_fn=sandbox_failure,
        args={},
        shared_ctx=ctx,
        run_ctx=ctx,
        part_id="part-sandbox",
        start_time=0,
        isolated=False,
    )

    outcome = await ToolHooks("session").dispatch_execute(prepared)

    assert outcome.terminal_event == "error"
    assert outcome.result.title == "Container Error"
    assert outcome.result.metadata["failure_code"] == "sandbox_transport_error"
    assert outcome.result.metadata["container_error"] is True


@pytest.mark.asyncio
async def test_finalization_heartbeat_loss_cancels_old_oss_transfer(monkeypatch):
    from agent.driver import LeaseLostError

    transfer_started = asyncio.Event()
    transfer_cancelled = asyncio.Event()

    async def hanging_copy(*_args, **_kwargs):
        transfer_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            transfer_cancelled.set()

    async def lost_heartbeat():
        raise LeaseLostError("new owner")

    monkeypatch.setattr(video_mod, "_FINALIZATION_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(video_mod, "_copy_provider_video_to_oss", hanging_copy)

    with pytest.raises(LeaseLostError):
        await video_mod._copy_provider_video_with_heartbeat(
            "https://provider.example.test/video.mp4",
            object(),
            "stable/video.mp4",
            1024,
            lost_heartbeat,
        )

    assert transfer_started.is_set()
    assert transfer_cancelled.is_set()


@pytest.mark.asyncio
async def test_tool_result_with_failure_metadata_never_emits_completed():
    async def reported_failure(_args, _ctx):
        return ToolResult(
            title="Outcome unknown",
            output="manual review",
            metadata={"error": True, "failure_code": "effect_outcome_unknown"},
        )

    ctx = ToolContext()
    prepared = PreparedToolExecution(
        tool_id="image_gen",
        execute_fn=reported_failure,
        args={},
        shared_ctx=ctx,
        run_ctx=ctx,
        part_id="part-2",
        start_time=0,
        isolated=False,
    )

    outcome = await ToolHooks("session").dispatch_execute(prepared)

    assert outcome.terminal_event == "error"
    assert outcome.result.metadata["failure_code"] == "effect_outcome_unknown"
