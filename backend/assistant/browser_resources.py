"""Actor-bound control of a separate, finite browser-profile supervisor.

The SQL lease closes before remote I/O. The original supervisor journal must
then confirm drainage before an epoch can be granted. A timeout leaves the
command pending and the local lease closed; it never licenses a replacement.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import asyncio
import re

import httpx
from sqlalchemy import select

from assistant import resource_control as controls
from assistant.commands import _authority, command_digest
from assistant.policy import AssistantError, lock_actor
from core.identifier import generate_id
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.browser_resource import BrowserResourceBinding, BrowserResourceSession
from db.models.external_effect import ExternalEffect
from db.models.private_runtime import PrivateRuntimeBinding
from db.models.resource_control import ResourceControlLease
from db.models.session import Session
from sandbox.browser_resource_client import BrowserResourceClient, BrowserResourceError, PROTOCOL
from session.internal_parts import begin_session_write


PROVIDER = "private_browser_v1"
OPERATIONS = frozenset({"capture", "navigate", "back", "reload", "mouse", "key", "text", "wheel"})
ISOLATION_CHECKS = frozenset({
    "linux", "supervisor_uid_zero", "browser_uid_distinct", "browser_gid_distinct",
    "browser_capabilities_empty", "no_new_privileges", "supervisor_capabilities_bounded",
    "root_filesystem_readonly", "protected_storage", "private_pipe",
    "browser_cannot_read_journal", "browser_cannot_write_journal",
    "browser_cannot_read_supervisor_environment", "browser_cannot_open_control_pipe",
    "browser_cannot_signal_supervisor",
})


def held(code="BROWSER_CONTROL_HELD"):
    return AssistantError(423, code, "浏览器控制暂不可用，请核对当前控制权与未完成操作。")


def _identity(value):
    if (not isinstance(value, dict) or set(value) != {"resource_id", "profile_id", "journal_id", "runtime_id"}
            or any(not isinstance(item, str) or not re.fullmatch(
                r"[0-9a-f]{64}" if key == "resource_id" else r"[0-9a-f]{32}", item)
                for key, item in value.items())):
        raise held("BROWSER_IDENTITY_CHANGED")
    return dict(value)


def _control(payload, identity):
    if payload.get("protocol") != PROTOCOL or _identity(payload.get("identity")) != identity:
        raise held("BROWSER_IDENTITY_CHANGED")
    control = payload.get("control")
    if not isinstance(control, dict):
        raise held("BROWSER_PROTOCOL_UNAVAILABLE")
    try:
        fence = controls.ResourceFence(**control["fence"])
    except (KeyError, TypeError, ValueError) as exc:
        raise held("BROWSER_PROTOCOL_UNAVAILABLE") from exc
    if (fence.resource_id != identity["resource_id"]
            or control.get("admission") not in {"open", "closed"}
            or control.get("status") not in {"active", "draining", "hold"}):
        raise held("BROWSER_PROTOCOL_UNAVAILABLE")
    expiry = control.get("expires_at")
    if fence.owner_kind == "human" and (type(expiry) not in (int, float) or expiry <= 0):
        raise held("BROWSER_PROTOCOL_UNAVAILABLE")
    return control, fence


def _isolation(payload, mode):
    """Require the persisted mode and the dedicated supervisor's startup proof.

    The supplier separately verifies the original Docker image and topology.
    Diagnostic Chromium cannot enroll, even if a caller supplies true checks.
    """
    report = payload.get("isolation") or {}
    checks = report.get("checks") or {}
    if (mode not in {"container_uid", "chromium_sandbox"} or report.get("mode") != mode
            or report.get("verification") != "passed"
            or any(type(report.get(key)) is not int or report[key] != expected
                for key, expected in (("supervisor_uid", 0), ("browser_uid", 1100), ("browser_gid", 1100)))
            or not ISOLATION_CHECKS.issubset(checks) or any(checks[name] is not True for name in ISOLATION_CHECKS)
            or payload.get("browser_sandbox") is not (mode == "chromium_sandbox")):
        raise held("BROWSER_ISOLATION_UNVERIFIED")


async def binding_locked(db, *, user_id, workspace_id, main_id, resource_id):
    await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    await controls.actor(db, user_id, workspace_id)
    row = await controls.locked(db, resource_id)
    binding = await db.get(BrowserResourceBinding, resource_id)
    if (row is None or binding is None or row.provider != PROVIDER
            or row.resource_type != "browser_profile" or row.workspace_id != workspace_id
            or (binding.actor_user_id, binding.workspace_id, binding.assistant_session_id)
                != (user_id, workspace_id, main_id)):
        raise AssistantError(404, "BROWSER_UNAVAILABLE", "当前账号没有这个私有浏览器。")
    identity = _identity(binding.identity)
    runtime = await db.get(PrivateRuntimeBinding, binding.private_runtime_id)
    if (runtime is None or runtime.status != "ready" or runtime.kind != "browser_profile"
            or (runtime.actor_user_id, runtime.workspace_id, runtime.revision)
                != (user_id, workspace_id, binding.runtime_revision)
            or row.physical_id != identity["runtime_id"] + ":" + identity["profile_id"]
            or row.remote_journal_id != identity["journal_id"]):
        raise held("BROWSER_IDENTITY_CHANGED")
    return row, binding


async def client_for(binding):
    """Re-resolve only to verify the original physical binding, never to follow it."""
    from sandbox.private_runtime import resolve_private_runtime, validate_private_runtime
    route = await resolve_private_runtime(session_id=binding.assistant_session_id,
        user_id=binding.actor_user_id, workspace_id=binding.workspace_id,
        kind="browser_profile", create=False)
    if (route.binding_id, route.revision, route.kind) != (
            binding.private_runtime_id, binding.runtime_revision, "browser_profile"):
        raise held("BROWSER_IDENTITY_CHANGED")
    await validate_private_runtime(route, session_id=binding.assistant_session_id,
        user_id=binding.actor_user_id, workspace_id=binding.workspace_id, kind="browser_profile")
    client = BrowserResourceClient(f"http://{route.host}:{route.port}", route.api_key,
        identity=_identity(binding.identity))
    _isolation(await client.status(), route.isolation_mode)
    return client


async def ensure_browser(*, user_id, workspace_id, main_id):
    from sandbox.private_runtime import resolve_private_runtime, validate_private_runtime
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        old = await db.scalar(select(BrowserResourceBinding).where(
            BrowserResourceBinding.actor_user_id == user_id, BrowserResourceBinding.workspace_id == workspace_id))
    if old is not None:
        return await read_browser(user_id=user_id, workspace_id=workspace_id, main_id=main_id,
            resource_id=old.resource_id)
    route = await resolve_private_runtime(session_id=main_id, user_id=user_id,
        workspace_id=workspace_id, kind="browser_profile", create=True)
    client = BrowserResourceClient(f"http://{route.host}:{route.port}", route.api_key)
    # Docker reports "running" before Chromium and its startup proof are ready.
    # Probe only this original address; never provision a replacement on delay.
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise held("BROWSER_STARTUP_PENDING")
        try:
            status = await asyncio.wait_for(client.status(), timeout=min(2, remaining))
            if status.get("browser_live") is True:
                break
        except (httpx.TransportError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(min(.15, max(0, deadline - asyncio.get_running_loop().time())))
    identity = _identity(status.get("identity"))
    if identity["resource_id"] != route.resource_id:
        raise held("BROWSER_IDENTITY_CHANGED")
    remote, fence = _control(status, identity)
    _isolation(status, route.isolation_mode)
    if (fence != controls.ResourceFence(identity["resource_id"], 1, "automation", workspace_id)
            or remote["status"] != "active" or remote["admission"] != "open"
            or status.get("browser_live") is not True or status.get("blocking_operations")
            or set(status.get("supported_operations", [])) != OPERATIONS):
        raise held("BROWSER_INITIAL_STATE_UNAVAILABLE")
    await validate_private_runtime(route, session_id=main_id, user_id=user_id,
        workspace_id=workspace_id, kind="browser_profile")
    async with get_db_session() as db:
        await begin_session_write(db)
        await lock_actor(db, user_id)
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        runtime = await db.get(PrivateRuntimeBinding, route.binding_id)
        if (runtime is None or runtime.status != "ready" or runtime.revision != route.revision
                or runtime.kind != "browser_profile"):
            raise held("BROWSER_IDENTITY_CHANGED")
        old = await db.scalar(select(BrowserResourceBinding).where(
            BrowserResourceBinding.actor_user_id == user_id, BrowserResourceBinding.workspace_id == workspace_id))
        if old is not None:
            if old.identity != identity or old.private_runtime_id != route.binding_id:
                raise held("BROWSER_IDENTITY_CHANGED")
        else:
            now = await controls.clock(db)
            db.add(ResourceControlLease(id=fence.resource_id, resource_type="browser_profile", provider=PROVIDER,
                physical_id=identity["runtime_id"] + ":" + identity["profile_id"], workspace_id=workspace_id,
                owner_kind="automation", owner_id=workspace_id, epoch=1, status="active", admission_state="open",
                remote_journal_id=identity["journal_id"], remote_status=status, created_at=now, updated_at=now))
            await db.flush()
            db.add(BrowserResourceBinding(resource_id=fence.resource_id, private_runtime_id=route.binding_id,
                runtime_revision=route.revision, actor_user_id=user_id, workspace_id=workspace_id,
                assistant_session_id=main_id, identity=identity, created_at=now))
    return await read_browser(user_id=user_id, workspace_id=workspace_id, main_id=main_id,
        resource_id=fence.resource_id)


def _snapshot(row, binding, *, remote_available, pending=None):
    return {"resource_id": row.id, "resource_type": "browser_profile", "fence": asdict(controls.fence_for(row)),
        "status": row.status, "admission": row.admission_state,
        "expires_at": controls.aware(row.expires_at).isoformat() if row.expires_at else None,
        "remote_available": remote_available,
        "can_takeover": remote_available and row.owner_kind == "automation",
        "can_giveback": remote_available and row.owner_kind == "human" and row.owner_id == binding.actor_user_id,
        "fresh_observation_required": bool((row.remote_status or {}).get("fresh_observation_required", True)),
        "pending_control": ({"action": pending.action.removeprefix("browser_"),
            "expected_epoch": pending.expected_revision, "idempotency_key": pending.idempotency_key,
            "command_id": pending.id} if pending else None)}


async def current_browser(*, user_id, workspace_id, main_id):
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        binding = await db.scalar(select(BrowserResourceBinding).where(
            BrowserResourceBinding.actor_user_id == user_id, BrowserResourceBinding.workspace_id == workspace_id))
    if binding is None:
        return {"resource": None}
    return {"resource": await read_browser(user_id=user_id, workspace_id=workspace_id,
        main_id=main_id, resource_id=binding.resource_id)}


async def read_browser(*, user_id, workspace_id, main_id, resource_id):
    scope = dict(user_id=user_id, workspace_id=workspace_id, main_id=main_id, resource_id=resource_id)
    async with get_db_session() as db:
        _, binding = await binding_locked(db, **scope)
    available, status = False, None
    try:
        client = await client_for(binding)
        status = await client.status()
        remote, fence = _control(status, binding.identity)
        available = status.get("browser_live") is True
    except (BrowserResourceError, httpx.HTTPError, OSError, TimeoutError):
        pass
    async with get_db_session() as db:
        row, binding = await binding_locked(db, **scope)
        if status is not None:
            if fence != controls.fence_for(row):
                # Only an original accepted command may publish a new epoch.
                row.status, row.admission_state = "hold", "closed"
                available = False
            elif remote["admission"] == "closed":
                row.status, row.admission_state = "hold", "closed"
            row.remote_status = status
        if not available:
            row.status, row.admission_state = "hold", "closed"
        if row.expires_at and controls.aware(row.expires_at) <= await controls.clock(db):
            row.status, row.admission_state = "hold", "closed"
        pending = await db.scalar(select(AssistantCommand).where(AssistantCommand.target_type == "browser_resource",
            AssistantCommand.target_id == resource_id, AssistantCommand.state == "accepted"))
        return _snapshot(row, binding, remote_available=available, pending=pending)


async def _linked_tasks(db, resource_id):
    ids = set((await db.scalars(select(BrowserResourceSession.session_id).where(
        BrowserResourceSession.resource_id == resource_id))).all())
    # Child tools retain their own Session identity; task control belongs to
    # the owning root execution. Follow only persisted parents.
    pending = list(ids)
    while pending:
        session = await db.get(Session, pending.pop())
        if session is not None and session.parent_id and session.parent_id not in ids:
            ids.add(session.parent_id)
            pending.append(session.parent_id)
    return list((await db.scalars(select(AssistantTask).where(
        AssistantTask.execution_session_id.in_(ids), AssistantTask.archived_at.is_(None)))).all()) if ids else []


async def accept_control(*, user_id, workspace_id, main_id, resource_id, action, expected_epoch, idempotency_key):
    if (action not in {"takeover", "giveback", "close"} or type(expected_epoch) is not int or expected_epoch < 1
            or not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 64):
        raise ValueError("A supported browser command, epoch and stable key are required")
    scope = dict(user_id=user_id, workspace_id=workspace_id, main_id=main_id, resource_id=resource_id)
    digest = command_digest({"action": action, "resource_id": resource_id, "expected_epoch": expected_epoch})
    async with get_db_session() as db:
        await begin_session_write(db)
        await lock_actor(db, user_id)
        row, binding = await binding_locked(db, **scope)
        old = await db.scalar(select(AssistantCommand).where(AssistantCommand.actor_user_id == user_id,
            AssistantCommand.workspace_id == workspace_id, AssistantCommand.assistant_session_id == main_id,
            AssistantCommand.idempotency_key == idempotency_key))
        if old is not None:
            if old.payload_digest != digest or old.target_type != "browser_resource":
                raise AssistantError(409, "ASSISTANT_COMMAND_CONFLICT", "命令编号已用于其他请求。")
            return old.id
        source = controls.fence_for(row)
        if (source.epoch != expected_epoch or source.epoch >= 2**31 - 1
                or action == "takeover" and source.owner_kind != "automation"
                or action == "giveback" and (source.owner_kind, source.owner_id) != ("human", user_id)):
            raise held("BROWSER_FENCE_CHANGED")
        if await db.scalar(select(AssistantCommand.id).where(
                AssistantCommand.target_type == "browser_resource", AssistantCommand.target_id == resource_id,
                AssistantCommand.state == "accepted")):
            raise held("BROWSER_COMMAND_PENDING")
        pauses, resumes = [], []
        for task in await _linked_tasks(db, resource_id):
            if (task.user_id, task.workspace_id, task.assistant_session_id) != (user_id, workspace_id, main_id):
                raise held("BROWSER_TASK_SCOPE_CHANGED")
            if task.desired_state == "running":
                driver = await db.get(AgentDriverState, task.execution_session_id)
                pauses.append({"task_id": task.id, "expected_revision": task.control_revision,
                    "expected_run": {"run_id": driver.run_id, "generation": driver.generation}
                        if driver and driver.phase != "idle" else None})
        if action == "giveback":
            takeover = await db.scalar(select(AssistantCommand).where(
                AssistantCommand.target_type == "browser_resource", AssistantCommand.target_id == resource_id,
                AssistantCommand.action == "browser_takeover", AssistantCommand.expected_revision == source.epoch - 1,
                AssistantCommand.state == "applied"))
            if takeover and takeover.source_ref.get("target_fence") == asdict(source):
                for candidate in takeover.source_ref.get("resumable_tasks", []):
                    task = await db.get(AssistantTask, candidate["task_id"])
                    if (task and task.desired_state == task.observed_state == "paused"
                            and task.control_revision == candidate["expected_revision"]):
                        resumes.append(dict(candidate))
        row.status, row.admission_state, row.last_observation_ref = "draining", "closed", None
        now, command_id = await controls.clock(db), generate_id()
        target = asdict(source) if action == "close" else asdict(controls.ResourceFence(resource_id,
            source.epoch + 1, "human" if action == "takeover" else "automation",
            user_id if action == "takeover" else workspace_id))
        db.add(AssistantCommand(id=command_id, actor_user_id=user_id, workspace_id=workspace_id,
            assistant_session_id=main_id, idempotency_key=idempotency_key, action="browser_" + action,
            target_type="browser_resource", target_id=resource_id, payload_digest=digest,
            expected_revision=expected_epoch, state="accepted", source_ref={"identity": binding.identity,
                "private_runtime_id": binding.private_runtime_id, "runtime_revision": binding.runtime_revision,
                "source_fence": asdict(source), "target_fence": target, "pauses": pauses, "resumes": resumes},
            receipt={"command_id": command_id, "resource_id": resource_id, "action": action, "state": "draining"},
            created_at=now, updated_at=now))
        return command_id


async def _command_scope(command_id):
    async with get_db_session() as db:
        command = await db.get(AssistantCommand, command_id)
        if command is None or command.target_type != "browser_resource":
            raise held("BROWSER_COMMAND_UNAVAILABLE")
        scope = dict(user_id=command.actor_user_id, workspace_id=command.workspace_id,
            main_id=command.assistant_session_id, resource_id=command.target_id)
        row, binding = await binding_locked(db, **scope)
        source = command.source_ref
        if (source.get("identity") != binding.identity
                or source.get("private_runtime_id") != binding.private_runtime_id
                or source.get("runtime_revision") != binding.runtime_revision):
            raise held("BROWSER_IDENTITY_CHANGED")
        return command, row, binding, scope


async def _local_drained(db, resource_id):
    if await db.scalar(select(ExternalEffect.id).where(ExternalEffect.resource_id == resource_id,
            ExternalEffect.submitting_at.is_not(None), ExternalEffect.state.not_in(("succeeded", "failed"))).limit(1)):
        return False
    return all(task.desired_state != "running" and task.observed_state not in {"pausing", "canceling"}
        for task in await _linked_tasks(db, resource_id))


async def _resumable_tasks(db, command):
    """Freeze the fully stopped revisions still owned by this takeover."""
    candidates = []
    for pause in command.source_ref["pauses"]:
        task = await db.get(AssistantTask, pause["task_id"])
        latest = await db.scalar(select(AssistantCommand).where(
            AssistantCommand.actor_user_id == command.actor_user_id,
            AssistantCommand.workspace_id == command.workspace_id,
            AssistantCommand.assistant_session_id == command.assistant_session_id,
            AssistantCommand.target_type == "task", AssistantCommand.target_id == pause["task_id"],
            AssistantCommand.action.in_(("task_pause", "task_resume", "task_cancel")))
            .order_by(AssistantCommand.created_at.desc(), AssistantCommand.id.desc()).limit(1))
        if (task and latest and task.desired_state == task.observed_state == "paused"
                and latest.action == "task_pause" and latest.state == "applied"
                and latest.idempotency_key == command_digest({"browser_command": command.id, "task": task.id})
                and task.intent_revision == latest.source_ref.get("control", {}).get("intent_revision")):
            candidates.append({"task_id": task.id, "expected_revision": task.control_revision, "expected_run": None})
    return candidates


def _receipt(payload, command, binding, action, *, closing=False):
    if payload.get("protocol") != PROTOCOL or payload.get("identity") != binding.identity:
        raise held("BROWSER_IDENTITY_CHANGED")
    receipt = payload.get("command_receipt") or {}
    original = command.source_ref
    expected_id = command.id + ":close" if closing else command.id
    if (receipt.get("identity") != binding.identity or receipt.get("command_id") != expected_id
            or receipt.get("action") != action or receipt.get("source_fence") != original["source_fence"]
            or receipt.get("target_fence") != (original["source_fence"] if closing else original["target_fence"])
            or receipt.get("state") != "applied"):
        raise held("BROWSER_RECEIPT_CHANGED")
    return receipt


async def dispatch_control(command_id):
    command, row, binding, scope = await _command_scope(command_id)
    client = await client_for(binding)
    action = command.action.removeprefix("browser_")
    source = command.source_ref["source_fence"]
    if command.state == "accepted":
        if asdict(controls.fence_for(row)) != source or row.admission_state != "closed":
            raise held("BROWSER_FENCE_CHANGED")
        closed = await client.control("close", fence=source, command_id=command.id + ":close",
            actor_id=command.actor_user_id)
        _receipt(closed, command, binding, "close", closing=True)
        # Each pause retains the task revision/run observed at acceptance.
        # A changed task is held for review instead of silently re-targeted.
        from assistant.control import accept_control_command
        for pause in command.source_ref["pauses"]:
            await accept_control_command(user_id=command.actor_user_id, workspace_id=command.workspace_id,
                main_id=command.assistant_session_id, action="pause",
                idempotency_key=command_digest({"browser_command": command.id, "task": pause["task_id"]}), **pause)
        async with get_db_session() as db:
            row, _ = await binding_locked(db, **scope)
            if not await _local_drained(db, row.id):
                return dict(command.receipt)
        status = await client.status()
        remote, remote_fence = _control(status, binding.identity)
        if status.get("blocking_operations") or status.get("browser_live") is not True:
            return dict(command.receipt)
        if action == "close":
            result = closed
        else:
            # A replay after response loss may already have the target epoch.
            # Only the exact command's immutable receipt can prove that grant.
            target = controls.ResourceFence(**command.source_ref["target_fence"])
            if remote_fence == target:
                result = await client.control_receipt(command.id)
            elif asdict(remote_fence) == source and remote["admission"] == "closed":
                result = await client.control(action, fence=source, command_id=command.id,
                    actor_id=command.actor_user_id,
                    next_owner_id=command.actor_user_id if action == "takeover" else None,
                    ttl_seconds=120 if action == "takeover" else None)
            else:
                raise held("BROWSER_FENCE_CHANGED")
            _receipt(result, command, binding, action)
        latest = await client.status()
        control, latest_fence = _control(latest, binding.identity)
        if (asdict(latest_fence) != command.source_ref["target_fence"]
                or latest.get("blocking_operations") or latest.get("browser_live") is not True):
            raise held("BROWSER_NOT_DRAINED")
        async with get_db_session() as db:
            await begin_session_write(db)
            row, current_binding = await binding_locked(db, **scope)
            current = await db.get(AssistantCommand, command.id)
            if current.state == "accepted":
                if (asdict(controls.fence_for(row)) != source or current_binding.identity != binding.identity
                        or row.admission_state != "closed" or not await _local_drained(db, row.id)):
                    raise held("BROWSER_FENCE_CHANGED")
                now = await controls.clock(db)
                expiry = (datetime.fromtimestamp(control["expires_at"], timezone.utc)
                    if control.get("expires_at") is not None else None)
                expired = expiry is not None and expiry <= now
                if expired and control["admission"] != "closed":
                    # SQL time alone cannot prove that the remote human input
                    # gate closed. Retry the original command/status; its
                    # remote expiry tick must confirm closure first.
                    raise held("BROWSER_TOKEN_EXPIRED")
                row.epoch, row.owner_kind, row.owner_id = latest_fence.epoch, latest_fence.owner_kind, latest_fence.owner_id
                row.status, row.admission_state = ("hold", "closed") if expired else (control["status"], control["admission"])
                row.expires_at, row.last_observation_ref, row.remote_status = expiry, None, latest
                row.updated_at = now
                current.state, current.updated_at = "applied", now
                if action == "takeover":
                    current.source_ref = {**current.source_ref, "resumable_tasks": await _resumable_tasks(db, current)}
                current.receipt = {"command_id": current.id, "resource_id": row.id,
                    "action": action, "state": "applied", "fence": asdict(latest_fence),
                    "fresh_observation_required": action == "giveback",
                    **({"human_grant_expired": True} if expired and action == "takeover" else {})}
    command, row, binding, scope = await _command_scope(command_id)
    response = dict(command.receipt)
    if response.get("human_grant_expired") is True:
        # This is historical adoption of the exact already applied grant, not
        # a new grant. Never reconstruct/return its expired token; the user can
        # now explicitly give back the held human epoch through normal control.
        return response
    if (action == "giveback" and command.state == "applied"
            and asdict(controls.fence_for(row)) == command.source_ref["target_fence"]
            and row.status == "active" and row.admission_state == "open"):
        from assistant.control import accept_control_command, recover_controls
        resumed, held_tasks = [], []
        for resume in command.source_ref.get("resumes", []):
            try:
                await accept_control_command(user_id=command.actor_user_id, workspace_id=command.workspace_id,
                    main_id=command.assistant_session_id, action="resume",
                    idempotency_key=command_digest({"browser_return": command.id, "task": resume["task_id"]}),
                    browser_fence=command.source_ref["target_fence"], **resume)
                resumed.append(resume["task_id"])
            except AssistantError:
                # An intervening manual task command keeps its newer authority.
                held_tasks.append(resume["task_id"])
        for task_id in resumed:
            await recover_controls(task_id=task_id)
        response.update(resume_requested_task_ids=resumed, task_control_changed_ids=held_tasks)
    if action == "takeover" and command.state == "applied":
        grant = await client.control("takeover", fence=source, command_id=command.id,
            actor_id=command.actor_user_id, next_owner_id=command.actor_user_id, ttl_seconds=120)
        _receipt(grant, command, binding, action)
        control, fence = _control(await client.status(), binding.identity)
        async with get_db_session() as db:
            row, _ = await binding_locked(db, **scope)
            if (asdict(controls.fence_for(row)) != command.source_ref["target_fence"]
                    or fence != controls.fence_for(row) or row.status != "active"
                    or row.admission_state != "open" or control["admission"] != "open"
                    or not row.expires_at or controls.aware(row.expires_at) <= await controls.clock(db)):
                raise held("BROWSER_TOKEN_EXPIRED")
        token = grant.get("human_token")
        if not isinstance(token, str) or not token:
            raise held("BROWSER_TOKEN_UNAVAILABLE")
        response["human_token"] = token
        response["expires_at"] = controls.aware(row.expires_at).isoformat()
    return response


async def human_operation(*, user_id, workspace_id, main_id, resource_id, fence, operation_id,
                          kind, args, human_token):
    scope = dict(user_id=user_id, workspace_id=workspace_id, main_id=main_id, resource_id=resource_id)
    if (kind not in OPERATIONS or not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 64
            or not isinstance(human_token, str) or not 1 <= len(human_token) <= 256):
        raise ValueError("A bounded browser operation and current human grant are required")
    expected = controls.ResourceFence(**fence)

    async def current():
        async with get_db_session() as db:
            row, binding = await binding_locked(db, **scope)
            if (expected != controls.fence_for(row) or expected.owner_kind != "human"
                    or expected.owner_id != user_id or row.status != "active" or row.admission_state != "open"
                    or not row.expires_at or controls.aware(row.expires_at) <= await controls.clock(db)):
                raise held("BROWSER_TOKEN_EXPIRED")
            return binding

    binding = await current()
    client = await client_for(binding)
    await current()
    try:
        result = await client.operate(fence=asdict(expected), operation_id=operation_id,
            kind=kind, args=args, human_token=human_token)
        _control(result, binding.identity)
        receipt = result.get("receipt") or {}
        if (receipt.get("identity") != binding.identity or receipt.get("fence") != asdict(expected)
                or receipt.get("operation_id") != operation_id or receipt.get("kind") != kind
                or receipt.get("state") != "completed"):
            raise held("BROWSER_OUTCOME_UNKNOWN")
    except (httpx.HTTPError, BrowserResourceError, AssistantError):
        async with get_db_session() as db:
            row, _ = await binding_locked(db, **scope)
            if controls.fence_for(row) == expected:
                row.status, row.admission_state, row.last_observation_ref = "hold", "closed", None
        raise
    await client_for(binding)
    await current()
    return {"operation_id": operation_id, "fence": asdict(expected), "state": "completed",
        "result": receipt.get("result")}


async def heartbeat(*, user_id, workspace_id, main_id, resource_id, fence, human_token, command_id):
    scope = dict(user_id=user_id, workspace_id=workspace_id, main_id=main_id, resource_id=resource_id)
    expected = controls.ResourceFence(**fence)
    async with get_db_session() as db:
        row, binding = await binding_locked(db, **scope)
        if (expected != controls.fence_for(row) or expected.owner_kind != "human" or expected.owner_id != user_id
                or row.status != "active" or row.admission_state != "open"
                or not row.expires_at or controls.aware(row.expires_at) <= await controls.clock(db)):
            raise held("BROWSER_TOKEN_EXPIRED")
    client = await client_for(binding)
    result = await client.control("heartbeat", fence=asdict(expected), command_id=command_id,
        actor_id=user_id, ttl_seconds=120, human_token=human_token)
    remote, actual = _control(result, binding.identity)
    if actual != expected or remote["admission"] != "open":
        raise held("BROWSER_FENCE_CHANGED")
    await client_for(binding)
    async with get_db_session() as db:
        row, _ = await binding_locked(db, **scope)
        if expected != controls.fence_for(row) or row.admission_state != "open":
            raise held("BROWSER_FENCE_CHANGED")
        row.expires_at = datetime.fromtimestamp(remote["expires_at"], timezone.utc)
        return {"fence": asdict(expected), "expires_at": row.expires_at.isoformat()}
