"""Payment outbox consumer. DB leases, not HTTP requests, own provisioning.

No network call runs in the payment transaction. Every cloud step can be
replayed after restart, except purchases: their durable intent is reconciled
against ECD before any further purchase is permitted.
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import timedelta

from sqlalchemy import exists, or_, select, update

from billing.service import lock_balance, now
from billing.subscriptions import active_subscription, utc
from core.config import get_config
from core.log import create_logger
from db.base import get_db_session
from db.models.billing import BillingSubscription, PaymentOrder
from db.models.cloud_desktop import CloudDesktop
from db.models.desktop_activation import DesktopActivation
from db.repository.cloud_desktop_repo import cloud_desktop_repo as desktops
from sandbox.entitlement import SandboxSubscriptionRequired, require_sandbox_subscription, subscription_sandbox_enabled
from sandbox.channel import ChannelVerificationStopped

log = create_logger("sandbox.activation")
LEASE_SECONDS = 90
POLL_SECONDS = 5


async def enqueue_paid_activation(db, order: PaymentOrder) -> None:
    """Caller holds the balance lock. Atomic with the verified paid receipt."""
    if not subscription_sandbox_enabled() or order.status != "paid":
        return
    sub = await active_subscription(db, order.workspace_id, now())
    if sub is None or sub.plan_id == "free":
        # A delayed callback may have bought a future term; schedule it, never
        # grant early access. Plain top-ups by free accounts grant no desktop.
        sub = await db.scalar(select(BillingSubscription).where(
            BillingSubscription.order_id == order.id,
            BillingSubscription.plan_id != "free",
            BillingSubscription.ends_at > now(),
        ))
        if sub is None:
            return
    row = await db.get(DesktopActivation, order.workspace_id)
    at = now()
    if row is None:
        row = DesktopActivation(workspace_id=order.workspace_id, state="queued", step="queued",
            attempts=0, next_run_at=at, created_at=at, updated_at=at)
        db.add(row)
    row.request_id = order.id
    row.user_id = order.user_id
    row.next_run_at = max(at, utc(sub.starts_at))
    row.updated_at = at
    # Do not invalidate a worker's lease or erase an unresolved purchase.
    if not row.lease_until or utc(row.lease_until) <= at:
        row.state = "queued"
        row.step = "queued"
        row.error = None


async def activation_status(workspace_id: str) -> dict:
    """Read-only: opening a page must never buy/start/adopt a desktop."""
    async with get_db_session() as db:
        sub = await active_subscription(db, workspace_id, now())
        entitled = sub is not None and sub.plan_id != "free"
        job = await db.get(DesktopActivation, workspace_id)
        record = await db.scalar(select(CloudDesktop).where(
            CloudDesktop.workspace_id == workspace_id, CloudDesktop.is_deleted.is_(False),
        ))
        state = "subscription_required"
        if entitled:
            state = "queued"
            if job and job.state in {"retrying", "needs_attention"}:
                state = job.state
            elif record and record.status == "running" and record.tunnel_state == "up" and (
                job is None or job.state == "ready"
            ):
                state = "running"
            elif job:
                state = job.step if job.step in {"assigning", "creating", "starting", "connecting"} else "queued"
        return {
            "mode": "per_user", "state": state, "entitled": entitled,
            "subscription_ends_at": utc(sub.ends_at).isoformat() if entitled else None,
            "retained": record is not None,
            "desktopId": record.desktop_id if record else None,
            "channel": {"state": record.tunnel_state if record else "pending"},
            "activation": {
                "request_id": job.request_id, "state": job.state, "step": job.step,
                "attempts": job.attempts, "error": job.error,
                "updated_at": utc(job.updated_at).isoformat(),
                "next_retry_at": utc(job.next_run_at).isoformat(),
                "can_retry": entitled and job.state == "retrying",
            } if job else None,
        }


async def retry_activation(workspace_id: str) -> dict:
    await require_sandbox_subscription(workspace_id)
    async with get_db_session() as db:
        await lock_balance(db, workspace_id)
        sub = await active_subscription(db, workspace_id, now())
        if sub is not None:
            order = await db.get(PaymentOrder, sub.order_id)
            if order is not None:
                row = await db.get(DesktopActivation, workspace_id)
                if row is None:
                    await enqueue_paid_activation(db, order)
                elif not row.lease_until or utc(row.lease_until) <= now():
                    # Retrying never clears purchase intent or buys a new box.
                    row.next_run_at = now()
    return await activation_status(workspace_id)


class LeaseLost(RuntimeError):
    pass


class PurchaseUncertain(RuntimeError):
    pass


class DesktopActivationService:
    def __init__(self):
        self._loop: asyncio.Task | None = None
        self._workers: set[asyncio.Task] = set()

    def start(self):
        if subscription_sandbox_enabled() and (self._loop is None or self._loop.done()):
            self._loop = asyncio.create_task(self._run(), name="desktop-activation-sweep")

    async def stop(self):
        tasks = [*self._workers, *([self._loop] if self._loop else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._loop = None

    async def _run(self):
        while True:
            try:
                await self.backfill()
            except Exception:
                log.exception("Desktop activation backfill failed; existing tasks will still run")
            try:
                for workspace_id in await self.due(limit=max(0, 4 - len(self._workers))):
                    task = asyncio.create_task(self.process(workspace_id))
                    self._workers.add(task)
                    task.add_done_callback(self._workers.discard)
            except Exception:
                log.exception("Desktop activation sweep failed; retrying")
            await asyncio.sleep(POLL_SECONDS)

    async def backfill(self):
        """Recover pre-upgrade paid accounts; also suspend legacy free desktops."""
        async with get_db_session() as db:
            missing = ~exists(select(DesktopActivation.workspace_id).where(
                DesktopActivation.workspace_id == PaymentOrder.workspace_id))
            orders = (await db.scalars(select(PaymentOrder).join(
                BillingSubscription, BillingSubscription.order_id == PaymentOrder.id,
            ).where(missing, PaymentOrder.status == "paid", BillingSubscription.plan_id != "free",
                BillingSubscription.ends_at > now()).order_by(PaymentOrder.paid_at.desc()).limit(100))).all()
        for order in orders:
            # Do not hold multiple workspaces' balance locks in one transaction.
            async with get_db_session() as db:
                await lock_balance(db, order.workspace_id)
                if await db.get(DesktopActivation, order.workspace_id) is None:
                    await enqueue_paid_activation(db, order)
                    await db.flush()
        async with get_db_session() as db:
            records = (await db.scalars(select(CloudDesktop).where(
                CloudDesktop.workspace_id.is_not(None), CloudDesktop.is_deleted.is_(False),
                ~exists(select(DesktopActivation.workspace_id).where(
                    DesktopActivation.workspace_id == CloudDesktop.workspace_id)),
            ).limit(100))).all()
        for record in records:
            async with get_db_session() as db:
                await lock_balance(db, record.workspace_id)
                if await db.get(DesktopActivation, record.workspace_id) is None:
                    at = now()
                    db.add(DesktopActivation(workspace_id=record.workspace_id, user_id=record.user_id,
                        state="queued", step="queued", attempts=0, next_run_at=at,
                        created_at=at, updated_at=at))
                    await db.flush()

    async def due(self, limit: int = 4) -> list[str]:
        if limit <= 0:
            return []
        async with get_db_session() as db:
            return list(await db.scalars(select(DesktopActivation.workspace_id).where(
                DesktopActivation.next_run_at <= now(),
                or_(DesktopActivation.lease_until.is_(None), DesktopActivation.lease_until <= now()),
            ).order_by(DesktopActivation.next_run_at).limit(limit)))

    async def _save(self, workspace_id: str, token: str, **values):
        async with get_db_session() as db:
            result = await db.execute(update(DesktopActivation).where(
                DesktopActivation.workspace_id == workspace_id,
                DesktopActivation.lease_owner == token,
                DesktopActivation.lease_until > now(),
            ).values(**values, updated_at=now()))
            if result.rowcount != 1:
                raise LeaseLost(workspace_id)

    async def _heartbeat(self, workspace_id, token, operation):
        try:
            while True:
                await asyncio.sleep(15)
                await self._save(workspace_id, token, lease_until=now() + timedelta(seconds=LEASE_SECONDS))
        except Exception:
            operation.cancel()  # Do not continue cloud mutations after losing ownership.

    async def process(self, workspace_id: str) -> bool:
        token = secrets.token_hex(16)
        heartbeat = None
        claimed = False
        try:
            async with get_db_session() as db:
                result = await db.execute(update(DesktopActivation).where(
                    DesktopActivation.workspace_id == workspace_id,
                    DesktopActivation.next_run_at <= now(),
                    or_(DesktopActivation.lease_until.is_(None), DesktopActivation.lease_until <= now()),
                ).values(lease_owner=token, lease_until=now() + timedelta(seconds=LEASE_SECONDS),
                    attempts=DesktopActivation.attempts + 1, updated_at=now()))
                claimed = result.rowcount == 1
            if not claimed:
                return False
            heartbeat = asyncio.create_task(self._heartbeat(workspace_id, token, asyncio.current_task()))
            await self._advance(workspace_id, token)
            return True
        except asyncio.CancelledError:
            raise
        except LeaseLost:
            log.warning("Desktop activation lease lost for %s", workspace_id)
        except ChannelVerificationStopped:
            # A scheduled retry is not a new enrollment authorization.
            try:
                await self._save(workspace_id, token, state="needs_attention", step="connecting",
                    error="云电脑通道已撤销或归属已改变，当前连接尝试已停止，请检查桌面分配。",
                    next_run_at=now() + timedelta(seconds=300))
            except LeaseLost:
                pass
        except SandboxSubscriptionRequired:
            try:
                await self._save(workspace_id, token, state="queued", step="suspending", next_run_at=now())
            except LeaseLost:
                pass  # The next lease owner will observe the same expired term.
        except Exception as exc:
            log.exception("Desktop activation failed for %s", workspace_id)
            if claimed:
                try:
                    async with get_db_session() as db:
                        row = await db.get(DesktopActivation, workspace_id)
                        attempt = row.attempts
                    uncertain = isinstance(exc, PurchaseUncertain)
                    await self._save(workspace_id, token,
                        state="needs_attention" if uncertain else "retrying",
                        error=("云电脑购买结果待核实，正在查找原机器；不会重复购买。请联系管理员核实云订单。"
                            if uncertain else "云电脑暂未准备好，服务器会自动重试；普通对话不受影响。"),
                        next_run_at=now() + timedelta(seconds=min(300, 5 * 2 ** min(attempt, 6))))
                except Exception:
                    log.exception("Could not persist desktop activation retry")
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if claimed:
                # A crash instead leaves a bounded lease; another process takes it.
                try:
                    async with get_db_session() as db:
                        await db.execute(update(DesktopActivation).where(
                            DesktopActivation.workspace_id == workspace_id,
                            DesktopActivation.lease_owner == token,
                        ).values(lease_owner=None, lease_until=None))
                except Exception:
                    log.exception("Could not release desktop lease; it will expire")
        return False

    async def _advance(self, workspace_id: str, token: str):
        from sandbox import wuying_ecd as ecd
        from sandbox.channel import wuying_channel
        from sandbox.wuying_desktop_service import _parse_expired_time

        async def save(**values):
            await self._save(workspace_id, token, **values)

        async def stage(step):
            await require_sandbox_subscription(workspace_id)
            await save(state="working", step=step, error=None)

        async with get_db_session() as db:
            sub = await active_subscription(db, workspace_id, now())
            job = await db.get(DesktopActivation, workspace_id)
        record = await desktops.get_for_workspace(workspace_id)
        if sub is None or sub.plan_id == "free":
            # Suspension retains the original instance, disk and credentials.
            # It has no enrollment authority, even when the channel is revoked.
            async def suspension_authority():
                async with get_db_session() as db:
                    owner = await db.scalar(select(DesktopActivation.workspace_id).where(
                        DesktopActivation.workspace_id == workspace_id,
                        DesktopActivation.lease_owner == token,
                        DesktopActivation.lease_until > now()))
                    active = await active_subscription(db, workspace_id, now())
                    if owner is None or (active is not None and active.plan_id != "free"):
                        raise LeaseLost(workspace_id)

            attempt = await wuying_channel.maintain(record,
                authority_check=suspension_authority, reuse=True) if record else None

            async def suspend_save(*, desktop_fields=None, **values):
                # Serialize renewal/payment with both the original desktop CAS
                # and the suspended receipt. Never hold a SQL lock during IO.
                async with get_db_session() as db:
                    await lock_balance(db, workspace_id)
                    active = await active_subscription(db, workspace_id, now())
                    owner = await db.scalar(select(DesktopActivation.workspace_id).where(
                        DesktopActivation.workspace_id == workspace_id,
                        DesktopActivation.lease_owner == token,
                        DesktopActivation.lease_until > now()).with_for_update())
                    if owner is None or (active is not None and active.plan_id != "free"):
                        raise LeaseLost(workspace_id)
                    if attempt is not None:
                        await attempt.write(session=db, **(desktop_fields or {}))
                    result = await db.execute(update(DesktopActivation).where(
                        DesktopActivation.workspace_id == workspace_id,
                        DesktopActivation.lease_owner == token,
                        DesktopActivation.lease_until > now()).values(**values, updated_at=now()))
                    if result.rowcount != 1:
                        raise LeaseLost(workspace_id)

            await suspension_authority()
            needs_suspension = job.step != "suspended"
            if attempt and not record.get("desktop_id") and job.purchase_kind == "create":
                remote = await attempt.call(ecd.list_desktops, user_id=workspace_id)
                if len(remote) == 1:
                    await suspend_save(desktop_fields={"desktop_id": remote[0]["desktop_id"],
                        "end_user_id": ecd.eu_id_for(workspace_id)}, purchase_kind=None, purchase_started_at=None)
                    needs_suspension = True
            if attempt and attempt.record.get("desktop_id") and needs_suspension:
                await suspend_save(state="working", step="suspending")
                desktop_id = attempt.record["desktop_id"]
                info = await attempt.call(ecd.describe_desktop, desktop_id)
                if info and info["status"] != "Running":
                    # Never start a free user's machine merely to remove access.
                    await suspend_save(state="suspended", step="suspending", error=None,
                        next_run_at=now() + timedelta(seconds=30))
                    return
                if info:
                    end_user = await attempt.call(ecd.verify_ownership, desktop_id, workspace_id)
                    await attempt.call(ecd.modify_entitlement, desktop_id, [])
                    await attempt.call(ecd.disconnect_desktop_sessions, desktop_id, end_user)
            await suspend_save(state="suspended", step="suspended", error=None,
                next_run_at=now() + timedelta(seconds=30))
            return

        expiry = record.get("expires_at") if record else None
        if job.state == "ready" and record and record.get("status") == "running" and (
            record.get("tunnel_state") == "up"
        ) and (not expiry or utc(expiry) > now() + timedelta(hours=24)):
            await save(attempts=0, next_run_at=min(utc(sub.ends_at), now() + timedelta(seconds=30)))
            return

        async def authority():
            async with get_db_session() as db:
                owner = await db.scalar(select(DesktopActivation.workspace_id).where(
                    DesktopActivation.workspace_id == workspace_id,
                    DesktopActivation.lease_owner == token,
                    DesktopActivation.lease_until > now()))
                if owner is None:
                    raise LeaseLost(workspace_id)
            await require_sandbox_subscription(workspace_id)

        async def allocate(*, adopted=None):
            # Match payment's balance -> job lock order. No cloud IO runs
            # under these locks, and an old inventory response cannot allocate
            # a desktop/grant after losing its paid job authority.
            async with get_db_session() as db:
                await lock_balance(db, workspace_id)
                active = await active_subscription(db, workspace_id, now())
                if active is None or active.plan_id == "free":
                    raise SandboxSubscriptionRequired()
                owner = await db.scalar(select(DesktopActivation).where(
                    DesktopActivation.workspace_id == workspace_id,
                    DesktopActivation.lease_owner == token,
                    DesktopActivation.lease_until > now()).with_for_update())
                if owner is None:
                    raise LeaseLost(workspace_id)
                if adopted is not None:
                    return await desktops.create(workspace_id, get_config().wuying_region_id,
                        user_id=job.user_id, session=db, **adopted)
                config = get_config()
                allocated = None
                if config.pool_enabled and config.pool_assign_on_provision:
                    allocated = await desktops.claim_prewarm(workspace_id, job.user_id,
                        usable_until=now() + timedelta(days=3), session=db)
                if allocated is None:
                    allocated = await desktops.create(workspace_id, config.wuying_region_id,
                        user_id=job.user_id, charge_type="PrePaid", session=db)
                return allocated

        await stage("assigning")
        attempt = await wuying_channel.begin(record, authority_check=authority) if record else None
        if record is None or not record.get("desktop_id"):
            # Errors are deliberately not treated as an empty remote inventory.
            remote = (await attempt.call(ecd.list_desktops, user_id=workspace_id) if attempt
                      else await ecd.list_desktops(user_id=workspace_id))
            await authority()
            remote = [item for item in remote if item["status"] not in {"Deleted", "Deleting"}]
            if len(remote) > 1:
                raise PurchaseUncertain("multiple tagged desktops require reconciliation")
            if remote:
                found = remote[0]
                values = dict(desktop_id=found["desktop_id"], end_user_id=ecd.eu_id_for(workspace_id),
                    charge_type=found.get("charge_type"), status="starting",
                    expires_at=_parse_expired_time(found.get("expired_time")))
                if record:
                    record = await attempt.write(**values)
                else:
                    record = await allocate(adopted=values)
                    attempt = await wuying_channel.begin(record, authority_check=authority)
                await save(purchase_kind=None, purchase_started_at=None, purchase_baseline=None)
            elif job.purchase_kind:
                raise PurchaseUncertain("CreateDesktops outcome not yet visible")

        if record is None:
            record = await allocate()
            attempt = await wuying_channel.begin(record, authority_check=authority)

        if not record.get("desktop_id"):
            await stage("creating")

            async def before_purchase():
                await attempt.current()
                await save(purchase_kind="create", purchase_started_at=now())

            desktop_id = await attempt.call(ecd.create_desktop, workspace_id, monthly=True, before_submit=before_purchase)
            record = await attempt.write(desktop_id=desktop_id,
                end_user_id=ecd.eu_id_for(workspace_id), status="starting")
            await save(purchase_kind=None, purchase_started_at=None, purchase_baseline=None)

        desktop_id = record["desktop_id"]
        info = await attempt.call(ecd.describe_desktop, desktop_id)
        if info is None:
            # Retained machines must not be silently replaced (nor user data lost).
            raise PurchaseUncertain("retained desktop missing from ECD")
        owned_end_user = None
        if record.get("pool_state") != "assigning":
            # Verify before renewing, starting, or changing cloud authorization.
            owned_end_user = await attempt.call(ecd.verify_ownership, desktop_id, workspace_id)
        expiry = _parse_expired_time(info.get("expired_time"))
        await attempt.write(charge_type=info.get("charge_type"), expires_at=expiry)
        if job.purchase_kind == "renew":
            if expiry and job.purchase_baseline and expiry > utc(job.purchase_baseline):
                await save(purchase_kind=None, purchase_started_at=None, purchase_baseline=None)
            else:
                raise PurchaseUncertain("renewal outcome not yet visible")
        # Maintain monthly cloud capacity only while a paid term needs it.
        # Never enable Alibaba auto-renew (it would continue charging free users).
        if info.get("charge_type") == "PrePaid" and expiry and (
            expiry <= now() + timedelta(hours=24) and utc(sub.ends_at) > expiry
        ):
            await stage("starting")
            await save(purchase_kind="renew", purchase_started_at=now(), purchase_baseline=expiry)
            await attempt.call(ecd.renew_desktop, desktop_id, 1, "Month", auto_pay=True, auto_renew=False)
            refreshed = await attempt.call(ecd.describe_desktop, desktop_id)
            renewed_expiry = _parse_expired_time((refreshed or {}).get("expired_time"))
            if not renewed_expiry or renewed_expiry <= expiry:
                raise PurchaseUncertain("renewal submitted; awaiting new expiry")
            await save(purchase_kind=None, purchase_started_at=None, purchase_baseline=None)
            await attempt.write(expires_at=renewed_expiry)

        # Retained/tag-adopted desktops may predate workspace-based EndUser IDs.
        # Keep the verified identity used by the ticket API on renewal.
        end_user = owned_end_user
        if not end_user:
            end_user, _ = await attempt.call(ecd.ensure_end_user, workspace_id)
        await stage("starting")
        if info["status"] == "Stopped":
            await attempt.call(ecd.start_desktop, desktop_id)
        await attempt.call(ecd.wait_desktop_ready, desktop_id)
        await require_sandbox_subscription(workspace_id)
        await attempt.call(ecd.modify_entitlement, desktop_id, [end_user])
        if record.get("pool_state") == "assigning":
            await attempt.call(ecd.tag_desktop, desktop_id, {
                ecd.TAG_USER: workspace_id, ecd.TAG_WORKSPACE: workspace_id,
                ecd.TAG_EU: end_user, ecd.TAG_POOL: "assigned",
            })
        await stage("connecting")
        if not record.get("action_api_key_ciphertext") or record.get("tunnel_state") != "up" or record.get("pool_state") == "assigning":
            await attempt.install(rotate_key=record.get("pool_state") == "assigning")
        await attempt.verify()
        await require_sandbox_subscription(workspace_id)
        # Desktop completion and the leased activation receipt commit together.
        # Either fence failing rolls both writes back; never mark a successor ready.
        async with get_db_session() as db:
            await attempt.write(session=db, states=("up",), status="running", pool_state="assigned",
                end_user_id=end_user, error=None, assigned_at=record.get("assigned_at") or now())
            result = await db.execute(update(DesktopActivation).where(
                DesktopActivation.workspace_id == workspace_id,
                DesktopActivation.lease_owner == token,
                DesktopActivation.lease_until > now(),
            ).values(state="ready", step="ready", error=None, updated_at=now(),
                next_run_at=min(utc(sub.ends_at), now() + timedelta(seconds=30))))
            if result.rowcount != 1:
                raise LeaseLost(workspace_id)


desktop_activation_service = DesktopActivationService()
