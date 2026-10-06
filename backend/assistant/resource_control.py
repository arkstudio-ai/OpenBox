"""Durable resource admission shared by effects from every execution Session.

This module does not grant human control. Remote fencing, direct-channel
coverage and client revocation must prove exclusive access before an adapter
may expose that transition. No timeout or empty local queue proves drainage.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import re

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


def closed_transition_target(row, user_id):
    """An internal handoff can retire this actor's epoch, never grant input."""
    if row.epoch >= 2**31 - 1:
        raise AssistantError(409, "RESOURCE_EPOCH_EXHAUSTED", "Resource control requires inspection")
    if row.owner_kind == "automation" and row.owner_id == row.workspace_id:
        return ResourceFence(row.id, row.epoch + 1, "human", user_id)
    if row.owner_kind == "human" and row.owner_id == user_id:
        return ResourceFence(row.id, row.epoch + 1, "automation", row.workspace_id)
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
    if row.provider == "private_wuying_v1":
        await private_runtime_binding_locked(db, session, resource=row)
        if require_open and (row.remote_journal_id is None or row.remote_status is None):
            raise unavailable()
    else:
        # Every other provider, including retained rows of retired ones, must
        # still name a current SQL-assigned desktop or it admits nothing.
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


async def private_runtime_binding_locked(db, session, *, runtime_route=None, resource=None, lock=False):
    """Check the original guest/SQL attempt; a shared ECD needs no CloudDesktop.

    This is SQL identity validation, not a guest isolation proof. The private
    client independently rechecks the original configured route and guest at
    every transport boundary. No latest-row lookup can replace a supplied
    route or an already accepted resource fence.
    """
    from db.models.private_runtime import PrivateRuntimeBinding
    from sandbox.private_runtime import _scope
    if await _scope(db, session.id, session.user_id, session.workspace_id) is None:
        raise unavailable()
    if runtime_route is not None:
        binding_id = runtime_route.binding_id
    elif resource is not None:
        binding_id = resource.physical_id.split(":", 1)[0]
    else:
        raise unavailable()
    statement = select(PrivateRuntimeBinding).where(PrivateRuntimeBinding.id == binding_id).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update(key_share=True)
    binding = await db.scalar(statement)
    if (binding is None or binding.provider != "private_wuying_v1" or binding.kind != "sandbox"
            or binding.status != "ready" or binding.provision_phase != "ready"
            or binding.isolation_mode != "guest_uid_mount" or binding.workspace_id != session.workspace_id
            or binding.actor_user_id != session.user_id or not isinstance(binding.physical_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", binding.physical_digest)):
        raise unavailable()
    physical = f"{binding.id}:{binding.revision}:{binding.physical_digest}"
    if resource is not None and (resource.provider != "private_wuying_v1"
            or resource.resource_type != "actor_runtime" or resource.physical_id != physical
            or resource.desktop_record_id is not None):
        raise unavailable()
    if runtime_route is not None:
        guest = binding.provider_identity.get("guest_binding", {})
        endpoint = binding.provider_identity.get("endpoint", {})
        if (runtime_route.provider != binding.provider or runtime_route.kind != binding.kind
                or runtime_route.workspace_id != binding.workspace_id or runtime_route.actor_user_id != binding.actor_user_id
                or runtime_route.attempt_id != binding.attempt_id or runtime_route.revision != binding.revision
                or runtime_route.provider_identity != binding.provider_identity
                or runtime_route.isolation_mode != binding.isolation_mode
                or runtime_route.guest_binding_id != guest.get("id")
                or runtime_route.guest_attempt_id != guest.get("attempt_id")
                or runtime_route.scope_id != guest.get("scope_id")
                or runtime_route.desktop_id != endpoint.get("desktop_id")
                or runtime_route.region_id != endpoint.get("region_id")
                or sha256(runtime_route.api_key.encode()).hexdigest() != binding.api_key_hash
                or runtime_route.base_url != endpoint.get("base_url", "").rstrip("/") + "/private-runtime/" + str(guest.get("id"))):
            raise unavailable()
    return binding, physical


async def enroll_private_runtime_locked(db, session, runtime_route):
    await actor(db, session.user_id, session.workspace_id)
    # Enrollment serializes on the existing original actor binding before it
    # creates/locks the resource row. Validation never takes a binding write
    # lock after a resource lock, so two Sessions cannot invert this order.
    _, physical = await private_runtime_binding_locked(db, session, runtime_route=runtime_route, lock=True)
    resource_id = sha256(f"private_wuying_v1:actor_runtime:{physical}".encode()).hexdigest()
    row = await locked(db, resource_id)
    if row is None:
        stamp = await clock(db)
        row = ResourceControlLease(id=resource_id, resource_type="actor_runtime", provider="private_wuying_v1",
            physical_id=physical, workspace_id=session.workspace_id, desktop_record_id=None,
            owner_kind="automation", owner_id=session.workspace_id, epoch=1, status="active", admission_state="open",
            expires_at=None, created_at=stamp, updated_at=stamp)
        db.add(row)
        await db.flush()
    await private_runtime_binding_locked(db, session, runtime_route=runtime_route, resource=row)
    return row


async def capture_desktop_context_locked(db, session, desktop_id, *, images=None, run_fence=None, runtime_route=None):
    """Freeze SQL resource identity with the exact provider request; no remote IO.

    Runtime-only callers get a control snapshot. Provider callers also bind
    the actual resolved image receipt. Closed resources can be discussed,
    but cannot admit tools.
    """
    if runtime_route is not None:
        if runtime_route.desktop_id != desktop_id:
            raise unavailable()
        row = await enroll_private_runtime_locked(db, session, runtime_route)
    else:
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
