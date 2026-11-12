"""One video model per conversation, and the card that changes it.

A conversation generates every shot with the same model and resolution. When
the person picked them in the composer, that pick is the lock. When nobody
picked, the first submit locks the deployment's default tier onto the
conversation. Either way, a submit that asks for anything else does not run:
it files a ``video_model_switch`` card ("switch to X?" / "keep Y") and the
person decides, on screen or by voice in a call. The answer is written to the
conversation in the same transaction that resumes the waiting call.

Unattended runs (cron sessions, marketing autopilot) keep the older rule: the
session's pick if any, else the model the agent named. Nobody is there to
answer a card, and autopilot enforces its own template lock.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from question.cards import CardKind, CardOption, ask_card, register

KIND = "video_model_switch"


@dataclass(frozen=True)
class VideoChoice:
    model: str
    resolution: str


def _tiers(config) -> list[dict]:
    if not getattr(getattr(config, "model_tiers", None), "video", None):
        return []
    from api.metadata import _model_tiers
    return _model_tiers(config)["video"]


def default_resolution(config, model: str) -> str:
    """What a model generates at when nobody named a resolution: its tier's default, else the deployment's."""
    from tool import video_providers
    tier = next((row for row in _tiers(config) if row["model"] == model), None)
    if tier is not None and tier.get("resolution"):
        return tier["resolution"]
    declared = video_providers.declared_model(model, config)
    allowed = list(getattr(declared, "resolutions", None) or [])
    default = config.video_generation.default_resolution
    if allowed and default not in allowed:
        return allowed[0]
    return default or ""


def default_choice(config) -> VideoChoice:
    """The preset of the configured default model, else the first preset, else the default model alone."""
    default = config.video_generation.model
    tiers = _tiers(config)
    tier = next((row for row in tiers if row["model"] == default), None) or (tiers[0] if tiers else None)
    if tier is not None:
        return VideoChoice(tier["model"], tier["resolution"] or default_resolution(config, tier["model"]))
    return VideoChoice(default, default_resolution(config, default))


def describe(config, choice: VideoChoice) -> dict[str, Any]:
    """Display fields for one option: tier name, description, price per second."""
    from billing.media import quote_generation
    from core.model_tiers import video_label
    tier = next((row for row in _tiers(config) if row["model"] == choice.model
                 and (not row["resolutions"] or choice.resolution in row["resolutions"])), None)
    per_second, currency = None, ""
    if choice.resolution:
        try:
            quote = quote_generation(choice.model, choice.resolution, 1.0)
            if quote.credits is not None:
                per_second, currency = quote.snapshot["per_second"], quote.snapshot.get("currency", "")
        except Exception:  # a missing rate only hides the price
            pass
    return {"model": choice.model, "resolution": choice.resolution,
            "tier": tier["tier"] if tier else None,
            "tier_label": video_label(tier["tier"], tier.get("label") or "") if tier else None,
            "description": tier.get("description", "") if tier else "",
            "per_second": per_second, "currency": currency}


def _name(info: dict) -> str:
    name = info["tier_label"] or info["model"]
    return f"{name} · {info['resolution']}" if info["resolution"] else name


def _price(info: dict) -> str:
    return f"{info['per_second']} 积分/秒" if info["per_second"] else ""


async def session_state(ctx) -> tuple[Any, str, str]:
    """(session row or None, picked model, picked resolution); a failed read counts as nothing picked."""
    try:
        from db.base import get_db_session
        from db.models.session import Session
        if not getattr(ctx, "session_id", ""):
            return None, "", ""
        async with get_db_session() as db:
            row = await db.get(Session, ctx.session_id)
        if row is None or row.user_id != ctx.user_id:
            return None, "", ""
        return row, row.video_model or "", row.video_resolution or ""
    except Exception:
        return None, "", ""


def unattended(session, session_id: str) -> bool:
    from autopilot import ledger
    return session is None or session.kind == "cron" or ledger.get(session_id or "") is not None


async def lock_default(ctx, choice: VideoChoice) -> None:
    """Pin the default onto a conversation that has no pick yet; a concurrent pick wins."""
    from sqlalchemy import update
    from bus import bus
    from bus.events import SESSION_UPDATED
    from db.base import get_db_session
    from db.models.session import Session
    async with get_db_session() as db:
        result = await db.execute(update(Session).where(
            Session.id == ctx.session_id, Session.user_id == ctx.user_id, Session.video_model.is_(None))
            .values(video_model=choice.model, video_resolution=choice.resolution or None))
    if result.rowcount:
        bus.publish(SESSION_UPDATED, {"userId": ctx.user_id, "sessionId": ctx.session_id,
                                      "video_model": choice.model, "video_resolution": choice.resolution})


async def pending_switch(ctx) -> bool:
    """A switch card already waits in this conversation (parallel shots ask only once)."""
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.question import QuestionCheckpoint
    async with get_db_session() as db:
        rows = (await db.scalars(select(QuestionCheckpoint.continuation).where(
            QuestionCheckpoint.session_id == ctx.session_id, QuestionCheckpoint.status == "pending"))).all()
    return any(((row or {}).get("card") or {}).get("kind") == KIND for row in rows)


#: Parallel shots of one step check and file under one lock, so they ask once.
_asking: dict[str, asyncio.Lock] = {}


async def ask_switch(ctx, config, current: VideoChoice, requested: VideoChoice, *, picked: bool) -> None:
    """File the switch card and suspend the call; returns only when no card can be shown here."""
    if not ctx.part_id or not ctx.message_id:
        return
    lock = _asking.setdefault(ctx.session_id, asyncio.Lock())
    try:
        async with lock:
            if await pending_switch(ctx):
                from tool.video_production import _RequestError
                raise _RequestError(
                    "A card asking the person whether to switch the video model is already waiting in this "
                    "conversation. Do not submit more shots until it is answered.")
            await _file(ctx, config, current, requested, picked=picked)
    finally:
        if not lock.locked():
            _asking.pop(ctx.session_id, None)


async def _file(ctx, config, current: VideoChoice, requested: VideoChoice, *, picked: bool) -> None:
    now, wanted = describe(config, current), describe(config, requested)
    source = "你在输入框选的" if picked else "这个对话默认的"
    question = (f"要把这个对话的视频模型从「{_name(now)}」换成「{_name(wanted)}」吗？"
                f"\n现在用的是{source}「{_name(now)}」{('（' + _price(now) + '）') if _price(now) else ''}；"
                f"换成「{_name(wanted)}」{('（' + _price(wanted) + '）') if _price(wanted) else ''}。"
                "\n选定后这个对话后面的镜头都用它。")
    options = [
        CardOption(id="switch", label=f"换成{_name(wanted)}", description=_price(wanted) or wanted["description"],
                   extra={"video": wanted}),
        CardOption(id="keep", label=f"保持{_name(now)}", description=_price(now) or now["description"],
                   role="cancel", recommended=True, extra={"video": now}),
    ]
    await ask_card(ctx, kind=KIND, header="换视频模型", question=question, options=options,
                   payload={"current": [current.model, current.resolution],
                            "requested": [requested.model, requested.resolution]},
                   display={"current": now, "requested": wanted})


async def _apply(db, session, row, payload: dict, option: dict | None) -> dict:
    current, requested = VideoChoice(*payload["current"]), VideoChoice(*payload["requested"])
    target = requested if option and option["id"] == "switch" else current
    session.video_model = target.model
    session.video_resolution = target.resolution or None
    events = [{"type": "session.updated", "data": {"userId": session.user_id, "sessionId": session.id,
               "video_model": target.model, "video_resolution": target.resolution}}]
    again = ("Submit the shot again now with the same arguments but WITHOUT model and resolution: the "
             "conversation's video model decides them.")
    if target is requested:
        return {"title": "视频模型已切换",
                "output": f"The person switched this conversation's video model to {target.model} "
                          f"{target.resolution}. Every following shot uses it. {again}",
                "metadata": {"video_model": target.model, "video_resolution": target.resolution}, "events": events}
    return {"title": "保持原视频模型",
            "output": f"The person kept {target.model} {target.resolution} for this conversation. {again} Do not "
                      "ask to switch again unless the person asks for another model in a new message; if they "
                      "do, they can also pick it in the composer's video tier.",
            "metadata": {"video_model": target.model, "video_resolution": target.resolution}, "events": events}


register(CardKind(kind=KIND, voice_label="换视频模型", apply=_apply, voice_answerable=True, present="sheet"))
