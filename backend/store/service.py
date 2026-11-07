"""Store profile service: one 门店 per workspace, what the Light home reads.

Workspace scoping is explicit on every query. Rows that belong to another
workspace read as "not found" at the API layer, never as a 403.

Carried over from PR #60 without its desktop-binding and persona half. A Light
owner names the shop in the first-video form, which stays in user preferences;
the first read that finds no row turns that form into one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.store import CATEGORIES, OPEN_CATEGORIES, PLATFORMS, Store

log = create_logger("store.service")


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


def _line(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def to_public(row: Store) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspaceId": row.workspace_id,
        "name": row.name,
        "headline": row.headline,
        "category": row.category,
        "categoryOpen": row.category in OPEN_CATEGORIES,
        "mainPlatforms": list(row.main_platforms or []),
        "address": row.address,
        "city": row.city,
        "personaStatus": row.persona_status,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
    }


# ── Reads ──────────────────────────────────────────────────────────────────
async def get_store_row(db, workspace_id: str) -> Store | None:
    return await db.scalar(select(Store).where(Store.workspace_id == workspace_id))


async def get_store(workspace_id: str, user_id: str | None = None) -> dict | None:
    """The workspace's store; with `user_id`, one the first-video form implies is created first."""
    async with get_db_session() as db:
        row = await get_store_row(db, workspace_id)
        if row is not None:
            return to_public(row)
    return await _from_first_video_form(workspace_id, user_id) if user_id else None


async def _from_first_video_form(workspace_id: str, user_id: str) -> dict | None:
    from db.repository.preference_repo import PgPreferenceRepo
    prefs = await PgPreferenceRepo().get(user_id)
    extra = (prefs or {}).get("extra") or {}
    record = extra.get("first_video") if isinstance(extra, dict) else None
    if not isinstance(record, dict) or record.get("workspace_id") != workspace_id:
        return None
    return await record_first_video_shop(
        workspace_id=workspace_id, user_id=user_id, shop_name=record.get("shop_name"),
        headline=record.get("headline"), industry=record.get("industry"),
    )


# ── Writes ─────────────────────────────────────────────────────────────────
def _clean_platforms(values: list[str] | None) -> list[str]:
    seen: list[str] = []
    for value in values or []:
        if value in PLATFORMS and value not in seen:
            seen.append(value)
    return seen


async def create_store(*, workspace_id: str, user_id: str, name: str, category: str,
                       headline: str | None = None, main_platforms: list[str] | None = None,
                       address: str | None = None, city: str | None = None) -> dict:
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")
    now = _now()
    try:
        async with get_db_session() as db:
            if await get_store_row(db, workspace_id) is not None:
                raise StoreExists(workspace_id)
            row = Store(
                id=ascending("sto"), workspace_id=workspace_id, user_id=user_id,
                name=name.strip()[:128], headline=_line(headline, 40) or None, category=category,
                main_platforms=_clean_platforms(main_platforms), address=(address or None), city=(city or None),
                platform_bindings={}, data_sources={}, persona_status="none", created_at=now, updated_at=now,
            )
            db.add(row)
            await db.flush()
            return to_public(row)
    except IntegrityError as exc:  # two first reads at once: `uq_stores_workspace` picks one
        raise StoreExists(workspace_id) from exc


async def update_store(*, workspace_id: str, store_id: str, patch: dict[str, Any]) -> dict | None:
    async with get_db_session() as db:
        row = await get_store_row(db, workspace_id)
        if row is None or row.id != store_id:
            return None
        if patch.get("name") is not None:
            row.name = str(patch["name"]).strip()[:128] or row.name
        if patch.get("category") is not None:
            if patch["category"] not in CATEGORIES:
                raise ValueError(f"category must be one of {CATEGORIES}")
            row.category = patch["category"]
        if patch.get("main_platforms") is not None:
            row.main_platforms = _clean_platforms(patch["main_platforms"])
        if "headline" in patch:
            row.headline = _line(patch["headline"], 40) or None
        for field in ("address", "city"):
            if field in patch:
                setattr(row, field, (patch[field] or None))
        row.updated_at = _now()
        await db.flush()
        return to_public(row)


async def record_first_video_shop(*, workspace_id: str, user_id: str, shop_name: str | None,
                                  headline: str | None, industry: str | None) -> dict | None:
    """The first-video form names the shop. A store already there is left as it is."""
    name = _line(shop_name, 40)
    if not name:
        return None
    category = industry if industry in CATEGORIES else "other"
    try:
        return await create_store(workspace_id=workspace_id, user_id=user_id, name=name,
                                  category=category, headline=headline)
    except StoreExists:
        return await get_store(workspace_id)


# ── Starter cards (首页「今天可以做」) ──────────────────────────────────────
#: Each card opens a Light entry, so `entry` is an id in `light/entries.json`.
_CARDS: dict[str, dict[str, list[dict[str, str]]]] = {
    "food": {
        "zh-CN": [
            {"entry": "video", "title": "给{store}的招牌菜{signature}做一条短视频", "hint": "挑个样子就能出片"},
            {"entry": "shopkeeper", "title": "把{store}最近的差评逐条写好回复", "hint": "读取评价，按你的语气写好"},
            {"entry": "poster", "title": "给{store}本周末的团购做张海报", "hint": "说一句活动内容就有了"},
        ],
        "en-US": [
            {"entry": "video", "title": "Make a short video of {store}'s signature dish {signature}", "hint": "Pick a look and get the clip"},
            {"entry": "shopkeeper", "title": "Draft a reply to each of {store}'s recent bad reviews", "hint": "Reads the reviews and answers in your voice"},
            {"entry": "poster", "title": "Make a poster for {store}'s weekend deal", "hint": "One line about the deal is enough"},
        ],
    },
    "generic": {
        "zh-CN": [
            {"entry": "video", "title": "给{store}{signature}做一条新客体验短视频", "hint": "挑个样子就能出片"},
            {"entry": "shopkeeper", "title": "把{store}最近的评价整理成一页总结", "hint": "读取你绑定的平台，出一页看得懂的总结"},
            {"entry": "poster", "title": "给{store}本周的活动做张海报", "hint": "说一句活动内容就有了"},
        ],
        "en-US": [
            {"entry": "video", "title": "Make a short new-customer video for {store} {signature}", "hint": "Pick a look and get the clip"},
            {"entry": "shopkeeper", "title": "Summarise {store}'s recent reviews on one page", "hint": "Reads your connected platforms and writes a plain summary"},
            {"entry": "poster", "title": "Make a poster for {store}'s activity this week", "hint": "One line about the activity is enough"},
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
    """Three things to do today, grounded in the store: its name, and what it leads with."""
    lang = "zh-CN" if locale.lower().startswith("zh") else "en-US"
    templates = _CARDS.get(store.category, _CARDS["generic"])[lang]
    signature = _short(store.headline or _first_summary(memories, "SIGNATURE_CASE")
                       or _first_summary(memories, "OFFERING"))
    quoted = ("「" + signature + "」" if lang == "zh-CN" else signature) if signature else ""
    store_name = store.name.strip() or ("你的店" if lang == "zh-CN" else "your store")
    return [{
        "entry": card["entry"],
        "title": " ".join(card["title"].replace("{store}", store_name).replace("{signature}", quoted).split()),
        "hint": card["hint"],
    } for card in templates]


async def starter_cards_for(workspace_id: str, user_id: str, *, locale: str = "zh-CN") -> list[dict[str, str]] | None:
    async with get_db_session() as db:
        row = await get_store_row(db, workspace_id)
        if row is None:
            return None
    from memory.service import list_active_memories
    memories = await list_active_memories(user_id=user_id, workspace_id=workspace_id)
    return starter_cards(row, memories, locale=locale)
