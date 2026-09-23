"""Store profile service: one 门店 per workspace, bindings learned from desktop probes.

Workspace scoping is explicit on every query. Rows that belong to another
workspace read as "not found" at the API layer, never as a 403.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from bus import bus
from bus.events import STORE_UPDATED
from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.store import CATEGORIES, OPEN_CATEGORIES, PLATFORMS, Store

log = create_logger("store.service")

#: Desktop login sites whose `bound` transition means "the store is connected".
STORE_SITES = frozenset(PLATFORMS)
PLATFORM_LABELS = {"douyin_laike": "抖音来客", "meituan_merchant": "美团经营宝"}


class StoreExists(Exception):
    """The workspace already has a store; PATCH it instead."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite hands back naive UTC
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def to_public(row: Store) -> dict[str, Any]:
    bindings = {}
    for key, binding in (row.platform_bindings or {}).items():
        bindings[key] = {
            "status": binding.get("status", "unknown"),
            "accountId": binding.get("account_id"),
            "accountName": binding.get("account_name"),
            "role": binding.get("role"),
            "shopId": binding.get("shop_id"),
            "boundAt": binding.get("bound_at"),
            "updatedAt": binding.get("updated_at"),
        }
    return {
        "id": row.id,
        "workspaceId": row.workspace_id,
        "name": row.name,
        "category": row.category,
        "categoryOpen": row.category in OPEN_CATEGORIES,
        "mainPlatforms": list(row.main_platforms or []),
        "address": row.address,
        "city": row.city,
        "platformBindings": bindings,
        "dataSources": dict(row.data_sources or {}),
        "personaStatus": row.persona_status,
        "personaSessionId": row.persona_session_id,
        "personaStartedAt": _iso(row.persona_started_at),
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


def _publish(row: Store, user_id: str | None) -> None:
    if not user_id:
        return
    bus.publish(STORE_UPDATED, {"userId": user_id, "workspaceId": row.workspace_id, "storeId": row.id,
                                "personaStatus": row.persona_status})


# ── Reads ──────────────────────────────────────────────────────────────────
async def get_store_row(db, workspace_id: str) -> Store | None:
    return await db.scalar(select(Store).where(Store.workspace_id == workspace_id))


async def get_store(workspace_id: str) -> dict | None:
    async with get_db_session() as db:
        row = await get_store_row(db, workspace_id)
        return to_public(row) if row else None


# ── Writes ─────────────────────────────────────────────────────────────────
def _clean_platforms(values: list[str] | None) -> list[str]:
    seen: list[str] = []
    for value in values or []:
        if value in PLATFORMS and value not in seen:
            seen.append(value)
    return seen


async def create_store(*, workspace_id: str, user_id: str, name: str, category: str,
                       main_platforms: list[str] | None = None, address: str | None = None,
                       city: str | None = None) -> dict:
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")
    now = _now()
    async with get_db_session() as db:
        if await get_store_row(db, workspace_id) is not None:
            raise StoreExists(workspace_id)
        row = Store(
            id=ascending("sto"), workspace_id=workspace_id, user_id=user_id,
            name=name.strip()[:128], category=category, main_platforms=_clean_platforms(main_platforms),
            address=(address or None), city=(city or None), platform_bindings={}, data_sources={},
            persona_status="none", created_at=now, updated_at=now,
        )
        db.add(row)
        await db.flush()
        public = to_public(row)
    _publish(row, user_id)
    return public


async def update_store(*, workspace_id: str, store_id: str, user_id: str, patch: dict[str, Any]) -> dict | None:
    async with get_db_session() as db:
        row = await get_store_row(db, workspace_id)
        if row is None or row.id != store_id:
            return None
        if "name" in patch and patch["name"] is not None:
            row.name = str(patch["name"]).strip()[:128] or row.name
        if "category" in patch and patch["category"] is not None:
            if patch["category"] not in CATEGORIES:
                raise ValueError(f"category must be one of {CATEGORIES}")
            row.category = patch["category"]
        if "main_platforms" in patch and patch["main_platforms"] is not None:
            row.main_platforms = _clean_platforms(patch["main_platforms"])
        for field in ("address", "city"):
            if field in patch:
                setattr(row, field, (patch[field] or None))
        row.updated_at = _now()
        await db.flush()
        public = to_public(row)
    _publish(row, user_id)
    return public


def _identity_from_probe(row) -> dict[str, Any]:
    """What a desktop login row knows about the store on that platform."""
    display = (row.probe_detail or {}).get("display") or {}
    identity = {
        "account_id": display.get("account_id") or (row.external_id if not str(row.external_id or "").startswith("desk:") else None),
        "account_name": display.get("account_name") or row.nickname,
        "role": display.get("role"),
        "shop_id": display.get("shop_id"),
    }
    return {k: v for k, v in identity.items() if v}


async def record_platform_binding(db, *, workspace_id: str, user_id: str, site_key: str, status: str,
                                  identity: dict[str, Any], now: datetime,
                                  display_name: str = "") -> tuple[Store, bool]:
    """Merge one platform's state into the store; returns (store, first_bound).

    Runs inside the caller's transaction and never commits. A workspace whose
    merchant skipped registration gets a placeholder store named after the
    platform account so binding never depends on the onboarding form.
    """
    if site_key not in STORE_SITES:
        raise ValueError(f"unknown store platform: {site_key}")
    row = await get_store_row(db, workspace_id)
    if row is None:
        row = Store(
            id=ascending("sto"), workspace_id=workspace_id, user_id=user_id,
            name=(identity.get("account_name") or display_name or PLATFORM_LABELS.get(site_key, site_key))[:128],
            category="food", main_platforms=[site_key], platform_bindings={}, data_sources={},
            persona_status="none", created_at=now, updated_at=now,
        )
        db.add(row)
    bindings = dict(row.platform_bindings or {})
    previous = dict(bindings.get(site_key) or {})
    first_bound = status == "bound" and previous.get("status") != "bound"
    merged = {**previous, **identity, "status": status, "updated_at": now.isoformat()}
    if first_bound and not previous.get("bound_at"):
        merged["bound_at"] = now.isoformat()
    bindings[site_key] = merged
    row.platform_bindings = bindings
    if site_key not in (row.main_platforms or []):
        row.main_platforms = [*(row.main_platforms or []), site_key]
    row.updated_at = now
    await db.flush()
    return row, first_bound


async def on_desktop_bound(db, account_row, site, now: datetime, *, user_id: str) -> None:
    """Hook for `probe_workspace`: a store site just transitioned to `bound`.

    Records the binding in the same transaction; once that commits, starts the
    persona bootstrap (its own session and notification) off the request path.
    """
    if site.key not in STORE_SITES:
        return
    user_id = user_id or account_row.bound_by_user_id or ""
    row, first_bound = await record_platform_binding(
        db, workspace_id=account_row.workspace_id, user_id=user_id, site_key=site.key, status="bound",
        identity=_identity_from_probe(account_row), now=now, display_name=site.display,
    )
    if not first_bound or not user_id:
        return
    workspace_id, site_key = row.workspace_id, site.key
    from sqlalchemy import event

    sync_session = db.sync_session
    pending = sync_session.info.setdefault("store_bound_sites", [])
    if not pending:
        @event.listens_for(sync_session, "after_commit", once=True)
        def _kick(_):
            sites = list(sync_session.info.pop("store_bound_sites", []))
            from store.persona_init import start_after_binding
            _spawn(start_after_binding(workspace_id, user_id, sites))
    pending.append(site_key)


_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


async def set_persona_state(db, workspace_id: str, *, status: str | None = None, session_id: str | None = None,
                            started_at: datetime | None = None) -> Store | None:
    """Update persona bookkeeping inside the caller's transaction."""
    row = await get_store_row(db, workspace_id)
    if row is None:
        return None
    if status is not None:
        row.persona_status = status
    if session_id is not None:
        row.persona_session_id = session_id
    if started_at is not None:
        row.persona_started_at = started_at
    row.updated_at = _now()
    await db.flush()
    return row


# ── Starter cards ──────────────────────────────────────────────────────────
_CARDS: dict[str, dict[str, list[dict[str, str]]]] = {
    "food": {
        "zh-CN": [
            {"title": "给{store}的招牌菜{signature}做一条 30 秒到店短视频", "hint": "先出脚本给你确认，再生成成片"},
            {"title": "把{store}最近的差评整理出来，逐条写好回复", "hint": "读取评价，按你的语气写三版回复"},
            {"title": "为{store}本周末的团购活动写 3 版推广文案", "hint": "适合抖音来客和美团的短文案，选一版直接发"},
        ],
        "en-US": [
            {"title": "Make a 30-second in-store video for {store}'s signature dish {signature}", "hint": "Script first for your approval, then the final cut"},
            {"title": "Collect {store}'s recent negative reviews and draft a reply to each", "hint": "Reads reviews and writes three reply versions in your voice"},
            {"title": "Write 3 promo copies for {store}'s weekend group-buy deal", "hint": "Short copy for Douyin Laike and Meituan, ready to post"},
        ],
    },
    "generic": {
        "zh-CN": [
            {"title": "给{store}做一条 30 秒的新客体验短视频", "hint": "先出脚本给你确认，再生成成片"},
            {"title": "把{store}最近的评价整理成一页总结", "hint": "读取你绑定的平台，出一页看得懂的总结"},
            {"title": "为{store}本周的活动写 3 版推广文案", "hint": "三个版本，选一个直接发"},
        ],
        "en-US": [
            {"title": "Make a 30-second new-customer video for {store}", "hint": "Script first for your approval, then the final cut"},
            {"title": "Summarise {store}'s recent reviews on one page", "hint": "Reads your connected platforms and writes a plain summary"},
            {"title": "Write 3 promo copies for {store}'s activity this week", "hint": "Three versions, pick one and post"},
        ],
    },
}


def _first_summary(memories: list[dict], type_name: str) -> str:
    for item in memories:
        if item.get("type") == type_name:
            summary = (item.get("value") or {}).get("summary")
            if isinstance(summary, str) and summary.strip():
                return summary.strip()
    return ""


def _short(text: str, limit: int = 12) -> str:
    text = " ".join(text.split())
    for sep in ("；", ";", "，", ",", "。", "、"):
        if sep in text:
            text = text.split(sep, 1)[0]
    return text[:limit]


def starter_cards(store: Store, memories: list[dict], *, locale: str = "zh-CN") -> list[dict[str, str]]:
    """Three suggestion cards for the empty chat page, grounded in the store."""
    lang = "zh-CN" if locale.lower().startswith("zh") else "en-US"
    templates = _CARDS.get(store.category, _CARDS["generic"])[lang]
    signature = _short(_first_summary(memories, "SIGNATURE_CASE") or _first_summary(memories, "OFFERING"))
    if not signature:
        signature = "" if lang == "zh-CN" else ""
    store_name = store.name.strip() or ("你的店" if lang == "zh-CN" else "your store")
    cards = []
    for card in templates:
        title = card["title"].replace("{store}", store_name)
        if "{signature}" in title:
            if signature:
                title = title.replace("{signature}", ("「" + signature + "」") if lang == "zh-CN" else signature)
            else:
                title = title.replace("{signature}", "")
                title = " ".join(title.split())
        cards.append({"title": title, "hint": card["hint"]})
    return cards


async def starter_cards_for(workspace_id: str, user_id: str, *, locale: str = "zh-CN") -> list[dict[str, str]] | None:
    async with get_db_session() as db:
        row = await get_store_row(db, workspace_id)
        if row is None:
            return None
    from memory.service import list_active_memories
    memories = await list_active_memories(user_id=user_id, workspace_id=workspace_id)
    return starter_cards(row, memories, locale=locale)


async def store_brief(workspace_id: str, user_id: str, *, limit: int = 200) -> str:
    """One short line about the store + persona for auxiliary prompts (≤ `limit` chars)."""
    async with get_db_session() as db:
        row = await get_store_row(db, workspace_id)
        if row is None:
            return ""
    from memory.service import list_active_memories
    memories = await list_active_memories(user_id=user_id, workspace_id=workspace_id)
    bits = [f"门店：{row.name}（{row.category}）"]
    for type_name, label in (("IDENTITY", "身份"), ("OFFERING", "主推")):
        summary = _first_summary(memories, type_name)
        if summary:
            bits.append(f"{label}：{summary}")
    text = "；".join(bits)
    return text if len(text) <= limit else text[: limit - 1] + "…"
