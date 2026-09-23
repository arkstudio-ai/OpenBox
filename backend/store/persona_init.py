"""Persona bootstrap: one agent run per store that ends in a single summary card.

Triggered off the request path after a store platform first becomes bound
(or by hand from the API). The run lives in an ordinary session titled after
the store so the merchant can find the card in the sidebar and the inbox.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from core.config import get_config
from core.log import create_logger
from db.base import get_db_session
from store import service as store_service
from store.service import PLATFORM_LABELS

log = create_logger("store.persona_init")

SKILL_NAME = "store-persona-init"
SESSION_TITLE = "你的店"
#: A second bootstrap within this window is a duplicate trigger, not a redo.
RESTART_GAP = timedelta(hours=24)
#: Run the agent loop after creating the session. Unit tests switch this off:
#: a background run would hold the shared test database.
AUTO_RUN = True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_prompt(store: dict, sites: list[str]) -> str:
    platforms = "、".join(PLATFORM_LABELS.get(s, s) for s in sites) or "已绑定的平台"
    profile = {
        "store_id": store["id"], "name": store["name"], "category": store["category"],
        "main_platforms": store["mainPlatforms"],
        "platform_bindings": {k: {kk: vv for kk, vv in v.items() if vv} for k, v in store["platformBindings"].items()},
        "persona_status": store["personaStatus"],
    }
    return (
        f"[系统任务：完善门店人设 | 触发：{platforms} 登录态已绑定]\n"
        f"请立即调用 skill(skill=\"{SKILL_NAME}\") 加载技能，并严格按技能步骤执行：读取门店在平台后台的公开信息，"
        "推断五类人设记忆，最后用 creator_context(action=\"propose_bundle\") 出一张汇总卡让店主确认。\n"
        "执行期间不要向用户提问、不要用 question 工具；唯一的一次交互就是最后那张汇总卡。\n\n"
        f"[门店档案]\n{json.dumps(profile, ensure_ascii=False)}"
    )


async def _should_start(store: dict, *, force: bool) -> bool:
    if force:
        return True
    if store["personaStatus"] == "active":
        return False
    started = store.get("personaStartedAt")
    if started:
        try:
            stamp = datetime.fromisoformat(started)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if _now() - stamp < RESTART_GAP:
                return False
        except ValueError:
            pass
    return True


async def start_persona_init(workspace_id: str, user_id: str, *, sites: list[str] | None = None,
                             force: bool = False) -> str | None:
    """Create the bootstrap session, inject the task, and run it in the background."""
    store = await store_service.get_store(workspace_id)
    if store is None or not await _should_start(store, force=force):
        return None
    from session.session import create_session, create_user_message

    session = await create_session(
        user_id=user_id, workspace_id=workspace_id, agent="build",
        model=get_config().model or "", title=SESSION_TITLE, kind="normal",
    )
    async with get_db_session() as db:
        await store_service.set_persona_state(db, workspace_id, session_id=session.id, started_at=_now())
    await create_user_message(session_id=session.id, text=build_prompt(store, sites or store["mainPlatforms"]),
                              synthetic=True, user_id=user_id)
    if AUTO_RUN:
        store_service._spawn(_run(session.id, user_id))
    else:
        log.info("persona bootstrap session %s created; auto-run disabled", session.id)
    return session.id


async def _run(session_id: str, user_id: str) -> None:
    from agent.loop import run_loop
    try:
        await run_loop(session_id, user_id)
    except Exception:
        log.exception("persona bootstrap run failed for session %s", session_id)


async def start_after_binding(workspace_id: str, user_id: str, sites: list[str]) -> None:
    """Post-commit continuation of `store.service.on_desktop_bound`."""
    try:
        session_id = await start_persona_init(workspace_id, user_id, sites=sites)
    except Exception:
        log.exception("persona bootstrap could not start for workspace %s", workspace_id)
        return
    try:
        store = await store_service.get_store(workspace_id)
        async with get_db_session() as db:
            from notifications.events import emit
            await emit(db, user_id=user_id, workspace_id=workspace_id, kind="store_bound",
                       event_key=f"store_bound:{workspace_id}:{'+'.join(sorted(sites))}",
                       name=(store or {}).get("name", ""), session_id=session_id)
    except Exception:
        log.exception("store_bound notification failed for workspace %s", workspace_id)
