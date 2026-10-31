"""Light mode on the server: who is in it, which entries they can use, how a topic chat starts.

``ui_mode`` lives in ``user_preferences.extra`` (a shallow-merged key, so the
phone's other writes never erase it); new users start in ``light``, everyone
else reads as ``pro`` until they switch. Entry availability is computed here
from the workspace: an active paid plan, the desktop entitlement
(``sandbox/entitlement.py``) and bound platforms.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.log import create_logger
from light.entries import Entry, light_entries

log = create_logger(__name__)

MODES = ("light", "pro")
DEFAULT_MODE = "pro"
NEW_USER_MODE = "light"
MODE_KEY = "ui_mode"
ENTRYPOINT = "light_start"
RECENT_ROLES = ("final", "result")
RECENT_MIMES = ("image/", "video/")


class LightError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.code, self.status, self.extra = code, status, extra


# --- ui_mode ----------------------------------------------------------------

async def ui_mode(user_id: str) -> str:
    from db.repository.preference_repo import PgPreferenceRepo
    row = await PgPreferenceRepo().get(user_id)
    extra = (row or {}).get("extra") or {}
    value = extra.get(MODE_KEY) if isinstance(extra, dict) else None
    return value if value in MODES else DEFAULT_MODE


async def set_ui_mode(user_id: str, mode: str) -> str:
    if mode not in MODES:
        raise LightError("LIGHT_MODE_INVALID", "ui_mode must be light or pro")
    from db.repository.preference_repo import PgPreferenceRepo
    await PgPreferenceRepo().upsert(user_id, extra={MODE_KEY: mode})
    return mode


async def default_mode_for_new_user(user_id: str) -> None:
    """Registration hook: a new account opens in Light. Never fails the registration."""
    try:
        await set_ui_mode(user_id, NEW_USER_MODE)
    except Exception as exc:
        log.warning("ui_mode default not written user=%s error=%s", user_id, type(exc).__name__)


# --- availability -------------------------------------------------------------

async def _workspace_facts(workspace_id: str) -> dict:
    from sqlalchemy import select
    from billing.service import now
    from billing.subscriptions import active_subscription
    from db.base import get_db_session
    from db.models.platform_account import PlatformAccount
    from sandbox.entitlement import sandbox_access_status
    async with get_db_session() as db:
        subscription = await active_subscription(db, workspace_id, now())
        desktop = await sandbox_access_status(db, workspace_id)
        bound = set((await db.scalars(select(PlatformAccount.platform).where(
            PlatformAccount.workspace_id == workspace_id, PlatformAccount.status == "bound"))).all())
    return {
        "plan_id": subscription.plan_id if subscription else "free",
        "paid_plan": bool(subscription is not None and subscription.plan_id != "free"),
        "desktop": bool(desktop.get("allowed")),
        "bound": bound,
    }


def availability(entry: Entry, facts: dict) -> str:
    if entry.kind == "coming_soon":
        return "coming_soon"
    if entry.requires.plan and not facts["paid_plan"]:
        return "needs_plan"
    if entry.requires.desktop and not facts["desktop"]:
        return "needs_desktop"
    if entry.requires.binding and entry.requires.binding not in facts["bound"]:
        return "needs_binding"
    return "ok"


async def entries_view(user_id: str, workspace_id: str) -> dict:
    config = light_entries()
    facts = await _workspace_facts(workspace_id)
    return {
        "version": config.version,
        "ui_mode": await ui_mode(user_id),
        "plan_id": facts["plan_id"],
        "desktop": facts["desktop"],
        "entries": [{**entry.public(), "availability": availability(entry, facts)} for entry in config.entries],
    }


# --- start a topic chat ---------------------------------------------------------

async def start(*, user_id: str, workspace_id: str, entry_id: str, project_id: str | None = None,
                subtype: str | None = None, sample: str | None = None) -> dict:
    config = light_entries()
    entry = config.entry(entry_id)
    if entry is None:
        raise LightError("LIGHT_ENTRY_UNKNOWN", f"unknown entry {entry_id!r}")
    state = availability(entry, await _workspace_facts(workspace_id))
    if state != "ok":
        raise LightError("LIGHT_ENTRY_UNAVAILABLE", f"entry {entry.id} is {state}", status=409, availability=state)
    chosen_subtype, chosen_sample = entry.sample(subtype, sample)
    if (subtype and chosen_subtype is None) or (sample and chosen_sample is None):
        raise LightError("LIGHT_SAMPLE_UNKNOWN", "unknown subtype or sample for this entry")

    from agent.agent import default_agent_name
    from agent.model_resolve import resolve as resolve_model
    from api.v1.quality import resolve_quality
    from core.config import get_config
    from light import chat
    from session import session as session_mod
    config_ = get_config()
    model, _ = resolve_model("", config_, context="light start")
    selection: dict[str, Any] = {}
    if entry.default_tier:
        video_model, video_resolution = resolve_quality(entry.default_tier, config_)
        selection = {"video_model": video_model, "video_resolution": video_resolution}
    session = await session_mod.create_session(
        model=model, agent=default_agent_name(), title=entry.title,
        user_id=user_id, workspace_id=workspace_id, project_id=project_id,
    )
    await session_mod.update_session(session.id, user_id=user_id, light_entry=entry.id, **selection)
    opening = entry.opening(subtype, sample)
    picked = " · ".join(part.label for part in (chosen_subtype, chosen_sample) if part is not None)
    user_text = f"【{entry.title}】" + (f"选了「{picked}」" if picked else "开始")
    await chat.exchange(session.id, user_id, user_text=user_text, assistant_text=opening.text,
                        agent=session.agent, model=model, entrypoint=ENTRYPOINT)
    return {
        "session_id": session.id,
        "entry": entry.public(),
        "opening_card": opening.model_dump(),
        "steps": [step.model_dump() for step in entry.steps],
        "video_model": selection.get("video_model"),
        "video_resolution": selection.get("video_resolution"),
    }


# --- recent outputs, grouped by conversation --------------------------------------

async def recent(user_id: str, workspace_id: str, *, limit: int = 8, scan: int = 240) -> dict:
    """The person's latest image and video results, one group per session, newest first."""
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.part import Part
    from db.models.session import Session
    async with get_db_session() as db:
        rows = (await db.execute(
            select(Part, Session.title, Session.light_entry, Session.updated_at)
            .join(Session, Session.id == Part.session_id)
            .where(Part.type == "file", Part.user_id == user_id, Session.workspace_id == workspace_id,
                   Session.is_deleted.is_(False), Session.kind == "normal")
            .order_by(Part.created_at.desc()).limit(scan)
        )).all()
    groups: dict[str, dict] = {}
    for part, title, light_entry, updated_at in rows:
        data = part.data or {}
        relation = data.get("relation") or {}
        mime = str(data.get("mime_type") or "")
        if relation.get("role") not in RECENT_ROLES or not data.get("asset_id") or not mime.startswith(RECENT_MIMES):
            continue
        group = groups.get(part.session_id)
        if group is None:
            if len(groups) >= limit:
                continue
            group = groups[part.session_id] = {
                "session_id": part.session_id, "title": title or "", "entry_id": light_entry,
                "updated_at": _iso(updated_at), "outputs": [],
            }
        group["outputs"].append({
            "asset_id": data["asset_id"], "mime": mime, "kind": relation.get("kind") or "file",
            "label": relation.get("label") or relation.get("caption") or "",
            "created_at": _iso(part.created_at),
        })
    return {"groups": list(groups.values())}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()
