"""Publish one finished video to 抖音创作者中心 through the workspace's cloud desktop.

Order of gates (plan §7 C2/C3/C4): mode (breaker → request → default) →
login state of the `douyin_creator` desktop site → human-paced budget →
asset ownership → desktop Chrome with the person's profile → the script.
Every attempt is a `publish_jobs` row (`platform=douyin_creator`), including
dry runs (`status=draft`, not counted) and failures. A risk signal trips the
account's breaker, writes a notification, and tells the caller to degrade
to the QR package route; the breaker is only reset by a person.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from core.identifier import ascending as generate_id
from core.log import create_logger
from db.base import get_db_session
from db.models.platform_account import PlatformAccount
from db.models.publish_job import PublishJob
from publish import desktop_policy as policy
from publish import desktop_script as script

log = create_logger("publish.desktop")

SITE_KEY = "douyin_creator"
NOTIFY_DEGRADED = "desktop_publish_degraded"
NOTIFY_LOGIN = "desktop_login_expired"
NOTIFY_DONE = "publish_done"


class PublishRefusal(Exception):
    """Actionable reason.

    Exactly one of three shapes, so the caller never has to guess the route:
    * ``degrade=True``     — the desktop route is off for this account (switch or
                             breaker): produce the QR package instead.
    * ``login_expired``    — the person must re-login on the desktop; the video waits.
    * ``retryable=True``   — our own execution failed before anything was posted
                             (transfer, page, timeout): try once more, then report.
    None of them: a plain refusal (budget, arguments, ownership).
    """

    def __init__(self, message: str, *, degrade: bool = False, login_expired: bool = False,
                 retryable: bool = False, next_allowed_at: datetime | None = None, job_id: str | None = None):
        super().__init__(message)
        self.degrade = degrade
        self.login_expired = login_expired
        self.retryable = retryable
        self.next_allowed_at = next_allowed_at
        self.job_id = job_id


#: Script steps after which a retry could post the same video twice.
_STEPS_AFTER_PUBLISH_CLICK = ("publish", "readback")

RETRY_GUIDANCE = "这是我们这边的执行问题，不是账号问题：可以直接再试一次；仍失败就如实告诉用户这次没发出去、稍后再试。不要改用扫码投稿，不要出授权二维码。"
UNCLEAR_GUIDANCE = "发布按钮已经点过，结果不明：先用 action=status 或让用户看创作者中心的内容管理，确认没发出去再重试，不要直接重发。"


@dataclass
class PublishSpec:
    asset_id: str
    title: str
    intro: str = ""
    topics: list[str] = field(default_factory=list)
    declaration: str = "ai"
    hot_word: str | None = None
    visibility: str = "public"
    schedule_at: str | None = None
    mode: str | None = None
    dry_run: bool = False
    simulate_risk: bool = False


@dataclass
class Caller:
    workspace_id: str
    user_id: str
    session_id: str | None = None
    tool_call_id: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def creator_account(workspace_id: str) -> PlatformAccount | None:
    from platforms.desktop import service as desktop_service

    async with get_db_session() as db:
        row = (await db.execute(select(PlatformAccount).where(
            PlatformAccount.workspace_id == workspace_id, PlatformAccount.platform == SITE_KEY,
            PlatformAccount.auth_kind == desktop_service.AUTH_KIND, PlatformAccount.deleted_at.is_(None),
        ).order_by(PlatformAccount.updated_at.desc()))).scalars().first()
        if row is not None:
            db.expunge(row)
        return row


@dataclass
class Precheck:
    mode: str
    mode_reason: str
    account: PlatformAccount | None
    login_ok: bool
    budget: policy.Budget | None

    @property
    def can_auto(self) -> bool:
        return self.mode == "auto" and self.login_ok and self.budget is not None and self.budget.allowed


async def precheck(caller: Caller, *, requested_mode: str | None = None) -> Precheck:
    from core.config import get_config

    cfg = get_config().desktop_publish
    account = await creator_account(caller.workspace_id)
    mode, reason = policy.resolve_mode(requested_mode, account, cfg.default_mode)
    login_ok = account is not None and account.status == "bound"
    budget = None
    if mode == "auto":
        budget = await policy.check_budget(
            workspace_id=caller.workspace_id, account_id=account.id if account else None, now=_now(),
            daily_limit=cfg.daily_limit, min_interval_minutes=cfg.min_interval_minutes,
            window_start_hour=cfg.window_start_hour, window_end_hour=cfg.window_end_hour,
        )
    return Precheck(mode=mode, mode_reason=reason, account=account, login_ok=login_ok, budget=budget)


async def _notify(workspace_id: str, kind: str, title: str, body: str) -> None:
    from platforms import service as platform_service

    async with get_db_session() as db:
        await platform_service.add_notification(db, workspace_id=workspace_id, user_id=None, kind=kind, title=title, body=body)


async def _auth_blocked(caller: Caller, account: PlatformAccount | None, *, confirmed=False):
    if account is None:
        return
    from notifications.events import auth_blocked
    async with get_db_session() as db:
        row = await db.get(PlatformAccount, account.id)
        if not row or row.deleted_at:
            return
        if confirmed:
            row.status, row.last_error, row.updated_at = "expired", "desktop_publish_login_expired", _now()
        if row.status == "expired":
            await auth_blocked(db, row, session_id=caller.session_id, user_id=caller.user_id)


async def _create_job(caller: Caller, spec: PublishSpec, account: PlatformAccount | None, details: dict) -> PublishJob:
    now = _now()
    async with get_db_session() as db:
        job = PublishJob(
            id=generate_id("pub"), workspace_id=caller.workspace_id, user_id=caller.user_id, platform=policy.DESKTOP_PLATFORM,
            platform_account_id=account.id if account else None, file_asset_id=spec.asset_id, title=spec.title,
            hashtags=list(spec.topics), status="pending", details=details, expires_at=now + timedelta(hours=2),
            created_at=now, updated_at=now,
        )
        db.add(job)
        await db.flush()
        from trajectory.jobs import record_job_in_tx
        await record_job_in_tx(db, job, submitted=True, session_id=caller.session_id)
        db.expunge(job)
        return job


async def _finish_job(job_id: str, *, status: str, error: str | None = None, item_id: str | None = None,
                      details_update: dict | None = None, published: bool = False) -> None:
    async with get_db_session() as db:
        job = await db.get(PublishJob, job_id)
        if job is None:
            return
        job.status = status
        job.error = error
        if item_id:
            job.item_id = item_id
        if details_update:
            job.details = {**(job.details or {}), **details_update}
        if published:
            job.published_at = _now()
        job.updated_at = _now()
        from trajectory.jobs import record_job_in_tx
        await record_job_in_tx(db, job)


async def run_script_on_desktop(caller: Caller, record: dict, params: dict, *, timeout_s: int) -> dict:
    """Run the publish script on the workspace desktop and return its JSON."""
    from platforms.desktop import service as desktop_service

    return await desktop_service.run_command_on_desktop(
        record, script.build_command(params, timeout_s=timeout_s), parse=script.parse_output,
        summary=f"desktop_publish {'dry-run ' if params.get('dry_run') else ''}{params.get('title', '')[:30]}",
        operation="publish", lease=True, timeout=timeout_s + 30, lease_ttl=float(timeout_s + 60),
        span_kind="platform.publish", session_id=caller.session_id or "desktop-publish", tool_call_id=caller.tool_call_id,
    )


async def prepare_desktop(caller: Caller, record: dict, asset) -> tuple[Any, str]:
    """Desktop Chrome with the person's profile up, and the video copied onto the desktop."""
    from core.oss import get_oss
    from platforms.desktop import service as desktop_service
    from sandbox.assets import deliver
    from sandbox.browser import ensure_browser, is_headless

    client = desktop_service._client_for(record)
    key = record["desktop_id"]
    state = await ensure_browser(client, key, "local")
    if is_headless(state.get("chrome")):
        raise PublishRefusal("云电脑没有带登录态的桌面 Chrome（只有无头浏览器）；请先打开云电脑桌面再试。")
    paths = await deliver(client, key, get_oss(), [asset])
    if not paths:
        raise PublishRefusal("成片没能复制到云电脑（obx-file get 失败）。")
    return client, paths[0]


async def publish(caller: Caller, spec: PublishSpec, *, ctx) -> dict:
    """Returns the published/dry-run record; raises PublishRefusal otherwise."""
    from core.config import get_config
    from platforms.desktop import service as desktop_service
    from tool.video_production import _find_owned_asset

    cfg = get_config().desktop_publish
    # The script types topics as #chips after the intro; strip any the caller already
    # wrote into the intro so the published caption does not repeat them.
    if spec.topics and spec.intro:
        import re as _re

        intro = spec.intro
        for t in spec.topics:
            intro = _re.sub(r"\s*#" + _re.escape(t) + r"(?=\s|#|$)", "", intro)
        spec.intro = _re.sub(r"\s{2,}", " ", intro).strip()
    if len(spec.title) > cfg.max_title_chars:
        raise PublishRefusal(f"标题 {len(spec.title)} 字，创作者中心上限 {cfg.max_title_chars} 字，请缩短。")
    if len(spec.intro or "") > 1000:
        raise PublishRefusal("简介超过 1000 字。")
    if spec.declaration not in script.DECLARATIONS or spec.visibility not in script.VISIBILITY:
        raise PublishRefusal("declaration / visibility 取值无效。")
    pre = await precheck(caller, requested_mode=spec.mode)
    if pre.mode == "package":
        raise PublishRefusal(f"本次走扫码投稿包：{pre.mode_reason}。用 douyin_publish 出投稿码。", degrade=True)
    if not pre.login_ok:
        await _auth_blocked(caller, pre.account)
        status = pre.account.status if pre.account else "none"
        raise PublishRefusal(
            f"云电脑上的抖音创作者中心登录态不可用（{status}）。让用户用 desktop_login(action=open, site=douyin_creator) 在云电脑重新登录，"
            "登录后再发这条；视频留着，不要改用扫码投稿，不要出授权二维码。", login_expired=True,
        )
    if pre.budget is not None and not pre.budget.allowed and not spec.dry_run:
        raise PublishRefusal(pre.budget.reason, next_allowed_at=pre.budget.next_allowed_at)
    record = await desktop_service.workspace_desktop(caller.workspace_id)
    if not record:
        raise PublishRefusal("当前工作空间没有云电脑，无法通过创作者中心发布；改用 douyin_publish 投稿码。", degrade=True)
    asset = await _find_owned_asset(spec.asset_id, ctx)
    if asset is None:
        raise PublishRefusal(f"{spec.asset_id!r} 不是你可用的成片 asset_id（要 status=ready 的本人资产）。")
    if not (asset.mime or "").startswith("video/"):
        raise PublishRefusal(f"资产 {asset.name} 是 {asset.mime}，创作者中心视频发布只接受视频。")
    details = {"mode": "auto", "visibility": spec.visibility, "declaration": spec.declaration, "hot_word": spec.hot_word,
               "schedule_at": spec.schedule_at, "dry_run": spec.dry_run, "asset_name": asset.name,
               "desktop_id": record["desktop_id"]}
    job = await _create_job(caller, spec, pre.account, details)
    try:
        client, path = await prepare_desktop(caller, record, asset)
        params = script.build_params(
            file_path=path, title=spec.title, intro=spec.intro or "", topics=spec.topics, declaration=spec.declaration,
            hot_word=spec.hot_word, visibility=spec.visibility, schedule_at=spec.schedule_at, dry_run=spec.dry_run,
            simulate_risk=spec.simulate_risk, risk_patterns=cfg.risk_patterns,
            upload_timeout_seconds=cfg.upload_timeout_seconds, evidence_path=f"tmp/obx-publish-{job.id}.png",
        )
        result = await run_script_on_desktop(caller, record, params, timeout_s=cfg.upload_timeout_seconds + 150)
    except PublishRefusal as exc:
        await _finish_job(job.id, status="failed", error=str(exc)[:400])
        exc.job_id = job.id
        raise
    except Exception as exc:  # desktop unavailable / busy / transport — nothing was posted
        await _finish_job(job.id, status="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        raise PublishRefusal(f"云电脑上传或页面操作没完成（{type(exc).__name__}: {str(exc)[:160]}）。{RETRY_GUIDANCE}",
                             retryable=True, job_id=job.id) from exc
    return await _settle(caller, job, spec, pre.account, result)


async def _settle(caller: Caller, job: PublishJob, spec: PublishSpec, account: PlatformAccount | None, result: dict) -> dict:
    upd = {"steps": result.get("steps"), "upload": result.get("upload"), "summary": result.get("summary"),
           "evidence_path": result.get("evidence"), "final_url": result.get("final_url"),
           "declaration_row": result.get("declaration_row"), "visibility_check": result.get("visibility"),
           "hot_word_attached": result.get("hot_word_attached"),
           "hot_row": result.get("hot_row"), "schedule_row": result.get("schedule_row"), "publish_ms": result.get("publish_ms")}
    if result.get("login_expired"):
        await _auth_blocked(caller, account, confirmed=True)
        await _finish_job(job.id, status="failed", error=str(result.get("error"))[:400], details_update=upd)
        await _notify(caller.workspace_id, NOTIFY_LOGIN, "抖音创作者中心需要重新登录",
                      "云电脑上的创作者中心登录态已失效，自动发布已暂停；请在云电脑重新登录抖音。")
        raise PublishRefusal("创作者中心登录态失效，已通知用户重登；这条先不发。", login_expired=True, job_id=job.id)
    if result.get("risk"):
        reason = f"风控信号「{result['risk']}」于步骤 {result.get('step')}"
        await _finish_job(job.id, status="failed", error=("risk: " + str(result.get("error")))[:400],
                          details_update={**upd, "risk": result.get("risk"), "risk_evidence": result.get("risk_evidence")})
        if account is not None and not spec.simulate_risk:
            await policy.disable_auto(account.id, reason, _now())
        elif account is not None and spec.simulate_risk:
            await policy.disable_auto(account.id, reason + "（演练）", _now())
        await _notify(caller.workspace_id, NOTIFY_DEGRADED, "自动发布已停用，改为扫码发布",
                      f"创作者中心出现{reason}。该账号的云电脑自动发布已停用，后续视频改产扫码投稿包；确认账号正常后可在工具里重新启用。")
        raise PublishRefusal(f"{reason}；该账号自动发布已停用并已通知用户。这条改用 douyin_publish 出投稿码。",
                             degrade=True, job_id=job.id)
    if not result.get("ok"):
        await _finish_job(job.id, status="failed", error=str(result.get("error") or "unknown")[:400], details_update=upd)
        step = str(result.get("step") or "")
        if step in _STEPS_AFTER_PUBLISH_CLICK and not spec.dry_run:
            raise PublishRefusal(f"发布结果不明：{result.get('error') or 'unknown'}（步骤 {step}）。{UNCLEAR_GUIDANCE}", job_id=job.id)
        raise PublishRefusal(f"云电脑上传或页面操作没完成：{result.get('error') or 'unknown'}（步骤 {step}）。{RETRY_GUIDANCE}",
                             retryable=True, job_id=job.id)
    if spec.dry_run:
        await _finish_job(job.id, status="draft", details_update=upd)
        return {"job_id": job.id, "status": "draft", "dry_run": True, "summary": result.get("summary"),
                "declaration_row": result.get("declaration_row"), "visibility_check": result.get("visibility"),
                "hot_word_attached": result.get("hot_word_attached"),
                "hot_row": result.get("hot_row"), "upload": result.get("upload"), "evidence_path": result.get("evidence")}
    await _finish_job(job.id, status="published", item_id=result.get("item_id"), details_update={**upd, "item_url": result.get("item_url")}, published=True)
    await _notify(caller.workspace_id, NOTIFY_DONE, "视频已通过创作者中心发布",
                  f"《{spec.title}》已发布（{script.VISIBILITY[spec.visibility]}）。" + (f" 作品 {result['item_id']}" if result.get("item_id") else ""))
    return {"job_id": job.id, "status": "published", "item_id": result.get("item_id"), "item_url": result.get("item_url"),
            "hot_word_attached": result.get("hot_word_attached"), "declaration_row": result.get("declaration_row"),
            "declaration": spec.declaration, "visibility": spec.visibility,
            "publish_ms": result.get("publish_ms"), "upload": result.get("upload"), "summary": result.get("summary"),
            "evidence_path": result.get("evidence"), "final_url": result.get("final_url")}


async def recent_jobs(workspace_id: str, limit: int = 10) -> list[PublishJob]:
    async with get_db_session() as db:
        rows = (await db.execute(select(PublishJob).where(
            PublishJob.workspace_id == workspace_id, PublishJob.platform == policy.DESKTOP_PLATFORM,
        ).order_by(PublishJob.created_at.desc()).limit(limit))).scalars().all()
        for r in rows:
            db.expunge(r)
        return list(rows)
