"""Voice call records, the daily quota, the one-call-per-user lock and call summaries."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from core.identifier import generate_id
from db.base import get_db_session
from db.models.voice import VoiceCall, VoiceTurn
from voice.meter import PRICE_DATE, RATES

LOCK_TTL_SECONDS = 60
SUMMARY_CHARS = 600  # the summarizer is asked for 300 characters; a little slack, never a transcript
_fallback_cache = None


def _cache():
    """The shared cache (Redis with auth), else the ticket store's, else a process-local one."""
    from cache import get_cache
    import auth.ticket as tickets
    cache = get_cache() or tickets._cache
    if cache is None:
        global _fallback_cache
        if _fallback_cache is None:
            from cache.memory_cache import MemoryCache
            _fallback_cache = MemoryCache()
        cache = _fallback_cache
    return cache


def _lock_key(user_id: str) -> str:
    return f"voice:lock:{user_id}"


async def acquire_lock(user_id: str) -> bool:
    """One call per user across workers: only the first increment owns the call."""
    return await _cache().incr(_lock_key(user_id), ttl=LOCK_TTL_SECONDS) == 1


async def renew_lock(user_id: str) -> None:
    # An integer, so a racing INCR on Redis still counts instead of failing.
    await _cache().set(_lock_key(user_id), 1, ttl=LOCK_TTL_SECONDS)


async def release_lock(user_id: str) -> None:
    await _cache().delete(_lock_key(user_id))


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def remaining_seconds_today(user_id: str, daily_seconds: int, now: datetime | None = None) -> int:
    """Seconds left in the user's UTC day; calls count on the day they started."""
    start = (now or _now()).replace(hour=0, minute=0, second=0, microsecond=0)
    async with get_db_session() as db:
        used = await db.scalar(select(func.coalesce(func.sum(VoiceCall.duration_seconds), 0)).where(
            VoiceCall.user_id == user_id, VoiceCall.started_at >= start))
    return max(0, daily_seconds - int(used or 0))


async def create_call(*, user_id: str, workspace_id: str, main_session_id: str, client: str,
                      model: str, voice: str) -> str:
    call_id = generate_id()
    async with get_db_session() as db:
        db.add(VoiceCall(id=call_id, user_id=user_id, workspace_id=workspace_id, main_session_id=main_session_id,
                         client=client[:16], model=model[:64], voice=voice[:32], status="active",
                         started_at=_now(), duration_seconds=0, turns=0, usage={}, estimated_yuan="0",
                         unreported_rounds=0, price_date=PRICE_DATE))
    return call_id


async def finish_call(call_id: str, *, status: str, end_reason: str | None, duration_seconds: float,
                      turns: int, snapshot: dict) -> None:
    async with get_db_session() as db:
        await db.execute(update(VoiceCall).where(VoiceCall.id == call_id).values(
            status=status, end_reason=end_reason, ended_at=_now(), duration_seconds=int(round(duration_seconds)),
            turns=turns, usage={key: int(snapshot["tokens"].get(key, 0)) for key in RATES},
            estimated_yuan=snapshot["total_yuan"][:16], unreported_rounds=snapshot["unreported_rounds"],
            price_date=snapshot["price_date"]))


async def save_summary(call_id: str, summary: str) -> None:
    async with get_db_session() as db:
        await db.execute(update(VoiceCall).where(VoiceCall.id == call_id).values(summary=summary[:SUMMARY_CHARS]))


async def previous_call(user_id: str, workspace_id: str) -> dict | None:
    """The user's latest finished call in this workspace: when it ended and its summary (may be empty)."""
    async with get_db_session() as db:
        row = (await db.execute(select(VoiceCall.id, VoiceCall.ended_at, VoiceCall.summary).where(
            VoiceCall.user_id == user_id, VoiceCall.workspace_id == workspace_id,
            VoiceCall.ended_at.is_not(None), VoiceCall.duration_seconds > 0,
        ).order_by(VoiceCall.ended_at.desc(), VoiceCall.id.desc()).limit(1))).first()
    if row is None:
        return None
    return {"call_id": row.id, "ended_at": _aware(row.ended_at), "summary": row.summary or ""}


async def latest_call_summary(user_id: str, workspace_id: str, within_hours: int = 24) -> dict | None:
    """The newest call summary of the last ``within_hours`` (for the greeting, and later the text assistant).

    ``{"call_id", "ended_at", "summary"}`` or None. Only the owner's own calls
    in this workspace; a call that ended without a summary is skipped.
    """
    since = _now() - timedelta(hours=within_hours)
    async with get_db_session() as db:
        row = (await db.execute(select(VoiceCall.id, VoiceCall.ended_at, VoiceCall.summary).where(
            VoiceCall.user_id == user_id, VoiceCall.workspace_id == workspace_id,
            VoiceCall.ended_at >= since, VoiceCall.summary.is_not(None), VoiceCall.summary != "",
        ).order_by(VoiceCall.ended_at.desc(), VoiceCall.id.desc()).limit(1))).first()
    if row is None:
        return None
    return {"call_id": row.id, "ended_at": _aware(row.ended_at), "summary": row.summary}


def _aware(value: datetime) -> datetime:
    # SQLite returns naive values for timezone-aware columns; they were written in UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def add_turn(*, turn_id: str, call_id: str, user_id: str, provider_call_id: str, transcript: str,
                   inbox_id: str | None = None, message_id: str | None = None, outcome: str = "pending") -> None:
    async with get_db_session() as db:
        db.add(VoiceTurn(id=turn_id, call_id=call_id, user_id=user_id, provider_call_id=provider_call_id[:64],
                         inbox_id=inbox_id, message_id=message_id, transcript=transcript,
                         requested_at=_now(), outcome=outcome))


async def update_turn(turn_id: str, **fields) -> None:
    """``settled``/``delivered`` stamp the matching time; other fields are columns."""
    for flag, column in (("settled", "settled_at"), ("delivered", "delivered_at")):
        if fields.pop(flag, False):
            fields[column] = _now()
    if fields:
        async with get_db_session() as db:
            await db.execute(update(VoiceTurn).where(VoiceTurn.id == turn_id).values(**fields))
