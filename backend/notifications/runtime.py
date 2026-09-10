"""Lease-based push worker. Provider acceptance is not proof of device arrival."""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select

from auth.mobile import cancel_deliveries, mobile_transaction, now, utc
from core.log import create_logger
from db.models.push import PushDelivery, PushMessage
from notifications.providers import SendResult
from notifications.store import can_receive, valid_binding
from notifications import presence
from notifications.events import guard_valid

log = create_logger("push")


@dataclass(frozen=True)
class ClaimedDelivery:
    id: str
    lease_id: str
    provider: str
    token: str
    environment: str
    payload: dict
    ttl: int


async def claim_delivery(enabled):
    async with mobile_transaction() as db:
        rows = list((await db.scalars(select(PushDelivery).where(or_(
            and_(PushDelivery.status == "pending", PushDelivery.available_at <= now()),
            and_(PushDelivery.status == "sending", PushDelivery.lease_until <= now()),
        )).order_by(PushDelivery.available_at).limit(50).with_for_update(skip_locked=True))).all())
        for row in rows:
            message = await db.get(PushMessage, row.message_id)
            device = await valid_binding(db, row)
            if (not device or not message or utc(message.expires_at) <= now()
                    or not await can_receive(db, row.user_id, message.workspace_id, message.payload.get("sessionId"))
                    or not await guard_valid(db, message)):
                row.status, row.error = "cancelled", "expired_or_revoked"
                row.lease_id, row.lease_until = None, None
                continue
            decision = await presence.for_delivery(db, row)
            if decision.action != "allow":
                row.lease_id, row.lease_until = None, None
                if decision.action == "suppress":
                    row.status, row.error = "cancelled", "app_foreground"
                else:
                    row.status, row.available_at, row.error = "pending", decision.until, "presence_" + decision.state
                continue
            if row.attempts >= 8:
                row.status, row.error = "failed", "retry_exhausted"
                continue
            if device.provider not in enabled:
                # Don't block ready providers behind an unconfigured channel.
                row.status, row.available_at = "pending", now() + timedelta(seconds=60)
                continue
            row.status, row.lease_id, row.error = "sending", uuid4().hex, None
            row.lease_until = now() + timedelta(seconds=60)
            row.attempts += 1
            return ClaimedDelivery(row.id, row.lease_id, device.provider, device.token,
                                   device.apns_environment, {k: v for k, v in message.payload.items() if k != "guard"},
                                   max(1, int((utc(message.expires_at) - now()).total_seconds())))
    return None


async def still_sendable(claim):
    """Recheck after claim, immediately before provider I/O. Native foreground
    suppression covers the unavoidable race once the provider accepts a push.
    """
    async with mobile_transaction() as db:
        row = await db.get(PushDelivery, claim.id)
        if not row or row.status != "sending" or row.lease_id != claim.lease_id:
            return False
        message = await db.get(PushMessage, row.message_id)
        decision = await presence.for_delivery(db, row)
        if (not await valid_binding(db, row) or not message or utc(message.expires_at) <= now()
                or not await can_receive(db, row.user_id, message.workspace_id, message.payload.get("sessionId"))
                or not await guard_valid(db, message) or decision.action != "allow"):
            row.status, row.error = "cancelled", "state_changed_before_send"
            row.lease_id, row.lease_until = None, None
            return False
        return True


async def settle_delivery(claim: ClaimedDelivery, result: SendResult):
    async with mobile_transaction() as db:
        row = await db.get(PushDelivery, claim.id)
        if not row or row.status != "sending" or row.lease_id != claim.lease_id:
            return
        device = await valid_binding(db, row)
        if not device:
            row.status, row.error = "cancelled", "binding_revoked"
        elif result.ok:
            row.status, row.provider_message_id, row.error = "accepted", result.message_id, None
        elif result.invalid_device:
            device.enabled = False
            await cancel_deliveries(db, row.user_id, binding_id=row.binding_id)
            row.status, row.error = "failed", result.error
        elif result.retry and row.attempts < 8:
            row.status, row.error = "pending", result.error
            row.available_at = now() + timedelta(seconds=min(300, 2 ** row.attempts))
        else:
            row.status, row.error = "failed", result.error
        row.lease_id, row.lease_until = None, None


class PushWorker:
    def __init__(self, providers):
        self.providers = providers
        self.task = None

    def start(self):
        if self.providers.enabled and self.task is None:
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        await self.providers.close()

    async def tick(self):
        for _ in range(20):
            claim = await claim_delivery(self.providers.enabled)
            if not claim:
                break
            if not await still_sendable(claim):
                continue
            result = await self.providers.send(claim.provider, claim.token, claim.environment, claim.payload, ttl=claim.ttl)
            await settle_delivery(claim, result)

    async def _run(self):
        while True:
            try:
                await self.tick()
            except Exception as error:
                log.warning("Push worker deferred: %s", type(error).__name__)
            await asyncio.sleep(1)
