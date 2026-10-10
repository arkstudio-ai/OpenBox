"""RunningHub image models behind image_gen: requests, receipts, recovery.

No network: RunningHub, the result CDN and OSS are httpx mock transports.
Prices and the 40310 behaviour were measured live on 2026-10-10
(docs/RUNNINGHUB_IMAGE_CHANNEL.md).
"""
import json
import struct
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

import agent.effect_ledger as effects
from core.config import OpenBoxConfig
from tool import image_runninghub as rh
from tool.image_gen import ImageGenArgs, ProviderTarget, StoredImage, execute
from tool.tool import ToolContext

BASE = "https://rh.test"
KEY = "rh-test-only"


def _png(width: int, height: int) -> bytes:
    """Enough of a PNG for header reads (nothing here decodes pixels)."""
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + b"\x00" * 4)


RESULT = _png(1024, 1024)


# ── request shaping ──────────────────────────────────────────────────────────

def test_sizes_map_onto_each_models_own_parameters():
    pro, flash, g2, seedream = (rh.MODELS[name] for name in (
        "rhart-image-n-pro", "rhart-image-n-g31-flash", "rhart-image-g-2", "seedream-v5-pro"))

    wide = rh.plan_request(pro, prompt="p", size="1920x1080", image_urls=[], output_format="png")
    assert wide.endpoint == "rhart-image-n-pro/text-to-image"
    assert wide.body == {"prompt": "p", "aspectRatio": "16:9", "resolution": "2k"} and wide.tier == "2k"
    assert "16:9" in wide.note
    assert rh.plan_request(flash, prompt="p", size="auto", image_urls=[], output_format="png").body[
        "aspectRatio"] == "1:1"
    # "auto" lets GPT Image 2 choose, and keeps an edited image's own shape.
    assert rh.plan_request(g2, prompt="p", size="auto", image_urls=[], output_format="png").body[
        "aspectRatio"] == "empty"
    edit = rh.plan_request(pro, prompt="p", size="auto", image_urls=["https://oss.test/a"], output_format="png")
    assert edit.endpoint == "rhart-image-n-pro/edit" and edit.body["aspectRatio"] == "empty"
    assert edit.body["imageUrls"] == ["https://oss.test/a"]
    assert rh.plan_request(pro, prompt="p", size="3840x2160", image_urls=[], output_format="png").tier == "4k"

    exact = rh.plan_request(seedream, prompt="p", size="1920x1080", image_urls=[], output_format="jpeg")
    assert exact.body == {"prompt": "p", "width": 1920, "height": 1080, "outputFormat": "jpeg"}
    assert exact.tier == "2k"  # priced above 1024x1024
    small = rh.plan_request(seedream, prompt="p", size="1024x1024", image_urls=[], output_format="png")
    assert small.tier == "1k"
    kept = rh.plan_request(seedream, prompt="p", size="auto", image_urls=["https://oss.test/a"],
                           output_format="png", input_size=(1200, 800))
    assert (kept.endpoint, kept.body["width"], kept.body["height"]) == ("seedream-v5-pro/image-to-image", 2048, 1365)

    with pytest.raises(rh.ImageRequestRefused, match="supported aspect ratios"):
        rh.plan_request(pro, prompt="p", size="2000x1000", image_urls=[], output_format="png")
    with pytest.raises(rh.ImageRequestRefused, match="240 to 8192"):
        rh.plan_request(seedream, prompt="p", size="200x200", image_urls=[], output_format="png")


def test_requests_a_model_cannot_take_are_refused_before_paying():
    pro = rh.MODELS["rhart-image-n-pro"]
    common = dict(mask=False, background=None, input_count=0, prompt="p")
    assert "one image per request" in rh.unsupported(pro, n=2, **{**common})
    assert "no mask" in rh.unsupported(pro, n=1, **{**common, "mask": True})
    assert "transparent" in rh.unsupported(pro, n=1, **{**common, "background": "transparent"})
    assert "at most 10" in rh.unsupported(pro, n=1, **{**common, "input_count": 11})
    assert "5000" in rh.unsupported(rh.MODELS["seedream-v5-pro"], n=1, **{**common, "prompt": "x" * 5001})
    assert rh.unsupported(pro, n=1, **common) is None


def test_task_replies_and_image_headers():
    done = {"taskId": "t", "status": "SUCCESS", "errorCode": "",
            "results": [{"url": "https://cdn.test/a.png", "outputType": "png"},
                        {"url": "https://cdn.test/v.mp4", "outputType": "mp4"}]}
    assert rh.task_state(done) == "completed" and rh.result_urls(done) == ["https://cdn.test/a.png"]
    assert rh.task_state({"status": "SUCCESS", "results": None}) == "running"
    assert rh.task_state({"status": "QUEUED"}) == "queued"
    assert rh.task_state({"status": "FAILED", "errorCode": "1000"}) == "failed"
    assert "内容审核" in rh.failure_message({"status": "FAILED", "errorMessage": "sensitive content detected"})
    assert rh.image_dimensions(_png(1200, 800)) == (1200, 800)
    jpeg = b"\xff\xd8" + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 600, 900) + b"\x00" * 12
    assert rh.image_dimensions(jpeg) == (900, 600)
    assert rh.image_dimensions(b"not an image") is None


# ── the tool path, with the effect ledger ───────────────────────────────────

class FakeOss:
    def presign_get(self, key, expires_sec=300, internal=False):
        return f"https://oss.test/{key}?Expires={expires_sec}&Signature=signed"


class Gateway:
    """RunningHub, its result CDN and OSS reads behind one mock transport."""

    def __init__(self):
        self.submits: list[tuple[str, dict]] = []
        self.queries = 0
        self.submit_reply = lambda _request: httpx.Response(200, json={"taskId": "rh-task-1", "status": "QUEUED"})
        self.query_reply = lambda _request: httpx.Response(200, json={
            "taskId": "rh-task-1", "status": "SUCCESS", "errorCode": "",
            "results": [{"url": "https://cdn.test/out.png", "outputType": "png"}],
            "usage": {"thirdPartyConsumeMoney": "0.27"}})
        self.inputs: dict[str, bytes] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(BASE + "/openapi/v2/query"):
            self.queries += 1
            return self.query_reply(request)
        if url.startswith(BASE + "/openapi/v2/"):
            assert request.headers["Authorization"] == f"Bearer {KEY}"
            self.submits.append((request.url.path, json.loads(request.content)))
            return self.submit_reply(request)
        if url.startswith("https://cdn.test/"):
            return httpx.Response(200, content=RESULT)
        if url.startswith("https://oss.test/"):
            return httpx.Response(200, content=self.inputs[request.url.path.lstrip("/")])
        raise AssertionError(f"unexpected request {request.method} {url}")


@pytest.fixture
def runninghub(monkeypatch):
    import core.oss
    import tool.image_gen as image_mod
    from billing import media
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from sqlalchemy import update

    gateway = Gateway()
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: client(transport=httpx.MockTransport(gateway), **kw))
    target = ProviderTarget("runninghub", "seedream-v5-pro", KEY, BASE, 30, channel="runninghub")
    settings = SimpleNamespace(default_size="auto", default_quality="medium", output_format="png", dedupe=False)
    monkeypatch.setattr(image_mod, "_configured_target", lambda: (target, settings))
    config = OpenBoxConfig.model_validate({"provider": {"runninghub": {"api_key": KEY, "base_url": BASE}}})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(core.oss, "get_oss", lambda: FakeOss())
    monkeypatch.setattr(rh, "POLL_FIRST_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(rh, "POLL_MAX_DELAY_SECONDS", 0.01)

    async def store(_ctx, _oss, data, _fmt, _name, _prompt, _mode, _index, _total, *, reserved_asset):
        async with get_db_session() as db:
            await db.execute(update(FileAsset).where(FileAsset.id == reserved_asset.id)
                             .values(status="ready", size=len(data), mime="image/png"))
        return StoredImage(reserved_asset.id, reserved_asset.name, "image/png", len(data),
                           f"/workspace/{reserved_asset.name}")

    async def upload(_oss, _key, _mime, data):
        return len(data)

    settled = []

    async def settle(**kwargs):
        settled.append(kwargs)
        return None

    monkeypatch.setattr(image_mod, "_store_output", store)
    monkeypatch.setattr(image_mod, "_upload_bytes", upload)
    monkeypatch.setattr(media, "settle_image", settle)
    gateway.settled = settled
    gateway.target = target
    return gateway


async def _seed(prefix: str):
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
        db.add(Project(id=project, user_id=user, name="RunningHub images", slug=f"rh-{suffix}",
                       created_at=now, updated_at=now))
        db.add(Session(id=session, user_id=user, project_id=project, status="idle",
                       created_at=now, updated_at=now))
    lease = await driver.reserve_run(session, user)

    def context(part_id: str) -> ToolContext:
        return ToolContext(user_id=user, project_id=project, session_id=session, message_id="m",
                           part_id=part_id, run_id=lease.run_id, run_generation=lease.generation,
                           _assert_current=lease.assert_current)

    return user, lease, context


async def _ledger(user: str, asset_ids: list[str]):
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.external_effect import ExternalEffect
    from db.models.file_asset import FileAsset

    async with get_db_session() as db:
        rows = (await db.execute(select(ExternalEffect).where(
            ExternalEffect.tenant_id == user, ExternalEffect.adapter == "image_gen",
        ).order_by(ExternalEffect.created_at))).scalars().all()
        assets = (await db.execute(select(FileAsset).where(FileAsset.id.in_(asset_ids)))).scalars().all()
    return rows, {row.id: row for row in assets}


async def test_text_to_image_submits_once_keeps_the_task_receipt_and_bills_its_tier(runninghub):
    replies = iter([{"taskId": "rh-task-1", "status": "RUNNING", "errorCode": ""}])
    finished = runninghub.query_reply
    runninghub.query_reply = lambda request: (
        httpx.Response(200, json=next(replies)) if runninghub.queries == 1 else finished(request))
    user, lease, context = await _seed("rh-ok")
    try:
        result = await execute(ImageGenArgs(prompt="a honey jar", size="1024x1024"), context("part-ok"))
    finally:
        await lease.release(session_status="idle")

    assert not result.metadata.get("error"), result.output
    assert result.metadata["provider_task_id"] == "rh-task-1"
    assert "Seedream v5 Pro (seedream-v5-pro)" in result.output and "provider_task_id=rh-task-1" in result.output
    assert runninghub.submits == [("/openapi/v2/seedream-v5-pro/text-to-image", {
        "prompt": "a honey jar", "width": 1024, "height": 1024, "outputFormat": "png"})]
    assert runninghub.queries == 2
    [effect], _assets = await _ledger(user, [])
    assert (effect.state, effect.provider_handle) == ("succeeded", "rh-task-1")
    assert effect.safe_context["channel"] == "runninghub" and effect.safe_context["billing_tier"] == "1k"
    phases = [item["phase"] for item in await effects.list_effect_evidence(effect.id)]
    assert phases == ["prepared", "claim_acquired", "submitting", "accepted", "succeeded"]
    assert runninghub.settled[0]["tier"] == "1k" and runninghub.settled[0]["model_id"] == "seedream-v5-pro"


@pytest.mark.parametrize("reply,kind,reason", [
    # HTTP 200 business refusals: only our own words are shown.
    (httpx.Response(200, json={"taskId": None, "errorCode": "605", "errorMessage": "SECRET balance"}),
     "quota", "余额不足"),
    (httpx.Response(200, json={"taskId": None, "errorCode": "40310", "errorMessage": "SECRET offline"}),
     "not_found", "40310"),
    # HTTP 4xx: the provider's own message is shown, redacted.
    (httpx.Response(403, json={"msg": "model not enabled https://rh.test/x?sig=SECRET"}),
     "permission", "provider_message=model not enabled [url]"),
    (httpx.Response(429, json={"errorMessage": "slow down"}), "rate_limit", "频率限制"),
])
async def test_definite_refusals_fail_without_a_task(runninghub, reply, kind, reason):
    runninghub.submit_reply = lambda _request: reply
    user, lease, context = await _seed("rh-refused")
    try:
        result = await execute(ImageGenArgs(prompt="a honey jar"), context("part-refused"))
    finally:
        await lease.release(session_status="idle")

    assert result.metadata["failure_code"] == "image_provider_rejected"
    assert result.metadata["rejection_kind"] == kind
    assert result.metadata["manual_review"] is False and result.metadata["outcome_unknown"] is False
    assert "未受理，不扣费" in result.output and reason in result.output
    assert "SECRET" not in result.output
    assert len(runninghub.submits) == 1 and runninghub.queries == 0
    [effect], assets = await _ledger(user, result.metadata["asset_ids"])
    assert effect.state == "failed" and effect.provider_handle is None
    assert {row.status for row in assets.values()} == {"rejected"}


async def test_a_task_still_running_at_the_deadline_is_finished_by_its_task_id(runninghub, monkeypatch):
    import tool.image_gen as image_mod

    finished = runninghub.query_reply
    runninghub.query_reply = lambda _request: httpx.Response(200, json={
        "taskId": "rh-task-1", "status": "RUNNING", "errorCode": ""})
    # A zero wait: the first query still says RUNNING when time is up.
    slow = ProviderTarget("runninghub", "seedream-v5-pro", KEY, BASE, 0, channel="runninghub")
    settings = SimpleNamespace(default_size="auto", default_quality="medium", output_format="png", dedupe=False)
    monkeypatch.setattr(image_mod, "_configured_target", lambda: (slow, settings))
    user, lease, context = await _seed("rh-late")
    try:
        result = await execute(ImageGenArgs(prompt="a honey jar"), context("part-late"))
        assert result.metadata["failure_code"] == "image_task_pending"
        assert result.metadata["provider_task_id"] == "rh-task-1" and result.metadata["do_not_retry"] is True
        assert "Do not resubmit" in result.output
        [effect], assets = await _ledger(user, result.metadata["asset_ids"])
        assert (effect.state, effect.provider_handle) == ("outcome_unknown", "rh-task-1")
        assert {row.status for row in assets.values()} == {"generating"}

        # Replaying the same call reports the pending task; it never resubmits.
        replay = await execute(ImageGenArgs(prompt="a honey jar"), context("part-late"))
        assert replay.metadata["failure_code"] == "image_task_pending"
        assert len(runninghub.submits) == 1
    finally:
        await lease.release(session_status="idle")

    runninghub.query_reply = finished
    assert await effects.recover_effect_once(effect.id) == "reconciled"
    [effect], assets = await _ledger(user, result.metadata["asset_ids"])
    assert effect.state == "succeeded" and effect.projection["asset_ids"] == result.metadata["asset_ids"]
    assert all(row.status == "ready" and row.size == len(RESULT) for row in assets.values())
    assert len(runninghub.submits) == 1
    [charge] = runninghub.settled
    assert charge["key"] == f"image:effect:{effect.id}" and charge["tier"] == "2k"


async def test_a_failed_task_found_by_reconciliation_is_final(runninghub, monkeypatch):
    import tool.image_gen as image_mod

    runninghub.query_reply = lambda _request: httpx.Response(200, json={
        "taskId": "rh-task-1", "status": "RUNNING", "errorCode": ""})
    slow = ProviderTarget("runninghub", "seedream-v5-pro", KEY, BASE, 0, channel="runninghub")
    settings = SimpleNamespace(default_size="auto", default_quality="medium", output_format="png", dedupe=False)
    monkeypatch.setattr(image_mod, "_configured_target", lambda: (slow, settings))
    user, lease, context = await _seed("rh-fail")
    try:
        result = await execute(ImageGenArgs(prompt="a honey jar"), context("part-fail"))
    finally:
        await lease.release(session_status="idle")
    runninghub.query_reply = lambda _request: httpx.Response(200, json={
        "taskId": "rh-task-1", "status": "FAILED", "errorCode": "1000", "errorMessage": "unknown error"})
    [effect], _assets = await _ledger(user, [])
    assert await effects.recover_effect_once(effect.id) == "reconciled"
    [effect], assets = await _ledger(user, result.metadata["asset_ids"])
    assert effect.state == "failed"
    assert {row.status for row in assets.values()} == {"rejected"}
    assert runninghub.settled == []


async def test_a_task_that_fails_while_the_tool_waits_is_reported_plainly(runninghub):
    runninghub.query_reply = lambda _request: httpx.Response(200, json={
        "taskId": "rh-task-1", "status": "FAILED", "errorCode": "1000", "errorMessage": "SECRET"})
    user, lease, context = await _seed("rh-failed-now")
    try:
        result = await execute(ImageGenArgs(prompt="a honey jar"), context("part-failed"))
    finally:
        await lease.release(session_status="idle")
    assert result.metadata["failure_code"] == "image_task_failed" and result.metadata["retryable"] is True
    assert "错误码 1000" in result.output and "SECRET" not in result.output
    [effect], assets = await _ledger(user, result.metadata["asset_ids"])
    assert effect.state == "failed" and {row.status for row in assets.values()} == {"rejected"}


async def test_a_submit_timeout_without_a_task_id_stays_unknown(runninghub):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    runninghub.submit_reply = timeout
    user, lease, context = await _seed("rh-timeout")
    try:
        result = await execute(ImageGenArgs(prompt="a honey jar"), context("part-timeout"))
    finally:
        await lease.release(session_status="idle")
    assert result.metadata["failure_code"] == "image_provider_outcome_unknown"
    assert result.metadata["manual_review"] is True and result.metadata["do_not_retry"] is True
    [effect], _assets = await _ledger(user, [])
    assert (effect.state, effect.provider_handle) == ("outcome_unknown", None)
    # No receipt to query: review, never a second paid submit.
    assert await effects.recover_effect_once(effect.id) == "manual_review"
    assert len(runninghub.submits) == 1


async def test_image_to_image_sends_signed_urls_of_owned_assets_only(runninghub):
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    user, lease, context = await _seed("rh-edit")
    now = datetime.now(timezone.utc)
    mine, theirs = f"asset-{uuid4().hex[:10]}", f"asset-{uuid4().hex[:10]}"
    async with get_db_session() as db:
        for asset_id, owner in ((mine, user), (theirs, "someone-else")):
            key = f"assets/{owner}/{asset_id}/photo.png"
            runninghub.inputs[key] = _png(1200, 800)
            db.add(FileAsset(id=asset_id, user_id=owner, workspace_id="", session_id=None, name="photo.png",
                             oss_key=key, mime="image/png", size=len(runninghub.inputs[key]),
                             status="ready", source="user", created_at=now))
    try:
        refused = await execute(ImageGenArgs(prompt="watercolor", input_images=[theirs]), context("part-theirs"))
        assert "not a ready OSS resource owned by this user" in refused.output
        assert runninghub.submits == []

        edited = await execute(ImageGenArgs(prompt="watercolor", input_images=[mine]), context("part-mine"))
    finally:
        await lease.release(session_status="idle")

    assert not edited.metadata.get("error"), edited.output
    [(path, body)] = runninghub.submits
    assert path == "/openapi/v2/seedream-v5-pro/image-to-image"
    assert body["imageUrls"] == [
        f"https://oss.test/assets/{user}/{mine}/photo.png?Expires={rh.INPUT_URL_TTL_SECONDS}&Signature=signed"]
    # "auto" keeps the source's 3:2 shape at the 2k long edge.
    assert (body["width"], body["height"]) == (2048, 1365)


async def test_unsupported_requests_never_reach_runninghub(runninghub):
    user, lease, context = await _seed("rh-unsupported")
    try:
        many = await execute(ImageGenArgs(prompt="cats", n=2), context("part-many"))
        huge = await execute(ImageGenArgs(prompt="cats", size="9000x1000"), context("part-huge"))
    finally:
        await lease.release(session_status="idle")
    assert many.metadata["failure_code"] == "image_request_unsupported" and "one image per request" in many.output
    assert huge.title == "Invalid image size" and "240 to 8192" in huge.output
    assert runninghub.submits == []
