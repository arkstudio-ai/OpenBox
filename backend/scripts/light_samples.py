#!/usr/bin/env python
"""Make the Light gallery samples through the real pipeline (docs/LIGHT_MODE_PLAN.md §4.3).

Each sample in ``light/entries.zh-CN.json`` gets one real output, made the way a
person's would be: video samples through ``video_generate`` on the entry's
default tier (15 s, 9:16), menu and poster samples through ``image_gen``.
The output is copied to a stable key under ``--prefix`` so the entries file
can point at it (``media_url: "oss:<key>"``; the API signs it when serving).

It bills the account it runs as, like any generation. Run inside the backend
container of a test deployment, as a dedicated samples account:

    docker exec -i openbox-backend-1 python - --user light-full-samples --only dish_closeup,shop_evening < scripts/light_samples.py

Prints one JSON line per sample: id, kind, key, seconds, credits and the job
or asset it came from. ``--dry-run`` prints the prompts without calling anything.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

for root in (Path.cwd(), Path("/app")):
    if (root / "light").is_dir():
        sys.path.insert(0, str(root))
        break

NEGATIVE = "画面中不要出现任何文字、字幕、水印或 logo，不要出现清晰人脸。"

VIDEO_PROMPTS = {
    # 餐品展示
    "dish_closeup": "竖屏美食短片：一碗刚出锅的红烧牛肉面特写，热气升腾，红亮的汤汁和大块牛肉在暖光下泛着油光，"
                    "筷子缓缓挑起面条拉出热气，镜头从俯拍慢慢推近再环绕半圈，木质餐桌，浅景深，暖色灯光，烟火气，电影感。",
    "dish_dark": "竖屏高级感美食短片：深色背景上一份精致摆盘的招牌红烧肉，侧逆光勾出酱汁光泽与蒸汽，"
                 "镜头极慢地平移和推近，暗调低饱和，留白多，质感像高端餐厅宣传片。",
    "dish_fast": "竖屏快节奏美食短片：一张大圆桌上七八道家常菜轮流特写入画——小炒肉、酸菜鱼、干锅花菜、红烧排骨、凉拌黄瓜、蛋炒饭，"
                 "每道菜一到两秒，短切配合快速推拉，热气腾腾，色彩鲜亮，热闹丰盛。",
    # 店内环境
    "shop_evening": "竖屏餐馆氛围短片：傍晚街边一家亮着暖黄灯光的小餐馆门口，镜头从门外缓缓推进店内，"
                    "店里坐满用餐的人（只用中远景，背影或侧影），桌上菜品冒着热气，灯光温暖，烟火气，镜头平稳。",
    "shop_clean": "竖屏餐厅环境短片：白天自然光洒进一家明亮整洁的小餐厅，浅色木桌椅整齐摆放，餐具干净发亮，"
                  "绿植点缀，镜头沿着座位区平稳滑过，再停在靠窗的一张摆好餐具的桌子上，清新干净。",
    "shop_busy": "竖屏餐厅人气短片：晚饭高峰时段一家热闹的家常菜馆，满座的食客以中远景和背影呈现，服务员端着冒热气的菜穿行，"
                 "后厨窗口火光一闪，镜头手持感轻微晃动，暖色灯光，热闹有人气。",
    # 探店
    "visit_walk": "竖屏第一视角探店短片：从街边小店的门头走进店内，看一眼墙上的菜单板，入座后一碗热腾腾的招牌面端上桌，"
                  "第一视角夹起一口，最后镜头拉远看到整张餐桌，节奏自然，像真实探店 vlog，暖色调。",
    "visit_recommend": "竖屏探店推荐短片：三道招牌菜依次特写，第一道金黄酥脆的炸鸡，第二道红亮的麻辣小龙虾，第三道奶白的骨汤锅，"
                       "每道菜用推近镜头加筷子夹起的动作展示，节奏分明，暖色灯光，食欲感强。",
    "visit_price": "竖屏实惠探店短片：一张桌上摆满分量十足的家常菜和大碗米饭，镜头从俯拍展示整桌的丰盛，再特写满满一勺的肉菜，"
                   "突出分量足、实惠，暖色灯光，接地气的小馆子氛围。",
}

MENU_ITEMS = (
    "【招牌面】红烧牛肉面 ¥28 · 招牌\n番茄鸡蛋面 ¥18\n老坛酸菜面 ¥22\n"
    "【小炒】青椒肉丝 ¥26\n干煸豆角 ¥22\n"
    "【小吃】手工煎饺 ¥15\n凉拌黄瓜 ¥8\n"
    "【饮品】酸梅汤 ¥6\n冰豆浆 ¥5"
)

IMAGE_PROMPTS = {
    "menu_mono": ("1024x1536", "Use case: productivity-visual\nAsset type: restaurant menu, phone portrait\n"
                  "Style: 简约黑白 — white background, black sans-serif type, thin black rules, lots of white space\n"
                  "Layout: heading \"巷口面馆\"; categories as section headings; dish name left, price right with dot leaders; "
                  "the signature dish has a small 「招牌」 badge; a blank square labeled 「扫码点餐」 bottom right\n"
                  "Text (verbatim, Simplified Chinese, render every character exactly):\n" + MENU_ITEMS + "\n"
                  "Constraints: no other text, no photos, no people"),
    "menu_red": ("1024x1536", "Use case: productivity-visual\nAsset type: restaurant menu, phone portrait\n"
                 "Style: 烟火红 — deep red background, warm gold and cream text, bold headings, subtle festive pattern border\n"
                 "Layout: heading \"巷口面馆\"; categories as section headings; dish name left, price right with dot leaders; "
                 "the signature dish has a small gold 「招牌」 badge; a blank square labeled 「扫码点餐」 bottom right\n"
                 "Text (verbatim, Simplified Chinese, render every character exactly):\n" + MENU_ITEMS + "\n"
                 "Constraints: no other text, no photos, no people"),
    "menu_chalk": ("1024x1536", "Use case: productivity-visual\nAsset type: restaurant menu, phone portrait\n"
                   "Style: 手写粉笔 — dark green-black chalkboard texture, white and pastel hand-lettered look, small doodles of noodles and steam, still very legible\n"
                   "Layout: heading \"巷口面馆\"; categories as section headings; dish name left, price right; "
                   "the signature dish circled in chalk with 「招牌」; a blank square labeled 「扫码点餐」 bottom right\n"
                   "Text (verbatim, Simplified Chinese, render every character exactly):\n" + MENU_ITEMS + "\n"
                   "Constraints: no other text, no people"),
    "menu_dark": ("1024x1536", "Use case: productivity-visual\nAsset type: restaurant menu, phone portrait\n"
                  "Style: 深色高级 — charcoal background, thin gold rules, elegant serif headings, refined spacing\n"
                  "Layout: heading \"巷口面馆\"; categories as section headings; dish name left, price right with fine dot leaders; "
                  "the signature dish has a small gold 「招牌」 mark; a blank square labeled 「扫码点餐」 bottom right\n"
                  "Text (verbatim, Simplified Chinese, render every character exactly):\n" + MENU_ITEMS + "\n"
                  "Constraints: no other text, no photos, no people"),
    "poster_bold": ("1024x1536", "Use case: ads-marketing\nAsset type: restaurant promotion poster, portrait for WeChat Moments\n"
                    "Style: 大字促销 — huge bold headline, the price inside a red starburst, high contrast yellow and red, a steaming bowl of beef noodles at the bottom\n"
                    "Text (verbatim, Simplified Chinese, render exactly):\n  Headline: \"招牌牛肉面 19.9\"\n"
                    "  Offer: \"国庆七天 到店堂食特价\"\n  Small print: \"10月1日—10月7日\"\n  Shop: \"巷口面馆\"\n"
                    "Constraints: no other text, no people's faces, no logos"),
    "poster_photo": ("1024x1536", "Use case: ads-marketing\nAsset type: restaurant new-dish poster, portrait for WeChat Moments\n"
                     "Style: 实拍主图 — a realistic close-up photo of a glossy braised pork dish as the hero filling the upper two thirds, "
                     "a clean cream type band below\n"
                     "Text (verbatim, Simplified Chinese, render exactly):\n  Headline: \"新品上市\"\n"
                     "  Offer: \"秘制红烧肉 每天限量30份\"\n  Shop: \"巷口面馆\"\n"
                     "Constraints: no other text, no people's faces, no logos"),
    "poster_fresh": ("1024x1536", "Use case: ads-marketing\nAsset type: dessert shop promotion poster, portrait for WeChat Moments\n"
                     "Style: 手绘清新 — soft pastel watercolor illustration of a mango pomelo sago dessert and iced drinks, light and airy\n"
                     "Text (verbatim, Simplified Chinese, render exactly):\n  Headline: \"夏日第二杯半价\"\n"
                     "  Offer: \"杨枝甘露 / 柠檬茶 全场通用\"\n  Shop: \"巷口甜品\"\n"
                     "Constraints: no other text, no people's faces, no logos"),
}


async def _account(username: str) -> tuple[str, str]:
    from db.repository.user_repo import PgUserRepo
    user = await PgUserRepo().get_by_username(username)
    if user is None:
        raise SystemExit(f"no such user: {username} (register it first)")
    return user["id"], str(user["default_workspace_id"])


async def _grant(workspace_id: str, user_id: str, amount: str, key: str) -> None:
    from decimal import Decimal
    from sqlalchemy import select
    from billing.service import lock_balance, post_ledger
    from db.base import get_db_session
    from db.models.billing import CreditLedger
    async with get_db_session() as db:
        account = await lock_balance(db, workspace_id)
        if not await db.scalar(select(CreditLedger.id).where(CreditLedger.idempotency_key == key)):
            post_ledger(db, account, amount=Decimal(amount), kind="admin_grant", reference_id=user_id[:64], key=key)
        await db.flush()


async def _session(user_id: str, workspace_id: str, title: str, tier: str | None):
    from agent.agent import default_agent_name
    from agent.model_resolve import resolve as resolve_model
    from api.v1.quality import resolve_quality
    from core.config import get_config
    from session import session as session_mod
    config = get_config()
    model, _ = resolve_model("", config, context="light samples")
    session = await session_mod.create_session(model=model, agent=default_agent_name(), title=title,
                                               user_id=user_id, workspace_id=workspace_id)
    if tier:
        video_model, video_resolution = resolve_quality(tier, config)
        await session_mod.update_session(session.id, user_id=user_id,
                                         video_model=video_model, video_resolution=video_resolution)
    return session


async def _oss_key(asset_id: str) -> tuple[str, str]:
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    async with get_db_session() as db:
        row = await db.get(FileAsset, asset_id)
        return row.oss_key, str(row.mime or "")


async def _video(sample_id: str, user_id: str, workspace_id: str, prefix: str) -> dict:
    from tool import video_production as vp
    from tool.tool import ToolContext
    from tool.video_production import VideoGenerateArgs
    session = await _session(user_id, workspace_id, f"样片 {sample_id}", "low")
    ctx = ToolContext(session_id=session.id, user_id=user_id, workspace_id=workspace_id,
                      project_id=session.project_id or "")
    started = time.monotonic()
    result = await vp.execute_generate(VideoGenerateArgs(
        action="submit", prompt=VIDEO_PROMPTS[sample_id] + NEGATIVE, duration=15, ratio="9:16",
        idempotency_key=f"light_sample:{sample_id}:{prefix}", allow_duplicate=True), ctx)
    meta = result.metadata or {}
    job_id = meta.get("job_id")
    if not job_id:
        return {"id": sample_id, "kind": "video", "error": f"{result.title}: {str(result.output)[:300]}"}
    status, asset_id = meta.get("status"), meta.get("asset_id")
    while status not in ("completed", "failed", "cancelled") and time.monotonic() - started < 20 * 60:
        waited = await vp.execute_generate(
            VideoGenerateArgs(action="wait", job_id=job_id, wait_seconds=25.0, wait_iteration=1), ctx)
        meta = waited.metadata or {}
        status = meta.get("status") or status
        asset_id = meta.get("asset_id") or asset_id
    if status != "completed":
        return {"id": sample_id, "kind": "video", "job_id": job_id, "error": status}
    if not asset_id:
        from db.base import get_db_session
        from db.models.video_job import VideoJob
        async with get_db_session() as db:
            asset_id = (await db.get(VideoJob, job_id)).output_asset_id
    return await _publish(sample_id, "video", asset_id, prefix, started, job_id=job_id)


async def _image(sample_id: str, user_id: str, workspace_id: str, prefix: str) -> dict:
    from tool.image_gen import ImageGenArgs, execute
    from tool.tool import ToolContext
    session = await _session(user_id, workspace_id, f"样片 {sample_id}", None)
    ctx = ToolContext(session_id=session.id, user_id=user_id, workspace_id=workspace_id,
                      project_id=session.project_id or "")
    size, prompt = IMAGE_PROMPTS[sample_id]
    started = time.monotonic()
    result = await execute(ImageGenArgs(prompt=prompt, size=size, quality="medium",
                                        output_format="jpeg", filename=f"{sample_id}.jpg"), ctx)
    meta = result.metadata or {}
    asset_ids = meta.get("asset_ids") or ([meta["asset_id"]] if meta.get("asset_id") else [])
    if meta.get("error") or not asset_ids:
        return {"id": sample_id, "kind": "image", "error": f"{result.title}: {str(result.output)[:300]}"}
    return await _publish(sample_id, "image", asset_ids[0], prefix, started)


async def _publish(sample_id: str, kind: str, asset_id: str, prefix: str, started: float, **extra) -> dict:
    from core.oss import get_oss
    source, mime = await _oss_key(asset_id)
    extension = ".mp4" if kind == "video" else (".png" if mime == "image/png" else ".jpg")
    key = f"{prefix.rstrip('/')}/{sample_id}{extension}"
    await get_oss().copy(source, key)
    return {"id": sample_id, "kind": kind, "key": key, "asset_id": asset_id, "mime": mime,
            "seconds": round(time.monotonic() - started, 1), **extra}


async def _balance(workspace_id: str) -> str:
    from db.base import get_db_session
    from db.models.billing import CreditBalance
    async with get_db_session() as db:
        row = await db.get(CreditBalance, workspace_id)
        return str(row.balance) if row else "0"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", required=True, help="username of the samples account")
    parser.add_argument("--only", default="", help="comma-separated sample ids (default: all)")
    parser.add_argument("--prefix", default="light/samples/v1", help="OSS key prefix for the copies")
    parser.add_argument("--grant", default="", help="admin_grant this many credits first (idempotent per prefix)")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    wanted = [item for item in args.only.split(",") if item] or [*VIDEO_PROMPTS, *IMAGE_PROMPTS]
    unknown = [item for item in wanted if item not in VIDEO_PROMPTS and item not in IMAGE_PROMPTS]
    if unknown:
        raise SystemExit(f"unknown sample ids: {unknown}")
    if args.dry_run:
        for item in wanted:
            print(json.dumps({"id": item, "prompt": VIDEO_PROMPTS.get(item) or IMAGE_PROMPTS[item][1]}, ensure_ascii=False))
        return 0
    # Same shared infrastructure the server sets up at startup (main.py).
    from cache import set_cache
    from cache.redis_cache import RedisCache
    from core.config import get_config
    from db.base import init_engine
    config = get_config()
    init_engine(config.database_url, config.db_pool_size, config.db_pool_overflow)
    config._cache = RedisCache(config.redis_url)
    set_cache(config._cache)
    user_id, workspace_id = await _account(args.user)
    if args.grant:
        await _grant(workspace_id, user_id, args.grant, f"light_samples_grant:{args.prefix}")
    before = await _balance(workspace_id)
    gate = asyncio.Semaphore(max(1, args.concurrency))

    async def one(item: str) -> dict:
        async with gate:
            try:
                if item in VIDEO_PROMPTS:
                    return await _video(item, user_id, workspace_id, args.prefix)
                return await _image(item, user_id, workspace_id, args.prefix)
            except Exception as exc:  # report and keep going with the others
                return {"id": item, "error": f"{type(exc).__name__}: {exc}"[:300]}

    for row in await asyncio.gather(*(one(item) for item in wanted)):
        print(json.dumps(row, ensure_ascii=False), flush=True)
    print(json.dumps({"balance_before": before, "balance_after": await _balance(workspace_id)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
