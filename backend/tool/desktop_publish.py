"""desktop_publish: post a finished video to 抖音创作者中心 through the cloud desktop.

The transitional "auto publish" route (plan §1.5 / §7 C2–C4). Every gate is
enforced here, not in prose: mode switch, login state, per-account budget,
asset ownership, risk breaker. On a risk signal the tool degrades — it never
retries the desktop route by itself — and tells the caller to use
`douyin_publish` (QR package) for this video.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from publish import desktop_policy as policy
from publish import desktop_service as svc
from publish.desktop_script import DECLARATIONS, VISIBILITY
from tool.tool import ToolContext, ToolResult, define_tool


class DesktopPublishArgs(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["precheck", "publish", "status", "enable_auto"] = "precheck"
    #: publish: the finished video (owned, ready asset id).
    asset_id: str | None = Field(default=None, max_length=96)
    #: ≤ 30 chars on the creator-center form.
    title: str | None = Field(default=None, max_length=60)
    intro: str = Field(default="", max_length=1000)
    #: Topic words without '#', ≤ 10.
    topics: list[str] = Field(default_factory=list, max_length=10)
    #: 自主声明 single choice; AI-generated content must declare `ai`.
    declaration: Literal["ai", "opinion", "repost", "marketing", "fiction", "none"] = "ai"
    #: 关联热点 word (best effort; reported back whether it attached).
    hot_word: str | None = Field(default=None, max_length=40)
    visibility: Literal["public", "friends", "private"] = "public"
    #: 定时发布 "YYYY-MM-DD HH:mm" Asia/Shanghai, 2h–14d ahead; omit for now.
    schedule_at: str | None = Field(default=None, max_length=16)
    #: Template/request override of the deployment default (auto | package).
    mode: Literal["auto", "package"] | None = None
    #: Fill everything, screenshot, then 暂存离开 — never clicks 发布.
    dry_run: bool = False
    #: Drill: inject a fake captcha before publishing to exercise the degrade path (implies no publish).
    simulate_risk: bool = False
    #: status: one job; enable_auto: not needed.
    job_id: str | None = Field(default=None, max_length=96)

    @model_validator(mode="after")
    def _by_action(self) -> "DesktopPublishArgs":
        if self.action == "publish":
            if not self.asset_id or not self.title:
                raise ValueError("publish requires asset_id and title")
            if self.schedule_at:
                import re
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", self.schedule_at):
                    raise ValueError("schedule_at must be 'YYYY-MM-DD HH:mm' (Asia/Shanghai)")
            if any(("#" in t or not t.strip()) for t in self.topics):
                raise ValueError("topics are words without '#'")
        return self


def _caller(ctx: ToolContext) -> svc.Caller:
    return svc.Caller(workspace_id=ctx.workspace_id, user_id=ctx.user_id, session_id=ctx.session_id or None,
                      tool_call_id=ctx.part_id or "")


def _precheck_lines(pre: svc.Precheck) -> list[str]:
    lines = [f"mode={pre.mode} ({pre.mode_reason})"]
    acct = pre.account
    lines.append(f"login={'ok' if pre.login_ok else (acct.status if acct else 'none')}" + (f" account={acct.nickname or acct.external_id}" if acct else ""))
    if acct is not None and acct.auto_publish_disabled_at is not None:
        lines.append(f"auto_publish=disabled since {acct.auto_publish_disabled_at.isoformat()} reason={acct.auto_publish_disabled_reason}")
    if pre.budget is not None:
        b = pre.budget
        lines.append(f"budget={'ok' if b.allowed else 'blocked'} today={b.today_count}/{b.daily_limit}" + ("" if b.allowed else f" reason={b.reason}") + (f" next_allowed_at={b.next_allowed_at.astimezone(policy.SHANGHAI).isoformat()}" if b.next_allowed_at else ""))
    lines.append(f"can_auto_publish={'true' if pre.can_auto else 'false'}")
    if pre.mode == "package":
        lines.append("→ use douyin_publish (QR package) for this video.")
    elif not pre.login_ok:
        lines.append("→ ask the person to re-login on the cloud desktop: desktop_login(action=open, site=douyin_creator); or use douyin_publish.")
    return lines


async def _precheck(args: DesktopPublishArgs, ctx: ToolContext) -> ToolResult:
    pre = await svc.precheck(_caller(ctx), requested_mode=args.mode)
    return ToolResult(title="Desktop publish precheck", output="\n".join(_precheck_lines(pre)),
                      metadata={"mode": pre.mode, "login_ok": pre.login_ok, "can_auto_publish": pre.can_auto,
                                "budget": None if pre.budget is None else {"allowed": pre.budget.allowed, "today": pre.budget.today_count,
                                                                            "limit": pre.budget.daily_limit, "reason": pre.budget.reason,
                                                                            "next_allowed_at": pre.budget.next_allowed_at.isoformat() if pre.budget.next_allowed_at else None},
                                "account_id": pre.account.id if pre.account else None})


async def _publish(args: DesktopPublishArgs, ctx: ToolContext) -> ToolResult:
    spec = svc.PublishSpec(asset_id=args.asset_id or "", title=(args.title or "").strip(), intro=args.intro.strip(),
                           topics=[t.strip() for t in args.topics], declaration=args.declaration, hot_word=args.hot_word,
                           visibility=args.visibility, schedule_at=args.schedule_at, mode=args.mode,
                           dry_run=args.dry_run or args.simulate_risk, simulate_risk=args.simulate_risk)
    try:
        res = await svc.publish(_caller(ctx), spec, ctx=ctx)
    except svc.PublishRefusal as exc:
        lines = [str(exc)]
        if exc.degrade:
            lines.append("degrade=true → call douyin_publish(action=publish, asset_id=…, title=…, hashtags=…) for the QR package and tell the person why.")
        if exc.login_expired:
            lines.append("login_expired=true")
        if exc.next_allowed_at:
            lines.append(f"next_allowed_at={exc.next_allowed_at.astimezone(policy.SHANGHAI).isoformat()}")
        if exc.job_id:
            lines.append(f"job_id={exc.job_id}")
        return ToolResult(title="Desktop publish refused" if not exc.degrade else "Desktop publish degraded",
                          output="\n".join(lines),
                          metadata={"refused": True, "degrade": exc.degrade, "login_expired": exc.login_expired,
                                    "next_allowed_at": exc.next_allowed_at.isoformat() if exc.next_allowed_at else None, "job_id": exc.job_id})
    lines = [f"job_id={res['job_id']}", f"status={res['status']}"]
    if res.get("item_id"):
        lines.append(f"item_id={res['item_id']}")
    if res.get("item_url"):
        lines.append(f"item_url={res['item_url']}")
    s = res.get("summary") or {}
    lines.append(f"title_on_form={s.get('title')!r} counters={s.get('counters')} declaration={res.get('declaration_row')} visibility={VISIBILITY[args.visibility]}")
    if res.get("upload"):
        lines.append(f"upload_ms={res['upload'].get('ms')}")
    if res.get("publish_ms") is not None:
        lines.append(f"publish_ms={res['publish_ms']}")
    if res.get("evidence_path"):
        lines.append(f"evidence={res['evidence_path']} (on the desktop)")
    if res.get("dry_run"):
        lines.append("dry_run=true: form filled and saved as draft (暂存离开); nothing published.")
    return ToolResult(title=f"Desktop publish · {res['status']}", output="\n".join(lines), metadata=res)


async def _status(args: DesktopPublishArgs, ctx: ToolContext) -> ToolResult:
    rows = await svc.recent_jobs(ctx.workspace_id, limit=10)
    if args.job_id:
        rows = [r for r in rows if r.id == args.job_id]
    items = [{"job_id": r.id, "status": r.status, "title": r.title, "item_id": r.item_id, "error": r.error,
              "created_at": r.created_at.isoformat(), "published_at": r.published_at.isoformat() if r.published_at else None,
              "visibility": (r.details or {}).get("visibility"), "dry_run": (r.details or {}).get("dry_run")} for r in rows]
    return ToolResult(title="Desktop publish jobs", output=json.dumps(items, ensure_ascii=False, indent=1) if items else "no desktop publish jobs",
                      metadata={"jobs": items})


async def _enable_auto(args: DesktopPublishArgs, ctx: ToolContext) -> ToolResult:
    acct = await svc.creator_account(ctx.workspace_id)
    if acct is None:
        return ToolResult(title="No creator account", output="没有绑定的抖音创作者中心桌面账号。")
    ok = await policy.enable_auto(acct.id)
    return ToolResult(title="Auto publish re-enabled" if ok else "Not found",
                      output=f"账号 {acct.nickname or acct.external_id} 的云电脑自动发布已重新启用。" if ok else "账号不存在。",
                      metadata={"account_id": acct.id, "enabled": ok})


async def execute(args: DesktopPublishArgs, ctx: ToolContext) -> ToolResult:
    if not ctx.workspace_id:
        return ToolResult(title="Desktop publish unavailable", output="No workspace in this context.")
    if args.action == "precheck":
        return await _precheck(args, ctx)
    if args.action == "publish":
        return await _publish(args, ctx)
    if args.action == "status":
        return await _status(args, ctx)
    return await _enable_auto(args, ctx)


DESKTOP_PUBLISH_DESCRIPTION = """Post a finished video to 抖音创作者中心 using the login state on the workspace's cloud desktop \
(the transitional auto-publish route). Actions:
- precheck: which route applies now (auto vs QR package), login state, today's budget, next allowed time. Call first.
- publish: asset_id + title (≤30 chars) + intro (≤1000, topics become #chips) + declaration (AI content must be `ai`) \
+ visibility + optional hot_word / schedule_at ("YYYY-MM-DD HH:mm", 2h–14d ahead). `dry_run=true` fills the form, \
screenshots and saves a draft without publishing. Returns job_id, status, item_id when the work is visible.
- status: recent desktop publish jobs (publish_jobs, platform douyin_creator).
- enable_auto: re-enable this account's auto publish after a person confirmed the account is fine.
Rules the tool enforces: per-account daily limit and minimum interval, posting hours, one login state per workspace. \
Any captcha/risk text stops the desktop route for that account (degrade=true): then use douyin_publish for the QR package. \
Never loop on a refusal; report it."""

desktop_publish_tool = define_tool(
    "desktop_publish",
    description=DESKTOP_PUBLISH_DESCRIPTION,
    parameters=DesktopPublishArgs,
    execute=execute,
    sandbox_required=False,
    parallel_safe=False,
)
