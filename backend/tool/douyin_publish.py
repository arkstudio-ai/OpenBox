"""The douyin_publish tool: what the agent uses to reach the 授权中心.

Four actions, all scoped to the session's workspace (identity comes only
from ToolContext, never from arguments):

* ``status``    — which Douyin accounts are bound and when they lapse.
* ``authorize`` — a fresh authorization link + QR code for the person to
                  scan when nothing is bound or the grant expired.
* ``publish``   — sign a posting QR code for one resource-centre video with
                  the caption and hashtags the agent prepared.
* ``result``    — whether a posting job has been published yet.

Douyin never lets a website application post on the person's behalf: the
person scans and presses 发布 inside the app. The QR codes are rendered
server-side (segno) and pinned to the reply as image attachments.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field

from core.log import create_logger
from core.oss import OssNotConfigured, get_oss
from platforms import service
from platforms.errors import PlatformError
from tool.tool import ToolContext, ToolResult, define_tool

log = create_logger("tool.douyin_publish")

PLATFORM = "douyin"


class DouyinPublishArgs(BaseModel):
    action: Literal["status", "authorize", "publish", "result"] = Field(
        description=(
            "status: list bound Douyin accounts and expiry; authorize: get a link + QR code "
            "for the person to (re)authorize; publish: sign a posting QR code for one video; "
            "result: check whether a posting job was published."
        )
    )
    asset_id: str | None = Field(
        default=None,
        description=(
            "publish: the resource-centre asset id of the video (from share_file's "
            "asset_id, or a file the person uploaded). mp4/mov, at most 128 MB."
        ),
    )
    title: str = Field(default="", description="publish: caption shown on Douyin, at most 55 characters.")
    hashtags: list[str] = Field(default_factory=list, description="publish: topic names without '#', at most 10.")
    private_status: int = Field(default=0, description="publish: 0 everyone, 1 only me, 2 friends.")
    job_id: str | None = Field(default=None, description="result: the posting job id returned by publish.")


def _fmt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return iso


def _error(code: str, message: str, **extra) -> ToolResult:
    return ToolResult(
        title=f"douyin_publish: {code}",
        output=message,
        metadata={"error": True, "code": code, "platform": PLATFORM, **extra},
    )


def _platform_error(exc: PlatformError) -> ToolResult:
    hints = {
        "PLATFORM_NOT_CONFIGURED": "抖音开放平台没有在本环境配置，无法绑定或投稿。",
        "PLATFORM_AUTH_REQUIRED": "抖音授权已失效，需要重新扫码授权（action=authorize）。",
        "PUBLISH_FILE_NOT_FOUND": "在当前工作空间里找不到这个视频，先用 share_file 把成片登记为资源再试。",
        "PUBLISH_FILE_TYPE": "抖音只接受 mp4/mov 视频。",
        "PUBLISH_FILE_TOO_LARGE": "视频超过抖音 128 MB 上限，需要先压缩。",
        "PUBLISH_JOB_NOT_FOUND": "没有这个投稿记录。",
    }
    return _error(exc.code, hints.get(exc.code, str(exc)), detail=str(exc))


def qr_png(text: str) -> bytes:
    """A scannable PNG for a schema/URL. Kept tiny: the chat shows it inline."""
    import segno

    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=6, border=2)
    return buf.getvalue()


async def _pin_png(ctx: ToolContext, png: bytes, *, name: str, label: str, caption: str, kind: str) -> str | None:
    """Upload a PNG to OSS, record it, and attach it to the assistant reply."""
    from core.identifier import ascending
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from models.message import FilePart, FileRelation
    from session.session import save_part
    from tool.image_gen import _upload_bytes

    try:
        oss = get_oss()
    except OssNotConfigured:
        return None
    asset_id = ascending("asset")
    key = f"assets/{ctx.user_id}/{asset_id}/{name}"
    size = await _upload_bytes(oss, key, "image/png", png)
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            FileAsset(
                id=asset_id,
                user_id=ctx.user_id,
                workspace_id=ctx.workspace_id,
                session_id=ctx.session_id or None,
                project_id=ctx.project_id or None,
                name=name,
                oss_key=key,
                mime="image/png",
                size=size,
                status="ready",
                source="agent",
                # A QR code is a working artefact of the conversation, not a
                # resource worth keeping in the resource centre.
                transient=True,
                created_at=now,
            )
        )
        await db.commit()
    await save_part(
        FilePart(
            path=f"/workspace/douyin/{name}",
            mime_type="image/png",
            asset_id=asset_id,
            oss_key=key,
            size=size,
            transient=True,
            relation=FileRelation(
                source_part_id=ctx.part_id or None,
                group_id=f"tool:{ctx.part_id}" if ctx.part_id else asset_id,
                role="result",
                kind=kind,
                label=label,
                caption=caption[:4000],
                ordinal=0,
            ),
            session_id=ctx.session_id,
            message_id=ctx.message_id,
        ),
        is_new=True,
        user_id=ctx.user_id,
    )
    return asset_id


async def _role(ctx: ToolContext) -> str | None:
    from db.repository.workspace_repo import PgWorkspaceRepo

    member = await PgWorkspaceRepo().get_member(ctx.workspace_id, ctx.user_id)
    return (member or {}).get("role")


def _describe(account: dict) -> str:
    status = {"bound": "已授权", "expired": "已失效", "revoked": "已解绑"}.get(account["status"], account["status"])
    line = f"- {account.get('nickname') or '未命名'}（{status}，openid {account['externalId'][:8]}…）"
    if account["status"] == "bound":
        line += (
            f"，预计到期 {_fmt(account.get('estimatedExpiresAt'))}，"
            f"当前授权有效至 {_fmt(account.get('refreshExpiresAt'))}，剩余自动续期 {account.get('renewalsLeft', 0)} 次"
        )
    elif account.get("lastError"):
        line += f"，原因：{account['lastError']}"
    return line


async def _status(ctx: ToolContext) -> ToolResult:
    from platforms.registry import get_provider

    if not get_provider(PLATFORM).info().configured:
        return _error("PLATFORM_NOT_CONFIGURED", "抖音开放平台没有在本环境配置，无法绑定或投稿。")
    rows = [service.to_public(r) for r in await service.list_accounts(ctx.workspace_id)]
    mine = [r for r in rows if r["platform"] == PLATFORM]
    bound = [r for r in mine if r["status"] == "bound"]
    lines = [_describe(r) for r in mine] or ["（当前工作空间还没有绑定抖音账号）"]
    if bound:
        verdict = "可以投稿：先把标题和话题给用户确认，再用 action=publish 出投稿二维码。"
    elif mine:
        verdict = "授权已失效，需要用户重新扫码：用 action=authorize 出授权链接和二维码。"
    else:
        verdict = "还没有绑定的抖音账号：用 action=authorize 出授权链接和二维码，用户扫码后再投稿。"
    return ToolResult(
        title="抖音账号状态",
        output="\n".join(lines) + f"\n\n{verdict}",
        metadata={
            "platform": PLATFORM,
            "bound": bool(bound),
            "accounts": [
                {
                    "id": r["id"],
                    "nickname": r["nickname"],
                    "status": r["status"],
                    "estimatedExpiresAt": r.get("estimatedExpiresAt"),
                    "renewalsLeft": r.get("renewalsLeft"),
                }
                for r in mine
            ],
        },
    )


async def _authorize(ctx: ToolContext) -> ToolResult:
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=600)
    role = await _role(ctx)
    if role is not None and role not in ("owner", "admin"):
        return _error(
            "PLATFORM_ROLE_REQUIRED",
            "只有工作空间的 owner 或 admin 可以绑定抖音账号；请告诉用户找管理员在授权中心绑定。",
        )
    try:
        started = await service.start_authorize(
            user_id=ctx.user_id, workspace_id=ctx.workspace_id, platform=PLATFORM, call_app=True
        )
    except PlatformError as exc:
        return _platform_error(exc)
    url = started["authorizeUrl"]
    asset_id = None
    try:
        asset_id = await _pin_png(
            ctx,
            qr_png(url),
            name="douyin-authorize.png",
            label="抖音授权二维码",
            caption="用手机扫码，或在电脑上打开链接后用抖音 App 扫描页面上的二维码。",
            kind="qr_code",
        )
    except Exception:
        log.warning("could not attach the Douyin authorize QR code", exc_info=True)
    output = (
        "已生成抖音授权链接，10 分钟内有效。请用户二选一：\n"
        f"1. 在电脑上打开 {url} ，用抖音 App 扫页面上的二维码并确认；\n"
        "2. 用手机扫下面的二维码，按提示在抖音里确认。\n"
        "授权完成后浏览器会回到授权中心页面；然后再用 action=status 确认已绑定。"
    )
    return ToolResult(
        title="抖音授权二维码",
        output=output,
        metadata={
            "platform": PLATFORM, "authorizeUrl": url, "asset_id": asset_id,
            "expiresInSeconds": 600,
            "expiresAt": expires_at.isoformat(),
        },
    )


async def _publish(args: DouyinPublishArgs, ctx: ToolContext) -> ToolResult:
    if not args.asset_id:
        return _error("PUBLISH_ASSET_REQUIRED", "publish 需要 asset_id：先用 share_file 把成片登记为资源（可 attach=false），拿到 asset_id 再调用。")
    if args.private_status not in (0, 1, 2):
        return _error("PUBLISH_BAD_ARGS", "private_status 只能是 0（所有人）、1（仅自己）、2（好友）。")
    rows = await service.list_accounts(ctx.workspace_id)
    if not any(r.platform == PLATFORM and r.status == "bound" for r in rows):
        from db.base import get_db_session
        from db.models.platform_account import PlatformAccount
        from notifications.events import auth_blocked
        async with get_db_session() as db:
            for account in rows:
                if account.platform == PLATFORM and account.status == "expired":
                    row = await db.get(PlatformAccount, account.id)
                    await auth_blocked(db, row, session_id=ctx.session_id, user_id=ctx.user_id)
        return _error(
            "PLATFORM_AUTH_REQUIRED",
            "当前工作空间没有有效的抖音授权，先用 action=authorize 让用户扫码授权。",
        )
    try:
        oss = get_oss()
    except OssNotConfigured as exc:
        return _error("OSS_NOT_CONFIGURED", f"对象存储未配置，无法生成投稿链接：{exc}")
    try:
        job, schema = await service.create_publish_job(
            oss=oss,
            user_id=ctx.user_id,
            workspace_id=ctx.workspace_id,
            file_asset_id=args.asset_id,
            title=args.title.strip(),
            hashtags=[h for h in args.hashtags if h and h.strip()],
            private_status=args.private_status,
        )
    except PlatformError as exc:
        return _platform_error(exc)
    public = service.job_to_public(job)
    asset_id = None
    try:
        asset_id = await _pin_png(
            ctx,
            qr_png(schema),
            name=f"douyin-publish-{job.id}.png",
            label="抖音投稿二维码",
            caption=f"标题：{public['title'] or '（无）'}；话题：{'、'.join(public['hashtags']) or '（无）'}",
            kind="qr_code",
        )
    except Exception:
        log.warning("could not attach the Douyin publish QR code", exc_info=True)
    output = (
        "投稿二维码已生成，一小时内有效。请用户打开抖音 App 扫描二维码：抖音会下载视频并进入发布页，"
        "标题和话题已预填，用户在抖音里点「发布」即完成。\n"
        f"标题：{public['title'] or '（无）'}\n"
        f"话题：{'、'.join(public['hashtags']) or '（无）'}\n"
        f"job_id={job.id}（用户说扫完了之后，用 action=result 查询是否发布成功；只有 Webhook 回调了才会变成 published）"
    )
    return ToolResult(
        title="抖音投稿二维码",
        output=output,
        metadata={
            "platform": PLATFORM,
            "job_id": job.id,
            "share_id": job.share_id,
            "asset_id": asset_id,
            "expiresAt": public["expiresAt"],
            # Same short-lived capability encoded in the attached QR image.
            # Native clients can explicitly open it on the same phone; it is
            # not an OAuth token and conveys no extra authority over that QR.
            "launchUrl": schema,
            "schemaSource": "local" if (job.error or "").startswith("schema_source=local") else "get_share",
        },
    )


async def _result(args: DouyinPublishArgs, ctx: ToolContext) -> ToolResult:
    if not args.job_id:
        return _error("PUBLISH_JOB_REQUIRED", "result 需要 job_id。")
    try:
        job = await service.get_job(args.job_id, ctx.workspace_id)
    except PlatformError as exc:
        return _platform_error(exc)
    public = service.job_to_public(job)
    text = {
        "pending": "还没有收到抖音的发布回调。如果用户已经在抖音里点了发布，稍等一分钟再查；二维码在 "
        f"{_fmt(public['expiresAt'])} 前有效。",
        "published": f"已在抖音发布（作品 id {public['itemId'] or '未知'}）。",
        "expired": "二维码已过期，用户没有在一小时内完成发布；需要重新 action=publish 生成。",
        "failed": f"投稿失败：{public['error'] or '未知原因'}",
    }.get(public["status"], public["status"])
    return ToolResult(title=f"投稿状态：{public['status']}", output=text, metadata={"platform": PLATFORM, **public})


async def execute_douyin_publish(args: DouyinPublishArgs, ctx: ToolContext) -> ToolResult:
    if not ctx.workspace_id:
        return _error("WORKSPACE_REQUIRED", "这个会话没有工作空间上下文，无法访问授权中心。")
    if args.action == "status":
        return await _status(ctx)
    if args.action == "authorize":
        return await _authorize(ctx)
    if args.action == "publish":
        return await _publish(args, ctx)
    return await _result(args, ctx)


DOUYIN_PUBLISH_DESCRIPTION = """\
FALLBACK route to Douyin: the 开放平台 QR posting package. The default route is \
desktop_publish (the cloud desktop's logged-in 创作者中心, no authorization needed); \
use this tool only when desktop_publish reports mode=package or degrade=true, or when \
the person explicitly asks to publish by scanning / manage the 开放平台 authorization. \
Never reach for it because a desktop upload failed or the desktop login expired. \
`publish` returns a QR code the person scans with the Douyin app, where the caption \
and hashtags are pre-filled and they press 发布. Always call action=status first; if \
nothing is bound or the grant expired, action=authorize gives a link + QR code (the \
authorization page shows the 开放平台 app name — explain why this step exists). Prepare \
the title and hashtags yourself and confirm them with the person before publishing. \
publish needs the video's asset_id (share_file with attach=false returns one)."""

douyin_publish_tool = define_tool(
    "douyin_publish",
    description=DOUYIN_PUBLISH_DESCRIPTION,
    parameters=DouyinPublishArgs,
    execute=execute_douyin_publish,
    sandbox_required=False,
    parallel_safe=False,
)
