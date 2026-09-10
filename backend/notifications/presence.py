"""Conservative visibility policy; a disconnected socket is NOT app exit."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update

from auth.mobile import mobile_transaction, now, require_mobile, utc
from db.models.push import MobilePresence, PushDelivery, PushMessage

HEARTBEAT_SECONDS = 15
FOREGROUND_FRESH_SECONDS = 45
OFFLINE_AFTER_SECONDS = 90
BACKGROUND_SETTLE_SECONDS = 3
VISIBLE_STATES = {"resumed", "inactive"}


@dataclass(frozen=True)
class PresenceDecision:
    state: str
    action: str  # allow | suppress | defer
    until: datetime | None = None


def decide(presence, *, at=None, fallback_at=None):
    at = at or now()
    reported = utc(presence.reported_at) if presence else utc(fallback_at or at)
    age = max(0, (at - reported).total_seconds())
    state = presence.state if presence else "detached"
    if state in {"hidden", "paused"}:
        until = reported + timedelta(seconds=BACKGROUND_SETTLE_SECONDS)
        return PresenceDecision("background", "allow" if at >= until else "defer", until)
    if age >= OFFLINE_AFTER_SECONDS:
        return PresenceDecision("offline_inferred", "allow")
    if state in VISIBLE_STATES and age <= FOREGROUND_FRESH_SECONDS:
        return PresenceDecision("foreground" if state == "resumed" else "foreground_inactive", "suppress")
    return PresenceDecision("reconnecting" if state in VISIBLE_STATES else "unknown", "defer",
                            reported + timedelta(seconds=OFFLINE_AFTER_SECONDS))


async def for_delivery(db, delivery):
    presence = await db.get(MobilePresence, delivery.user_id)
    if presence and presence.mobile_session_id != delivery.mobile_session_id:
        presence = None
    message = await db.get(PushMessage, delivery.message_id)
    return decide(presence, fallback_at=message.created_at if message else now())


def public_status(presence):
    decision = decide(presence)
    return {"reportedState": presence.state if presence else "detached",
            "appState": decision.state, "sequence": presence.sequence if presence else 0,
            "pushAllowed": decision.action == "allow", "policy": decision.action,
            "heartbeatSeconds": HEARTBEAT_SECONDS, "offlineAfterSeconds": OFFLINE_AFTER_SECONDS}


async def report(user_id, sid, body):
    async with mobile_transaction() as db:
        await require_mobile(db, user_id, sid)
        presence = await db.get(MobilePresence, user_id)
        if presence and presence.mobile_session_id == sid and body.sequence <= presence.sequence:
            return {**public_status(presence), "applied": False}
        if presence is None:
            presence = MobilePresence(user_id=user_id)
            db.add(presence)
        presence.mobile_session_id = sid
        presence.state, presence.sequence, presence.reported_at = body.state, body.sequence, now()
        if body.state in VISIBLE_STATES:
            # A queued remote test has a ten-second setup window. All business
            # messages and tests already due are cancelled on foreground return.
            messages = select(PushMessage.id).where(
                PushMessage.user_id == user_id,
                ~PushMessage.event_key.like("system_test:%"),
            )
            await db.execute(update(PushDelivery).where(
                PushDelivery.user_id == user_id, PushDelivery.mobile_session_id == sid,
                PushDelivery.status.in_(("pending", "sending")),
                (PushDelivery.message_id.in_(messages)) | (PushDelivery.available_at <= now()),
            ).values(status="cancelled", error="app_foreground", lease_id=None, lease_until=None))
        elif body.state in {"hidden", "paused"}:
            # An explicit background report ends an earlier unknown/reconnect
            # grace. Keep provider retry backoff and scheduled tests intact.
            await db.execute(update(PushDelivery).where(
                PushDelivery.user_id == user_id, PushDelivery.mobile_session_id == sid,
                PushDelivery.status == "pending", PushDelivery.error.like("presence_%"),
            ).values(available_at=now() + timedelta(seconds=BACKGROUND_SETTLE_SECONDS)))
        return {**public_status(presence), "applied": True}
