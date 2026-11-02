"""省心托管: keep doing a Light result on a schedule (docs/LIGHT_MODE_PLAN.md §4.3, IMPL §4.4).

A switch on a result card turns into an ordinary cron job whose ``template``
is a :class:`LightAutopilotTemplate` — the three answers (when, make only or
make and publish, the per-run credit cap) plus a *recipe* lifted from the
result itself (the clip's prompt, reference photo, shop name and slogan, or
the image's prompt).

Runs that only make something need no desktop and no agent turn: the
scheduler calls :func:`run`, which posts a new clip (or image) into the same
Light conversation through the real tools, inside the cap, with a fresh camera
variation each time so the provider's content cache cannot hand back
yesterday's clip. Publishing and the shop tasks (上下架 / 回评价 / 成绩复盘) need
the desktop; they run on the ordinary agent path with the cap enforced on the
run's session, and only paid workspaces can create them.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.log import create_logger
from light.mode import LightError

log = create_logger(__name__)

TEMPLATE_KIND = "light_autopilot"
KEY_PREFIX = "light_autopilot:"
TIMEZONE = "Asia/Shanghai"
DEFAULT_TIME = "10:00"
DEFAULT_CAP = Decimal("10")
MEDIA_TASKS = ("video", "image")
DESKTOP_TASKS = ("listing", "reviews", "report")
REPEATS = {"daily": "*", "weekdays": "1-5", "weekly": "1"}
REPEAT_LABELS = {"daily": "每天", "weekdays": "工作日", "weekly": "每周一"}
TASK_LABELS = {"video": "做一条视频", "image": "做一张图", "listing": "上下架", "reviews": "回评价",
               "report": "成绩复盘"}
#: One per run, in turn: the same look, a different camera, so every day's clip is new.
VARIATIONS = (
    "这一次镜头从侧面缓慢推近。",
    "这一次以俯拍开场，再慢慢拉近到细节。",
    "这一次节奏更快，用两三个特写短切。",
    "这一次带一点环境与人气的中远景，再落到主体。",
    "这一次以细节特写开场，最后拉远看全貌。",
    "这一次镜头环绕主体半圈，光线更暖。",
)
DONE_TEXT = "今天这条做好了（{when}自动做的）。不想要了，在「我的 → 托管中的事」里暂停或取消。"
SKIP_CAP_TEXT = "今天这条没做：预计 {quote} 积分，超过了你设的每次上限 {cap} 积分。可以在「托管中的事」里调高上限。"
SKIP_BALANCE_TEXT = "今天这条没做：积分不够了。充值或等每周额度到账后会接着做。"
FAILED_TEXT = "今天这条没做成，明天到点会再试一次。"


class Recipe(BaseModel):
    """What a run makes, read off the result the switch was flipped on."""

    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=4000)
    duration: int = Field(default=15, ge=4, le=30)
    ratio: str = Field(default="9:16", max_length=16)
    reference_asset_id: str | None = Field(default=None, max_length=96)
    shop_name: str = Field(default="", max_length=40)
    slogan: str = Field(default="", max_length=40)
    #: Images only.
    size: str | None = Field(default=None, max_length=16)


class When(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repeat: Literal["daily", "weekdays", "weekly"] = "daily"
    time: str = Field(default=DEFAULT_TIME, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    def cron_expr(self) -> str:
        hour, minute = (int(part) for part in self.time.split(":"))
        return f"{minute} {hour} * * {REPEATS[self.repeat]}"

    def label(self) -> str:
        return f"{REPEAT_LABELS[self.repeat]} {self.time}"


class LightAutopilotTemplate(BaseModel):
    """Stored verbatim in ``cron_jobs.template``; strict so a typo is a refused job."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["light_autopilot"] = TEMPLATE_KIND
    version: Literal[1] = 1
    task: Literal["video", "image", "listing", "reviews", "report"]
    mode: Literal["make_only", "make_and_publish"] = "make_only"
    credits_cap_per_run: Decimal = Field(gt=0, le=Decimal("500"))
    when: When = Field(default_factory=When)
    session_id: str = Field(min_length=1, max_length=96)
    entry_id: str | None = Field(default=None, max_length=32)
    source_asset_id: str | None = Field(default=None, max_length=96)
    recipe: Recipe | None = None
    note: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _media_tasks_have_a_recipe(self) -> "LightAutopilotTemplate":
        if self.task in MEDIA_TASKS and self.recipe is None:
            raise ValueError(f"a {self.task} autopilot needs a recipe")
        if self.task == "image" and self.mode != "make_only":
            raise ValueError("images are only made, never published")
        return self

    @property
    def needs_desktop(self) -> bool:
        return self.mode == "make_and_publish" or self.task in DESKTOP_TASKS

    def public(self) -> dict:
        data = self.model_dump(mode="json")
        data["credits_cap_per_run"] = _plain(self.credits_cap_per_run)
        return data


def parse(template: dict | None) -> LightAutopilotTemplate | None:
    if not template or template.get("kind") != TEMPLATE_KIND:
        return None
    return LightAutopilotTemplate.model_validate(template)


def is_light_autopilot(job: dict) -> bool:
    return isinstance(job.get("template"), dict) and job["template"].get("kind") == TEMPLATE_KIND


def runs_without_desktop(job: dict) -> bool:
    try:
        template = parse(job.get("template"))
    except Exception:
        return False
    return template is not None and not template.needs_desktop


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text


# --- recipes ------------------------------------------------------------------

async def _asset_id_for(reference: str, user_id: str) -> str | None:
    """A compose shot names its source by asset id or by its OSS URL; return the asset id."""
    if reference.startswith("asset_"):
        return reference
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    path = unquote(urlsplit(reference).path).lstrip("/")
    if reference.startswith("oss://"):
        path = path.split("/", 1)[1] if "/" in path else path
    async with get_db_session() as db:
        return await db.scalar(select(FileAsset.id).where(FileAsset.oss_key == path, FileAsset.user_id == user_id))


async def recipe_for_asset(user_id: str, session_id: str, asset_id: str) -> tuple[str, Recipe]:
    """(task, recipe) behind a result in this conversation; LightError when there is none."""
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_job import VideoJob
    async with get_db_session() as db:
        asset = await db.get(FileAsset, asset_id)
        if asset is None or asset.user_id != user_id:
            raise LightError("LIGHT_AUTOPILOT_SOURCE", "that result is not yours")
        job = await db.scalar(select(VideoJob).where(VideoJob.output_asset_id == asset_id,
                                                     VideoJob.user_id == user_id))
    if job is not None and job.kind == "compose":
        timeline = (job.request_data or {}).get("timeline") or {}
        shots, texts = timeline.get("shots") or [], timeline.get("texts") or []
        base_asset = await _asset_id_for(str((shots[0] if shots else {}).get("asset") or ""), user_id)
        async with get_db_session() as db:
            base = await db.scalar(select(VideoJob).where(VideoJob.output_asset_id == base_asset,
                                                          VideoJob.user_id == user_id)) if base_asset else None
        if base is None or not base.prompt:
            raise LightError("LIGHT_AUTOPILOT_SOURCE", "this cut has no single generated clip to repeat")
        recipe = _clip_recipe(base)
        recipe.shop_name = str((texts[0] if texts else {}).get("text") or "")[:40]
        recipe.slogan = str((texts[1] if len(texts) > 1 else {}).get("text") or "")[:40]
        return "video", recipe
    if job is not None and job.kind == "segment" and job.prompt:
        return "video", _clip_recipe(job)
    if str(asset.mime or "").startswith("image/"):
        prompt, size = await _image_prompt(session_id, asset_id)
        if prompt:
            return "image", Recipe(prompt=prompt[:4000], size=size)
    raise LightError("LIGHT_AUTOPILOT_SOURCE", "cannot tell how this result was made")


def _clip_recipe(job) -> Recipe:
    request = job.request_data or {}
    prompt = re.sub(r"这一次[^。]*。$", "", str(job.prompt or "")).strip()
    inputs = request.get("input_asset_ids") or []
    duration = request.get("duration")
    return Recipe(prompt=prompt[:4000], duration=int(duration) if isinstance(duration, int) and duration > 0 else 15,
                  ratio=str(request.get("ratio") or "9:16"),
                  reference_asset_id=inputs[0] if inputs else None)


async def _image_prompt(session_id: str, asset_id: str) -> tuple[str | None, str | None]:
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.part import Part
    async with get_db_session() as db:
        rows = (await db.scalars(select(Part).where(Part.session_id == session_id, Part.type == "tool")
                                 .order_by(Part.created_at.desc()).limit(200))).all()
    for part in rows:
        data = part.data or {}
        if data.get("tool") != "image_gen":
            continue
        state = data.get("state") or {}
        meta = state.get("metadata") or {}
        ids = meta.get("asset_ids") or ([meta["asset_id"]] if meta.get("asset_id") else [])
        if asset_id in ids:
            args = state.get("input") or {}
            return args.get("prompt"), args.get("size")
    return None, None


# --- create / list / change ------------------------------------------------------

def _session_scope(session, user_id: str, workspace_id: str) -> None:
    if (session is None or session.user_id != user_id or session.is_deleted
            or session.workspace_id != workspace_id or session.kind != "normal"):
        raise LightError("LIGHT_AUTOPILOT_SESSION", "no such conversation", status=404)


async def create(*, user_id: str, workspace_id: str, session_id: str, asset_id: str | None = None,
                 task: str | None = None, repeat: str = "daily", time: str = DEFAULT_TIME,
                 mode: str = "make_only", cap: Decimal = DEFAULT_CAP, note: str = "") -> dict:
    from db.base import get_db_session
    from db.models.session import Session
    async with get_db_session() as db:
        session = await db.get(Session, session_id)
    _session_scope(session, user_id, workspace_id)

    recipe = None
    if asset_id:
        task_from_asset, recipe = await recipe_for_asset(user_id, session_id, asset_id)
        task = task or task_from_asset
    if task not in TASK_LABELS:
        raise LightError("LIGHT_AUTOPILOT_TASK", "say what to keep doing")
    try:
        template = LightAutopilotTemplate(
            task=task, mode=mode, credits_cap_per_run=cap, when=When(repeat=repeat, time=time),
            session_id=session_id, entry_id=session.light_entry, source_asset_id=asset_id, recipe=recipe,
            note=note)
    except ValueError as exc:
        raise LightError("LIGHT_AUTOPILOT_INVALID", str(exc).splitlines()[0][:200]) from exc

    from light.mode import _workspace_facts
    facts = await _workspace_facts(workspace_id)
    if template.needs_desktop and not facts["desktop"]:
        # Free plans have no desktop: only 只出片 is offered (plan §8 #11).
        raise LightError("LIGHT_AUTOPILOT_NEEDS_DESKTOP", "publishing and shop tasks need a cloud desktop",
                         status=409, availability="needs_desktop" if facts["paid_plan"] else "needs_plan")

    existing = [job for job in await _jobs(user_id, workspace_id)
                if (job.get("template") or {}).get("session_id") == session_id
                and (job.get("template") or {}).get("source_asset_id") == asset_id
                and (job.get("template") or {}).get("task") == task]
    if existing:
        return {**view(existing[0]), "created": False}

    from cron.service import cron_service
    from cron.types import CronDeliveryConfig, CronJobCreate, CronScheduleCron
    name = f"省心托管 · {TASK_LABELS[task]}"
    created = await cron_service.add(user_id, CronJobCreate(
        project_id=session.project_id, session_id=session_id, name=name,
        description=f"{template.when.label()} · 每次最多 {_plain(template.credits_cap_per_run)} 积分",
        schedule=CronScheduleCron(expr=template.when.cron_expr(), tz=TIMEZONE),
        task_prompt=_task_prompt(template), delivery=CronDeliveryConfig(notifications_enabled=True),
        max_retries=1, template=template.public(),
    ), workspace_id=workspace_id)
    job = await _job(user_id, workspace_id, created["id"])
    return {**view(job), "created": True}


def _task_prompt(template: LightAutopilotTemplate) -> str:
    """For the agent path (publishing, shop tasks); make-only runs never read it."""
    base = {
        "video": "按「模版参数」里的 recipe 做今天这一条视频（同样的样子，换一个镜头），叠上店名",
        "image": "按 recipe 再做一张同样子的图",
        "listing": "按老板之前交代的上下架安排，到平台把商品上架或下架，留截图",
        "reviews": "去平台看新评价，按店里一贯的口吻回复，留截图",
        "report": "看近 7 天每条视频的数据和账号健康，写一段这周看下来，并给出下一条做什么",
    }[template.task]
    publish = "；做好后发到抖音（先做发布前检查，缺登录态就只出片并说明）" if template.mode == "make_and_publish" else ""
    return (f"【省心托管】{base}{publish}。这是定时自动运行，没有人回答问题：不要调用 question，"
            f"每次最多花 {_plain(template.credits_cap_per_run)} 积分，超了就停下并说明。结果用一两句白话交代。")


async def _jobs(user_id: str, workspace_id: str) -> list[dict]:
    from cron.service import cron_service
    jobs = await cron_service.list_jobs(user_id, workspace_id=workspace_id)
    return [job for job in jobs if is_light_autopilot(job)]


async def _job(user_id: str, workspace_id: str, job_id: str) -> dict:
    job = next((job for job in await _jobs(user_id, workspace_id) if job["id"] == job_id), None)
    if job is None:
        raise LightError("LIGHT_AUTOPILOT_UNKNOWN", "no such autopilot", status=404)
    return job


def view(job: dict) -> dict:
    template = job.get("template") or {}
    when = template.get("when") or {}
    recipe = template.get("recipe") or {}
    return {
        "id": job["id"], "name": job.get("name") or "", "enabled": bool(job.get("enabled")),
        "task": template.get("task"), "mode": template.get("mode"), "entry_id": template.get("entry_id"),
        "session_id": template.get("session_id") or job.get("session_id"),
        "source_asset_id": template.get("source_asset_id"),
        "repeat": when.get("repeat"), "time": when.get("time"),
        "credits_cap_per_run": template.get("credits_cap_per_run"),
        "shop_name": recipe.get("shop_name") or "",
        "next_run_at": job.get("next_run_at"), "last_run_at": job.get("last_run_at"),
        "last_status": job.get("last_status"), "last_error": job.get("last_error"),
        "total_runs": job.get("total_runs") or 0, "running": bool(job.get("running")),
    }


async def list_view(user_id: str, workspace_id: str) -> dict:
    jobs = await _jobs(user_id, workspace_id)
    return {"items": [view(job) for job in jobs]}


async def set_enabled(user_id: str, workspace_id: str, job_id: str, enabled: bool) -> dict:
    await _job(user_id, workspace_id, job_id)
    from cron.service import cron_service
    from cron.types import CronJobUpdate
    await cron_service.update(job_id, user_id, CronJobUpdate(enabled=enabled), workspace_id=workspace_id)
    return view(await _job(user_id, workspace_id, job_id))


async def update(user_id: str, workspace_id: str, job_id: str, *, repeat: str | None = None,
                 time: str | None = None, cap: Decimal | None = None) -> dict:
    job = await _job(user_id, workspace_id, job_id)
    template = LightAutopilotTemplate.model_validate(job["template"])
    when = When(repeat=repeat or template.when.repeat, time=time or template.when.time)
    try:
        template = template.model_copy(update={"when": when, "credits_cap_per_run": cap or template.credits_cap_per_run})
        LightAutopilotTemplate.model_validate(template.model_dump())
    except ValueError as exc:
        raise LightError("LIGHT_AUTOPILOT_INVALID", str(exc).splitlines()[0][:200]) from exc
    from cron.service import cron_service
    from cron.types import CronJobUpdate, CronScheduleCron
    await cron_service.update(job_id, user_id, CronJobUpdate(
        schedule=CronScheduleCron(expr=when.cron_expr(), tz=TIMEZONE), template=template.public(),
        description=f"{when.label()} · 每次最多 {_plain(template.credits_cap_per_run)} 积分",
    ), workspace_id=workspace_id)
    return view(await _job(user_id, workspace_id, job_id))


async def cancel(user_id: str, workspace_id: str, job_id: str) -> dict:
    await _job(user_id, workspace_id, job_id)
    from cron.service import cron_service
    await cron_service.remove(job_id, user_id, workspace_id=workspace_id)
    return {"ok": True}


# --- a make-only run ------------------------------------------------------------

async def run(job: dict) -> dict:
    """One scheduled run without a desktop; returns the executor's result dict."""
    from cron.executor import _create_run_entry, _update_run_entry
    from core.identifier import ascending
    started = datetime.now(timezone.utc)
    run_id = ascending("cron_run")
    await _create_run_entry(run_id, job, started)
    try:
        text, status, error = await _make(job, run_id)
    except Exception as exc:
        log.warning("light autopilot run failed job=%s error=%s: %s", job.get("id"), type(exc).__name__, exc)
        text, status, error = FAILED_TEXT, "error", f"{type(exc).__name__}"
    ended = datetime.now(timezone.utc)
    duration_ms = int((ended - started).total_seconds() * 1000)
    await _update_run_entry(run_id, job["id"], None, status=status, summary_text=text,
                            error_message=error, ended_at=ended, duration_ms=duration_ms, injected=True)
    result: dict[str, Any] = {"status": status, "summary_text": text, "run_id": run_id}
    if error:
        result["error"] = error
    return result


async def _make(job: dict, run_id: str) -> tuple[str, str, str | None]:
    template = LightAutopilotTemplate.model_validate(job["template"])
    session_id = template.session_id
    user_id = job["user_id"]
    workspace_id = job.get("workspace_id") or ""
    from db.base import get_db_session
    from db.models.session import Session
    async with get_db_session() as db:
        session = await db.get(Session, session_id)
    if session is None or session.is_deleted:
        return "", "error", "SESSION_GONE"

    quote = await _quote(template, session)
    from light import chat
    user_text = f"【省心托管】{template.when.label()}，{TASK_LABELS[template.task]}"
    if quote is not None and quote > template.credits_cap_per_run:
        await chat.exchange(session_id, user_id, user_text=user_text,
                            assistant_text=SKIP_CAP_TEXT.format(quote=_plain(quote), cap=_plain(template.credits_cap_per_run)),
                            agent=session.agent, model=None, entrypoint="light_autopilot")
        return SKIP_CAP_TEXT.format(quote=_plain(quote), cap=_plain(template.credits_cap_per_run)), "skipped", "OVER_CAP"
    if not await _has_balance(workspace_id):
        await chat.exchange(session_id, user_id, user_text=user_text, assistant_text=SKIP_BALANCE_TEXT,
                            agent=session.agent, model=None, entrypoint="light_autopilot")
        return SKIP_BALANCE_TEXT, "skipped", "INSUFFICIENT_CREDITS"

    from session import session as session_mod
    user_message = await session_mod.create_user_message(
        session_id=session_id, text=user_text, agent=session.agent, model=None, synthetic=True, user_id=user_id,
        origin="human", origin_ref={"actor_user_id": user_id, "entrypoint": "light_autopilot"},
    )
    assistant = await session_mod.create_assistant_message(session_id, user_message.id, model_id=None,
                                                           agent=session.agent, user_id=user_id)
    from tool.tool import ToolContext
    ctx = ToolContext(session_id=session_id, user_id=user_id, workspace_id=workspace_id,
                      project_id=session.project_id or "", message_id=assistant.id)
    # One clip (plus its name overlay) or one image: the quote above already
    # kept the run inside the cap, so the conversation's own cap is untouched.
    made = await (_make_video(ctx, template, run_id) if template.task == "video"
                  else _make_image(ctx, template, run_id))
    text = DONE_TEXT.format(when=template.when.label()) if made else FAILED_TEXT
    await chat.say(session_id, assistant.id, user_id, text)
    await chat.finish(session_id, assistant.id, user_id)
    return text, ("ok" if made else "error"), (None if made else "NOT_MADE")


async def _make_video(ctx, template: LightAutopilotTemplate, run_id: str) -> bool:
    from light import pipeline
    recipe = template.recipe
    variation = VARIATIONS[_run_number(run_id) % len(VARIATIONS)]
    key = f"{KEY_PREFIX}{run_id}"
    outcome = await pipeline.generate(ctx, key=key, prompt=f"{recipe.prompt}{variation}",
                                      reference=recipe.reference_asset_id, duration=recipe.duration,
                                      ratio=recipe.ratio)
    if outcome.status == "failed" and recipe.reference_asset_id:
        outcome = await pipeline.generate(ctx, key=key + ":text", prompt=f"{recipe.prompt}{variation}",
                                          reference=None, duration=recipe.duration, ratio=recipe.ratio)
    if outcome.status != "completed" or not outcome.asset_id:
        log.info("light autopilot: clip not made run=%s status=%s", run_id, outcome.status)
        return False
    if recipe.shop_name:
        await pipeline.overlay(ctx, outcome.asset_id, shop_name=recipe.shop_name, slogan=recipe.slogan,
                               key=f"{KEY_PREFIX}text:{run_id}", duration=recipe.duration,
                               filename="省心托管.mp4")
    return True


async def _make_image(ctx, template: LightAutopilotTemplate, run_id: str) -> bool:
    from tool.image_gen import ImageGenArgs, execute
    recipe = template.recipe
    result = await execute(ImageGenArgs(prompt=recipe.prompt, size=recipe.size or None, n=1), ctx)
    meta = result.metadata or {}
    return bool(meta.get("asset_ids") or meta.get("asset_id")) and not meta.get("error")


def _run_number(run_id: str) -> int:
    return sum(ord(ch) for ch in run_id)


async def _quote(template: LightAutopilotTemplate, session) -> Decimal | None:
    try:
        if template.task == "video":
            from billing.media import quote_generation
            quote = quote_generation(session.video_model or "", session.video_resolution, template.recipe.duration)
            overlay = Decimal("0.05") if template.recipe.shop_name else Decimal(0)
            return Decimal(str(quote.credits)) + overlay if quote.credits is not None else None
        return Decimal("1")
    except Exception as exc:  # no quote: let the run's own gates decide
        log.info("light autopilot: no quote error=%s", type(exc).__name__)
        return None


async def _has_balance(workspace_id: str) -> bool:
    from billing.service import BillingError, precheck_balance
    try:
        await precheck_balance(workspace_id)
        return True
    except BillingError:
        return False
