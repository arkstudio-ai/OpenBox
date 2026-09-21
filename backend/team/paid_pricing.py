"""Apply a paid operation's frozen tariff in the existing media billing path."""
from __future__ import annotations

import math
from decimal import Decimal

from sqlalchemy import select


async def frozen_media_quote(db, session, key: str, price, *, kind: str, tokens: dict):
    if session is None or session.kind != "team_member":
        return price
    from billing.media import MediaQuote
    from billing.pricing import PRECISION
    from db.models.team import TeamEvent, TeamRun
    from team.errors import TeamError
    from team.journal import read_state
    run = await db.scalar(select(TeamRun).join(TeamEvent, TeamEvent.team_run_id == TeamRun.id).where(
        TeamEvent.kind == "team.member.admitted", TeamEvent.entity_id == session.id,
        TeamRun.owner_user_id == session.user_id, TeamRun.workspace_id == session.workspace_id,
        TeamRun.project_id == session.project_id))
    if run is None:
        raise TeamError("INVALID_RESERVATION", "A paid team member has no durable admission.")
    state = await read_state(db, run)
    reservation = next((r for r in state["reservations"].values()
        if r.get("member_id") == session.id and key in r.get("billing_keys", [])), None)
    if reservation is None:
        raise TeamError("INVALID_RESERVATION", "The paid team operation has no matching reservation.")
    tariff = reservation["pricing"]
    metadata = {"team_attribution": reservation["team_attribution"]} if reservation.get("team_attribution") else {}
    if reservation["tool"] == "video_analyze" and kind == "video_transcribe":
        tariff = tariff.get("transcription") or {}
    elif reservation["tool"] != kind:
        raise TeamError("INVALID_RESERVATION", "The billing kind differs from the reserved operation.")
    if tariff.get("model") != price.model_id:
        raise TeamError("INVALID_RESERVATION", "The billed model differs from its frozen price.")
    if kind == "image_gen":
        units, unit_key = int(tokens.get("images") or 0), "per_image"
    elif kind == "video_generate":
        units, unit_key = math.ceil(float(tokens.get("duration_sec") or 0) - 1e-9), "per_second"
    else:
        duration = tokens.get("duration_sec")
        if duration is None or not math.isfinite(float(duration)) or float(duration) <= 0:
            from dataclasses import replace
            return replace(price, snapshot={**price.snapshot, **metadata})
        units = max(int(tariff.get("min_minutes", 1)), math.ceil(float(duration) / 60 - 1e-9))
        unit_key = "per_minute"
    rate = tariff.get(unit_key)
    if rate is None or units <= 0:
        raise TeamError("INVALID_RESERVATION", "The reserved tariff cannot price the reported quantity.")
    credits = (Decimal(rate) * units).quantize(PRECISION)
    return MediaQuote(price.model_id, price.tier, units, credits, {
        **tariff, **metadata, "reserved_pricing": True, "reported_units": units,
        "reservation_id": reservation["id"], "duration_sec": tokens.get("duration_sec")})
