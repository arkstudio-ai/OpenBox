"""The first video: a fixed template through the real video pipeline, no agent turn.

The person fills a form; this module creates a real session (so "再做一条" lands
in a conversation with the result and the prompt), posts the form as the user
message, reserves one assistant message, and drives ``video_generate`` with the
same ``ToolContext`` an agent would hold, so completion attaches the clip to
that message exactly as a chat generation does. When the person gave a shop
name, the clip goes once more through ``video_compose`` for the name overlay
(characters come out right that way; the model would misdraw them).

State lives in ``user_preferences.extra["first_video"]`` (its own key: the
``onboarding`` map is replaced wholesale by the phone) and the job is found by
its idempotency key ``first_video:<workspace>``. The sweep in
``video/job_recovery.py`` calls :func:`on_job_settled` for a job whose driver
died with the process, instead of waking the agent.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.log import create_logger
from light.templates import first_video_templates

log = create_logger(__name__)

KEY_PREFIX = "first_video:"
RETRY_SUFFIX = ":retry"
TEXT_KEY_PREFIX = "first_video_text:"
PREF_KEY = "first_video"
SESSION_TITLE = "我的第一条视频"
ENTRYPOINT = "first_video"
TERMINAL = ("ready", "failed")
#: ``offered`` is written at registration: the account is new enough to get the flow.
#: An account with no record at all predates the feature and is treated as skipped.
OFFERED = "offered"
STATUSES = ("none", "skipped", "submitted", "running", "ready", "failed")
MAX_GENERATION_SECONDS = 20 * 60
MAX_OVERLAY_SECONDS = 5 * 60
WAIT_SECONDS = 25.0

PENDING_TEXT = "正在为你制作第一条视频，大约需要 {eta} 秒。做好后会通知你。"
READY_TEXT = "这是你的第一条视频。想改风格、换文案，直接告诉我。"
READY_CHIPS = ("换个风格再做一条", "改成 30 秒", "帮我写一条抖音标题")
FAILED_TEXT = "这次没有生成成功。直接告诉我你想要什么样的视频，我再帮你做一条。"

_tasks: set[asyncio.Task] = set()
_finishing: set[str] = set()


class FirstVideoError(ValueError):
    code = "FIRST_VIDEO_INVALID"


@dataclass
class Outcome:
    job_id: str | None
    status: str  # completed | failed | cancelled | timeout | refused
    asset_id: str | None
    error: str | None


def job_key(workspace_id: str) -> str:
    return f"{KEY_PREFIX}{workspace_id}"


def is_first_video_job(job: Any) -> bool:
    return getattr(job, "kind", "") == "segment" and str(getattr(job, "idempotency_key", "") or "").startswith(KEY_PREFIX)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(text: str | None, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


# --- state ----------------------------------------------------------------

async def _pref(user_id: str) -> dict:
    from db.repository.preference_repo import PgPreferenceRepo
    row = await PgPreferenceRepo().get(user_id)
    extra = (row or {}).get("extra") or {}
    value = extra.get(PREF_KEY) if isinstance(extra, dict) else None
    return dict(value) if isinstance(value, dict) else {}


async def _write_pref(user_id: str, **changes) -> dict:
    from db.repository.preference_repo import PgPreferenceRepo
    current = await _pref(user_id)
    current.update(changes)
    current["updated_at"] = _now_iso()
    await PgPreferenceRepo().upsert(user_id, extra={PREF_KEY: current})
    return current


async def offer(user_id: str) -> None:
    """Registration hook: mark a new account as eligible. Never fails the registration."""
    try:
        if not await _pref(user_id):
            await _write_pref(user_id, status=OFFERED)
    except Exception as exc:
        log.warning("first video: offer not recorded user=%s error=%s", user_id, type(exc).__name__)


async def state(user_id: str, workspace_id: str) -> dict:
    pref = await _pref(user_id)
    status = pref.get("status") or "none"
    if status == OFFERED:
        status = "none"
    elif status not in STATUSES:
        status = "running"
    return {
        "status": status,
        "session_id": pref.get("session_id"),
        "job_id": pref.get("job_id"),
        "asset_id": pref.get("asset_id") if status == "ready" else None,
        "eta_seconds": first_video_templates().eta_seconds,
        "error": pref.get("error") if status == "failed" else None,
    }


async def _find_job(user_id: str, kind: str, key: str):
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.video_job import VideoJob
    async with get_db_session() as db:
        return (await db.execute(select(VideoJob).where(
            VideoJob.user_id == user_id, VideoJob.kind == kind, VideoJob.idempotency_key == key,
        ))).scalar_one_or_none()


async def _load_job(job_id: str):
    from db.base import get_db_session
    from db.models.video_job import VideoJob
    async with get_db_session() as db:
        return await db.get(VideoJob, job_id)


# --- submit ---------------------------------------------------------------

def _summary(templates, industry: str, shop_name: str, headline: str, template: str, has_image: bool) -> str:
    parts = [f"行业：{templates.label('industry', industry)}"]
    if shop_name:
        parts.append(f"店名：{shop_name}")
    if headline:
        parts.append(f"主打：{headline}")
    parts.append(f"模板：{templates.label('template', template)}")
    parts.append("参考图：" + ("上传了一张" if has_image else "没有"))
    return "【首条视频】" + "；".join(parts) + f"。请做一条 {templates.duration_seconds} 秒的竖屏视频。"


async def submit(*, user_id: str, workspace_id: str, industry: str, shop_name: str | None,
                 headline: str | None, template: str, asset_id: str | None,
                 project_id: str | None = None) -> dict:
    templates = first_video_templates()
    if template not in templates.template_ids():
        raise FirstVideoError("unknown template")
    if industry not in templates.industry_ids():
        industry = "other"
    pref = await _pref(user_id)
    if pref.get("status") in ("submitted", "running", "ready", "failed") \
            or await _find_job(user_id, "segment", job_key(workspace_id)) is not None:
        return await state(user_id, workspace_id)

    shop_name = _clean(shop_name, 40)
    headline = _clean(headline, 40)
    from tool.tool import ToolContext
    reference: str | None = None
    if asset_id:
        from tool import video_production as vp
        row = await vp._find_owned_asset(asset_id, ToolContext(user_id=user_id, workspace_id=workspace_id))
        if row is None or not str(row.mime or "").startswith("image/"):
            raise FirstVideoError("asset_id must be one of your ready images")
        reference = row.id

    from assistant import profile
    if industry in profile.BUSINESSES:
        try:
            await profile.save(user_id, {"business": industry}, via="intro")
        except Exception as exc:  # the video does not depend on the profile
            log.warning("first video: business not saved user=%s error=%s", user_id, type(exc).__name__)

    from agent.agent import default_agent_name
    from agent.model_resolve import resolve as resolve_model
    from api.v1.quality import resolve_quality
    from core.config import get_config
    from session import session as session_mod
    config = get_config()
    video_model, video_resolution = resolve_quality(templates.tier, config)
    model, _ = resolve_model("", config, context="first video")
    session = await session_mod.create_session(
        model=model, agent=default_agent_name(), title=SESSION_TITLE,
        user_id=user_id, workspace_id=workspace_id, project_id=project_id,
    )
    await session_mod.update_session(session.id, user_id=user_id,
                                     video_model=video_model, video_resolution=video_resolution)
    user_message = await session_mod.create_user_message(
        session_id=session.id, text=_summary(templates, industry, shop_name, headline, template, reference is not None),
        agent=session.agent, model=model, user_id=user_id,
        origin="human", origin_ref={"actor_user_id": user_id, "entrypoint": ENTRYPOINT},
    )
    assistant = await session_mod.create_assistant_message(
        session.id, user_message.id, model_id=model, agent=session.agent, user_id=user_id,
    )
    await _say(session.id, assistant.id, user_id, PENDING_TEXT.format(eta=templates.eta_seconds), channel="commentary")
    await _write_pref(
        user_id, status="submitted", workspace_id=workspace_id, session_id=session.id,
        message_id=assistant.id, project_id=session.project_id, industry=industry, template=template,
        shop_name=shop_name, headline=headline, reference_asset_id=reference,
        job_id=None, asset_id=None, error=None, retried=False,
    )

    from billing.media import quote_generation
    from billing.signup_gift import ensure_first_video_credits
    try:
        quote = quote_generation(video_model or "", video_resolution, templates.duration_seconds)
        await ensure_first_video_credits(workspace_id, user_id, quote.credits)
    except Exception as exc:  # the gate may still pass on the weekly allowance
        log.warning("first video: top-up skipped workspace=%s error=%s", workspace_id, type(exc).__name__)

    ctx = ToolContext(session_id=session.id, user_id=user_id, workspace_id=workspace_id,
                      project_id=session.project_id or "", message_id=assistant.id)
    _spawn(_drive(ctx, key=job_key(workspace_id),
                  prompt=templates.prompt(industry, template, with_image=reference is not None),
                  reference=reference,
                  fallback_prompt=templates.prompt(industry, template, with_image=False)))
    return await state(user_id, workspace_id)


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


# --- the pipeline ----------------------------------------------------------

async def _drive(ctx, *, key: str, prompt: str, reference: str | None, fallback_prompt: str) -> None:
    try:
        outcome = await _generate(ctx, key=key, prompt=prompt, reference=reference)
        if outcome.status in ("failed", "cancelled", "refused") and reference:
            # A photo with a face or a hand is the usual rejection; the shop
            # still gets its video, from the text-only template.
            log.info("first video: retrying without the reference image user=%s", ctx.user_id)
            await _write_pref(ctx.user_id, retried=True)
            outcome = await _generate(ctx, key=key + RETRY_SUFFIX, prompt=fallback_prompt, reference=None)
        await _complete(ctx, outcome)
    except Exception as exc:
        log.warning("first video: pipeline failed user=%s error=%s: %s", ctx.user_id, type(exc).__name__, exc)
        await _mark_failed(ctx, f"{type(exc).__name__}")


async def _generate(ctx, *, key: str, prompt: str, reference: str | None) -> Outcome:
    from tool import video_production as vp
    from tool.video_production import VideoGenerateArgs, VideoInputRef
    templates = first_video_templates()
    args = VideoGenerateArgs(
        action="submit", prompt=prompt, idempotency_key=key, duration=templates.duration_seconds,
        ratio=templates.ratio, allow_duplicate=True,
        input_assets=[VideoInputRef(asset_id=reference, role="reference_image")] if reference else [],
    )
    result = await vp.execute_generate(args, ctx)
    meta = result.metadata or {}
    job_id = meta.get("job_id")
    if not job_id:
        return Outcome(None, "refused", None, f"{result.title}: {result.output}"[:400])
    await _write_pref(ctx.user_id, status="running", job_id=job_id)
    status, asset_id = meta.get("status"), meta.get("asset_id")
    deadline = time.monotonic() + MAX_GENERATION_SECONDS
    while status not in ("completed", "failed", "cancelled") and time.monotonic() < deadline:
        waited = await vp.execute_generate(
            VideoGenerateArgs(action="wait", job_id=job_id, wait_seconds=WAIT_SECONDS, wait_iteration=1), ctx)
        meta = waited.metadata or {}
        status = meta.get("status") or status
        asset_id = meta.get("asset_id") or asset_id
        if status not in ("completed", "failed", "cancelled"):
            await asyncio.sleep(2)
    if status == "completed":
        if not asset_id:
            job = await _load_job(job_id)
            asset_id = job.output_asset_id if job else None
        return Outcome(job_id, "completed", asset_id, None)
    if status in ("failed", "cancelled"):
        job = await _load_job(job_id)
        return Outcome(job_id, status, None, (job.error if job else None) or status)
    return Outcome(job_id, "timeout", None, "generation did not finish within the inline budget")


async def _complete(ctx, outcome: Outcome) -> None:
    if outcome.status == "timeout":
        # The job is still the provider's; video/job_recovery.py finishes it
        # through on_job_settled once it lands.
        log.info("first video: left to recovery user=%s job=%s", ctx.user_id, outcome.job_id)
        return
    if outcome.status != "completed":
        await _mark_failed(ctx, outcome.error)
        return
    await _deliver(ctx, generated_asset_id=outcome.asset_id, job_id=outcome.job_id or "")


async def _deliver(ctx, *, generated_asset_id: str | None, job_id: str) -> None:
    """Overlay the shop name when there is one, then tell the person and the phone."""
    if job_id in _finishing:
        return
    _finishing.add(job_id)
    try:
        pref = await _pref(ctx.user_id)
        if pref.get("status") in TERMINAL:
            return
        final_asset = generated_asset_id
        shop_name = pref.get("shop_name") or ""
        if shop_name and generated_asset_id:
            templates = first_video_templates()
            slogan = pref.get("headline") or templates.entry(pref.get("industry") or "other", pref.get("template") or "signature").slogan
            overlaid = await _overlay(ctx, generated_asset_id, shop_name=shop_name, slogan=slogan,
                                      key=f"{TEXT_KEY_PREFIX}{job_id}")
            if overlaid:
                final_asset = overlaid
        await _say(ctx.session_id, ctx.message_id, ctx.user_id,
                   READY_TEXT + "\n\n" + "\n".join(f"- {chip}" for chip in READY_CHIPS))
        await _finish_message(ctx.session_id, ctx.message_id, ctx.user_id)
        await _write_pref(ctx.user_id, status="ready", asset_id=final_asset, job_id=job_id, error=None)
        await _notify(ctx.user_id, ctx.workspace_id or pref.get("workspace_id") or "", ctx.session_id,
                      kind="first_video_ready", name=shop_name)
    finally:
        _finishing.discard(job_id)


async def _overlay(ctx, asset_id: str, *, shop_name: str, slogan: str, key: str) -> str | None:
    from tool.video_compose import VideoComposeArgs, execute_compose
    templates = first_video_templates()
    end = float(templates.duration_seconds) - 0.2
    texts = [{"text": shop_name, "from_sec": 0.6, "to_sec": end, "y": 0.76,
              "style": {"size": 64, "outline": 4}}]
    if slogan:
        texts.append({"text": slogan, "from_sec": 1.2, "to_sec": end, "y": 0.835,
                      "style": {"size": 38, "outline": 3}})
    timeline = {"version": 1, "shots": [{"asset": asset_id, "duration_sec": float(templates.duration_seconds)}],
                "texts": texts}
    try:
        result = await execute_compose(VideoComposeArgs(action="submit", timeline=timeline, idempotency_key=key,
                                                        filename="first-video.mp4"), ctx)
        meta = result.metadata or {}
        job_id = meta.get("job_id")
        if not job_id:
            log.info("first video: overlay refused user=%s: %s", ctx.user_id, result.title)
            return None
        status, out_asset = meta.get("status"), meta.get("asset_id")
        deadline = time.monotonic() + MAX_OVERLAY_SECONDS
        while status not in ("completed", "failed", "cancelled") and time.monotonic() < deadline:
            waited = await execute_compose(
                VideoComposeArgs(action="wait", job_id=job_id, wait_seconds=WAIT_SECONDS, wait_iteration=1), ctx)
            meta = waited.metadata or {}
            status = meta.get("status") or status
            out_asset = meta.get("asset_id") or out_asset
            if status not in ("completed", "failed", "cancelled"):
                await asyncio.sleep(2)
        return out_asset if status == "completed" else None
    except Exception as exc:  # the plain clip is still a first video
        log.warning("first video: overlay failed user=%s error=%s", ctx.user_id, type(exc).__name__)
        return None


async def _mark_failed(ctx, error: str | None) -> None:
    pref = await _pref(ctx.user_id)
    if pref.get("status") in TERMINAL:
        return
    try:
        await _say(ctx.session_id, ctx.message_id, ctx.user_id, FAILED_TEXT)
        await _finish_message(ctx.session_id, ctx.message_id, ctx.user_id)
    except Exception as exc:
        log.warning("first video: failure note not written user=%s error=%s", ctx.user_id, type(exc).__name__)
    await _write_pref(ctx.user_id, status="failed",
                      error={"code": "GENERATION_FAILED", "message": _clean(error, 200) or "generation failed"})
    await _notify(ctx.user_id, ctx.workspace_id or pref.get("workspace_id") or "", ctx.session_id,
                  kind="first_video_failed", name=pref.get("shop_name") or "")


# --- messages and notifications -------------------------------------------

async def _say(session_id: str, message_id: str, user_id: str, text: str, *, channel: str = "final") -> None:
    from models.message import TextPart
    from session.session import save_part
    await save_part(TextPart(text=text, channel=channel, session_id=session_id, message_id=message_id),
                    is_new=True, user_id=user_id)


async def _finish_message(session_id: str, message_id: str, user_id: str) -> None:
    from models.message import MessageInfo
    from session.session import update_message_info
    await update_message_info(MessageInfo(id=message_id, session_id=session_id, role="assistant", finish="stop"),
                              user_id=user_id)


async def _notify(user_id: str, workspace_id: str, session_id: str, *, kind: str, name: str = "") -> None:
    if not workspace_id:
        return
    from db.base import get_db_session
    from notifications.events import emit
    try:
        async with get_db_session() as db:
            await emit(db, user_id=user_id, workspace_id=workspace_id, kind=kind,
                       event_key=f"{kind}:{workspace_id}", name=name, session_id=session_id, ttl_seconds=3600)
    except Exception as exc:  # the result is in the chat either way
        log.warning("first video: notification not sent user=%s kind=%s error=%s", user_id, kind, type(exc).__name__)


# --- recovery ---------------------------------------------------------------

async def on_job_settled(job) -> None:
    """A first-video job reached completed/failed without its driver (process restart)."""
    pref = await _pref(job.user_id)
    if pref.get("status") in TERMINAL or pref.get("session_id") != job.session_id or not pref.get("message_id"):
        return
    from tool.tool import ToolContext
    ctx = ToolContext(session_id=job.session_id, user_id=job.user_id,
                      workspace_id=pref.get("workspace_id") or "", project_id=pref.get("project_id") or "",
                      message_id=pref["message_id"])
    if job.status == "completed":
        from tool import video_production as vp
        await vp._attach_completed(job, ctx)
        await _deliver(ctx, generated_asset_id=job.output_asset_id, job_id=job.id)
        await _mark_settled(job)
        return
    if job.status not in ("failed", "cancelled"):
        return
    await _mark_settled(job)
    key = str(job.idempotency_key or "")
    if pref.get("reference_asset_id") and not pref.get("retried") and not key.endswith(RETRY_SUFFIX):
        templates = first_video_templates()
        await _write_pref(job.user_id, retried=True)
        _spawn(_drive(ctx, key=key + RETRY_SUFFIX,
                      prompt=templates.prompt(pref.get("industry") or "other", pref.get("template") or "signature",
                                              with_image=False),
                      reference=None, fallback_prompt=""))
        return
    await _mark_failed(ctx, job.error)


SETTLED_FLAG = "first_video_settled"


async def _mark_settled(job) -> None:
    """Remember that this job's outcome reached the person, so the sweep stops re-reading it."""
    from tool import video_production as vp
    await vp._update_job(job.id, result_data={**(job.result_data or {}), SETTLED_FLAG: True})


async def sweep_unfinished() -> int:
    """Recent first-video jobs that settled but were never delivered; returns how many were handled."""
    from datetime import timedelta
    from sqlalchemy import JSON, or_, select, type_coerce
    from db.base import get_db_session
    from db.models.video_job import VideoJob
    since = datetime.now(timezone.utc) - timedelta(days=2)
    async with get_db_session() as db:
        rows = list((await db.scalars(select(VideoJob).where(
            VideoJob.kind == "segment",
            VideoJob.idempotency_key.like(f"{KEY_PREFIX}%"),
            VideoJob.status.in_(("completed", "failed", "cancelled")),
            VideoJob.completed_at >= since,
            or_(VideoJob.status != "completed", VideoJob.attached_message_id.is_(None)),
            ~type_coerce(VideoJob.result_data, JSON)[SETTLED_FLAG].as_boolean().is_(True),
        ).order_by(VideoJob.completed_at).limit(20))).all())
    handled = 0
    for job in rows:
        try:
            await on_job_settled(job)
            handled += 1
        except Exception as exc:
            log.warning("first video: recovery failed job=%s error=%s", job.id, type(exc).__name__)
    return handled
