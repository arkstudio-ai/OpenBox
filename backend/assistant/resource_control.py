"""Durable resource admission shared by effects from every execution Session.

This module does not grant human control. Remote fencing, direct-channel
coverage and client revocation must prove exclusive access before an adapter
may expose that transition. No timeout or empty local queue proves drainage.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256

from sqlalchemy import select

from assistant.policy import AssistantError, require_membership
from db.base import get_db_session
from db.models.cloud_desktop import CloudDesktop
from db.models.external_effect import ExternalEffect
from db.models.resource_control import ResourceControlLease
from db.models.session import Session
from db.models.user import User
from session.internal_parts import begin_session_write


@dataclass(frozen=True)
class ResourceFence:
    resource_id: str
    epoch: int
    owner_kind: str
    owner_id: str

    def __post_init__(self):
        if (not isinstance(self.resource_id, str) or not 1 <= len(self.resource_id) <= 64
                or type(self.epoch) is not int or self.epoch < 1
                or self.owner_kind not in {"automation", "human"}
                or not isinstance(self.owner_id, str) or not 1 <= len(self.owner_id) <= 64):
            raise ValueError("Invalid resource control fence")


def fence_for(row):
    return ResourceFence(row.id, row.epoch, row.owner_kind, row.owner_id)


def unavailable():
    return AssistantError(423, "RESOURCE_CONTROL_HELD", "This resource is held or its control generation changed")


async def assert_native_ticket_unmanaged(*, region_id, desktop_id):
    """The legacy native SDK cannot receive credentials for a managed resource.

    Native tickets have no revocable owner/epoch token. Even an open automation
    lease cannot authorize this independent human-input channel. Match the
    persistent physical identity, including old/deleted SQL assignments, and
    read again at every ticket poll/return boundary. This refusal is not atomic
    with native credential use or an enrollment after the final read. Already
    issued tickets and connected clients still need a revocation protocol;
    exclusive takeover therefore remains unavailable.
    """
    if (not isinstance(region_id, str) or not region_id
            or not isinstance(desktop_id, str) or not desktop_id):
        raise unavailable()
    async with get_db_session() as db:
        resource_id = await db.scalar(select(ResourceControlLease.id).where(
            ResourceControlLease.provider == "wuying",
            ResourceControlLease.resource_type == "desktop",
            ResourceControlLease.physical_id == f"{region_id}:{desktop_id}"))
        if resource_id is not None:
            raise unavailable()


async def clock(db):
    from agent.effect_ledger import _read_database_now
    return await _read_database_now(db)


def aware(value):
    from question.runtime import utc
    return utc(value)


async def locked(db, resource_id):
    # NO KEY UPDATE retains FK admission while serializing owner/epoch changes.
    return await db.scalar(select(ResourceControlLease).where(ResourceControlLease.id == resource_id)
        .with_for_update(key_share=True).execution_options(populate_existing=True))


async def actor(db, user_id, workspace_id):
    await require_membership(db, user_id, workspace_id)
    if not await db.scalar(select(User.id).where(User.id == user_id,
            User.is_active.is_(True), User.is_deleted.is_(False))):
        raise unavailable()


async def validate_locked(db, fence, *, user_id, session_id, require_open=True):
    if not isinstance(fence, ResourceFence):
        raise unavailable()
    row = await locked(db, fence.resource_id)
    session = await db.get(Session, session_id)
    if (row is None or session is None or session.is_deleted or session.user_id != user_id
            or row.workspace_id != session.workspace_id):
        raise unavailable()
    await actor(db, user_id, row.workspace_id)
    desktop = await db.get(CloudDesktop, row.desktop_record_id) if row.desktop_record_id else None
    if (desktop is None or desktop.is_deleted or desktop.workspace_id != row.workspace_id
            or f"{desktop.region_id}:{desktop.desktop_id}" != row.physical_id
            or desktop.pool_state != "assigned"):
        raise unavailable()
    if require_open and (fence_for(row) != fence or row.owner_kind != "automation" or row.owner_id != row.workspace_id
            or row.status != "active" or row.admission_state != "open"
            or row.expires_at is not None and aware(row.expires_at) <= await clock(db)):
        raise unavailable()
    if require_open and row.remote_journal_id is not None:
        observed = row.remote_status or {}
        control = observed.get("control") or {}
        if (observed.get("protocol") != "resource_admission_v2"
                or observed.get("journal_id") != row.remote_journal_id
                or control.get("admission") != "open"
                or any(control.get(key) != value for key, value in {
                    "resource_id": row.id, "epoch": row.epoch,
                    "owner_kind": row.owner_kind, "owner_id": row.owner_id}.items())):
            raise unavailable()
    return row


async def enroll_desktop(*, desktop_id, workspace_id, user_id):
    """Bind only a current SQL-owned physical desktop, never a supplied URL."""
    async with get_db_session() as db:
        await begin_session_write(db)
        row = await enroll_desktop_locked(db, desktop_id=desktop_id,
            workspace_id=workspace_id, user_id=user_id)
        return fence_for(row)


async def enroll_desktop_locked(db, *, desktop_id, workspace_id, user_id):
    await actor(db, user_id, workspace_id)
    desktop = await db.scalar(select(CloudDesktop).where(CloudDesktop.desktop_id == desktop_id,
        CloudDesktop.workspace_id == workspace_id, CloudDesktop.is_deleted.is_(False),
        CloudDesktop.pool_state == "assigned").with_for_update(key_share=True))
    if desktop is None:
        raise unavailable()
    physical = f"{desktop.region_id}:{desktop.desktop_id}"
    resource_id = sha256(f"wuying:desktop:{physical}".encode()).hexdigest()
    row = await locked(db, resource_id)
    if row is None:
        stamp = await clock(db)
        row = ResourceControlLease(id=resource_id, resource_type="desktop", provider="wuying",
            physical_id=physical, workspace_id=workspace_id, desktop_record_id=desktop.id,
            owner_kind="automation", owner_id=workspace_id, epoch=1, status="active",
            admission_state="open", expires_at=None, last_observation_ref=None,
            created_at=stamp, updated_at=stamp)
        db.add(row)
        await db.flush()
    if row.workspace_id != workspace_id or row.desktop_record_id != desktop.id:
        # Reassigning a pooled machine needs an explicit, drained control
        # transition; possession of its newer SQL assignment is not enough.
        raise unavailable()
    return row


async def capture_desktop_context_locked(db, session, desktop_id, *, images=None, run_fence=None):
    """Freeze SQL resource identity with the exact provider request; no remote IO.

    Runtime-only callers get a control snapshot. Provider callers also bind
    the actual resolved image receipt. Closed resources can be discussed,
    but cannot admit tools.
    """
    row = await enroll_desktop_locked(db, desktop_id=desktop_id,
        workspace_id=session.workspace_id, user_id=session.user_id)
    context = {"version": 1, "desktop_id": desktop_id, "fence": asdict(fence_for(row)),
               "journal_id": row.remote_journal_id}
    if images is not None:
        from assistant.resource_observations import for_request_locked
        context.update(version=2, observation=await for_request_locked(db, row, session, images, run_fence))
    return context


async def close_admission_locked(db, fence, *, user_id):
    """The caller owns its control Command transaction; never await remote I/O."""
    row = await locked(db, fence.resource_id)
    if row is None:
        raise unavailable()
    await actor(db, user_id, row.workspace_id)
    if fence_for(row) != fence:
        raise unavailable()
    row.admission_state = "closed"
    row.status = "draining"
    row.updated_at = await clock(db)
    return row


async def drain_status_locked(db, row):
    # Failed pre-send operations never dispatched. All other ambiguous states
    # survive expired worker leases, stopped Drivers and process restarts.
    effects = list((await db.scalars(select(ExternalEffect).where(
        ExternalEffect.resource_id == row.id, ExternalEffect.submitting_at.is_not(None),
        ExternalEffect.state.not_in(("succeeded", "failed"))).order_by(ExternalEffect.created_at))).all())
    return {"resource_id": row.id, "epoch": row.epoch, "admission_state": row.admission_state,
        "tracked_operations_drained": not effects,
        "blocking_effect_ids": [effect.id for effect in effects],
        "remote_exclusivity_verified": False}


async def validate_effect_locked(db, effect, *, consume_observation=False):
    if effect.resource_id is None:
        return
    row = await validate_locked(db, ResourceFence(effect.resource_id, effect.resource_epoch,
        effect.resource_owner_kind, effect.resource_owner_id),
        user_id=effect.tenant_id, session_id=effect.session_id)
    if ("resource_journal_id" in effect.safe_context
            and effect.safe_context["resource_journal_id"] != row.remote_journal_id):
        raise unavailable()
    from assistant.resource_observations import guard_effect_locked
    await guard_effect_locked(db, effect, row, consume=consume_observation)


async def expire_leases(limit=100):
    """Expiry closes admission durably; it never changes ownership to Agent."""
    async with get_db_session() as db:
        await begin_session_write(db)
        stamp = await clock(db)
        rows = list((await db.scalars(select(ResourceControlLease).where(
            ResourceControlLease.expires_at <= stamp, ResourceControlLease.status != "hold")
            .order_by(ResourceControlLease.expires_at, ResourceControlLease.id).limit(limit)
            .with_for_update(skip_locked=True, key_share=True))).all())
        for row in rows:
            row.status, row.admission_state, row.updated_at = "hold", "closed", stamp
        return len(rows)


async def heartbeat(*, fence, user_id, ttl_seconds=60):
    from datetime import timedelta
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 120:
        raise ValueError("Invalid resource heartbeat duration")
    async with get_db_session() as db:
        await begin_session_write(db)
        row = await locked(db, fence.resource_id)
        if row is None:
            raise unavailable()
        await actor(db, user_id, row.workspace_id)
        if fence_for(row) != fence or row.owner_kind != "human" or row.owner_id != user_id:
            raise unavailable()
        stamp = await clock(db)
        if row.expires_at is None or aware(row.expires_at) <= stamp:
            row.status, row.admission_state, row.updated_at = "hold", "closed", stamp
        elif row.status != "active" or row.admission_state != "open":
            raise unavailable()
        else:
            row.expires_at, row.updated_at = stamp + timedelta(seconds=ttl_seconds), stamp
        return {"epoch": row.epoch, "status": row.status, "admission_state": row.admission_state,
            "expires_at": row.expires_at.isoformat()}
