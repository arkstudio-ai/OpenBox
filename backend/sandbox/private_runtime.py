"""SQL authority and recoverable provisioning for the private Docker adapter."""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import re
import secrets
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from assistant.policy import AssistantError
from core.crypto import decrypt_secret, encrypt_secret, secret_hash
from db.base import get_db_session
from db.models.private_runtime import PrivateRuntimeBinding
from db.models.session import Session
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from sandbox.private_docker import (
    DockerPrivateBackend, NAME_PREFIX, PrivateDockerError, PrivateDockerIdentityError,
    container_identity, labels, resource_id, volume_identity, volume_roles,
)


class PrivateRuntimeError(AssistantError):
    pass


@dataclass(frozen=True)
class PrivateSessionScope:
    session_id: str
    user_id: str
    workspace_id: str


@dataclass(frozen=True)
class PrivateRuntimeRoute:
    binding_id: str
    kind: str
    provider: str
    workspace_id: str
    actor_user_id: str
    attempt_id: str
    revision: int
    container_id: str
    name: str
    image: str
    image_id: str
    created_at: datetime
    host: str
    port: int
    route_key: str
    api_key: str = field(repr=False)
    resource_id: str | None = None
    isolation_mode: str = "process_uid"


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _now():
    return datetime.now(timezone.utc)


def _error(code, detail, status=409):
    return PrivateRuntimeError(status, "PRIVATE_RUNTIME_" + code, detail)


async def _scope(db, session_id, user_id, workspace_id=None, *, lock=False):
    statement = select(Session).join(User, User.id == Session.user_id).join(Workspace, Workspace.id == Session.workspace_id).join(
        WorkspaceMember, and_(WorkspaceMember.workspace_id == Session.workspace_id,
                              WorkspaceMember.user_id == user_id)).where(
        Session.id == session_id, Session.user_id == user_id, Session.is_deleted.is_(False),
        User.is_active.is_(True), User.is_deleted.is_(False),
        Workspace.is_deleted.is_(False), WorkspaceMember.status == "active")
    if workspace_id is not None:
        statement = statement.where(Session.workspace_id == workspace_id)
    if lock:
        statement = statement.with_for_update(of=(Session, User, Workspace, WorkspaceMember))
    session = await db.scalar(statement)
    if session is None:
        raise _error("SCOPE_INVALID", "The current Session owner or workspace membership is unavailable", 403)
    if session.kind != "assistant" and session.visibility != "private" and session.memory_policy != "assistant_isolated":
        return None
    return PrivateSessionScope(session.id, session.user_id, session.workspace_id)


async def private_session_scope(*, session_id, user_id, workspace_id=None):
    """Resolve persisted privacy, never a caller header or a resource label."""
    async with get_db_session() as db:
        return await _scope(db, session_id, user_id, workspace_id)


def _config(user_id, kind="sandbox"):
    from core.config import get_config
    config = get_config().private_runtime
    if not config.enabled or user_id not in config.allowed_user_ids:
        raise _error("DISABLED", "Private Docker runtime is not enabled for this actor", 403)
    if not config.secret_key:
        raise _error("UNAVAILABLE", "Private runtime secret storage is not configured", 503)
    if kind not in {"sandbox", "browser_profile"}:
        raise _error("UNAVAILABLE", "Unsupported private runtime kind")
    if kind == "browser_profile" and (not config.browser_image or config.browser_image == config.image):
        raise _error("UNAVAILABLE", "A separate private browser image must be configured", 503)
    return config


def _snapshot(row):
    return {column.name: deepcopy(getattr(row, column.name)) for column in PrivateRuntimeBinding.__table__.columns}


def _aad(binding):
    return "openbox:private-runtime:v1:" + ":".join(binding[key] for key in ("id", "workspace_id", "actor_user_id", "kind", "attempt_id"))


def _mode_enabled(binding, config):
    if binding["kind"] == "browser_profile" and binding["isolation_mode"] != config.browser_isolation:
        raise _error("DISABLED", "The pinned browser isolation mode is no longer enabled", 403)


def _route(binding, config):
    _mode_enabled(binding, config)
    if binding["status"] != "ready" or binding["kind"] not in {"sandbox", "browser_profile"} or binding["provider"] != "private_docker_v1":
        raise _error("UNAVAILABLE", "The private runtime has no verified ready route")
    if not binding["container_id"] or not binding["host_port"] or not binding["physical_digest"]:
        raise _error("UNAVAILABLE", "The private runtime binding is incomplete")
    try:
        key = decrypt_secret(binding["api_key_ciphertext"], _aad(binding), config.secret_key)
    except Exception:
        raise _error("UNAVAILABLE", "The private runtime credential cannot be recovered", 503) from None
    if secret_hash(key) != binding["api_key_hash"]:
        raise _error("IDENTITY_CHANGED", "The private runtime credential identity changed")
    return PrivateRuntimeRoute(binding["id"], binding["kind"], binding["provider"], binding["workspace_id"],
        binding["actor_user_id"], binding["attempt_id"], binding["revision"], binding["container_id"],
        binding["container_name"], binding["image"], binding["image_id"], _utc(binding["created_at"]),
        "127.0.0.1", binding["host_port"], binding["route_key"], key, resource_id(binding), binding["isolation_mode"])


async def find_private_binding(container_id):
    """Deny-only alias recognition, including non-ready and uncertain rows.

    This does not authorize an actor. Returned metadata contains no key,
    ciphertext, object contents or host volume path.
    """
    if not isinstance(container_id, str) or not container_id or len(container_id) > 160:
        return None
    reference = container_id.lstrip("/")
    filters = [PrivateRuntimeBinding.id == reference, PrivateRuntimeBinding.route_key == reference,
               PrivateRuntimeBinding.container_name == reference, PrivateRuntimeBinding.container_id == reference]
    if re.fullmatch(r"[0-9a-f]{12,64}", reference):
        filters.append(PrivateRuntimeBinding.container_id.startswith(reference, autoescape=True))
    async with get_db_session() as db:
        row = await db.scalar(select(PrivateRuntimeBinding).where(or_(*filters)).limit(1))
        return None if row is None else {key: getattr(row, key) for key in (
            "id", "kind", "provider", "status", "workspace_id", "actor_user_id", "container_id", "container_name", "route_key")}


async def _load(scope, *, binding_id=None, kind="sandbox"):
    async with get_db_session() as db:
        current = await _scope(db, scope.session_id, scope.user_id, scope.workspace_id)
        if current != scope:
            raise _error("SCOPE_INVALID", "The Session is no longer private", 403)
        statement = select(PrivateRuntimeBinding).where(PrivateRuntimeBinding.workspace_id == scope.workspace_id,
            PrivateRuntimeBinding.actor_user_id == scope.user_id, PrivateRuntimeBinding.kind == kind)
        if binding_id:
            statement = statement.where(PrivateRuntimeBinding.id == binding_id)
        row = await db.scalar(statement)
        return None if row is None else _snapshot(row)


async def _reserve(scope, config, kind="sandbox"):
    for _ in range(3):
        try:
            async with get_db_session() as db:
                if await _scope(db, scope.session_id, scope.user_id, scope.workspace_id, lock=True) != scope:
                    raise _error("SCOPE_INVALID", "The Session is no longer private", 403)
                existing = await db.scalar(select(PrivateRuntimeBinding).where(
                    PrivateRuntimeBinding.workspace_id == scope.workspace_id,
                    PrivateRuntimeBinding.actor_user_id == scope.user_id, PrivateRuntimeBinding.kind == kind))
                if existing:
                    return _snapshot(existing)
                identity = uuid4().hex
                values = dict(id="prt_" + identity, workspace_id=scope.workspace_id, actor_user_id=scope.user_id,
                    kind=kind, isolation_mode="process_uid" if kind == "sandbox" else config.browser_isolation,
                    provider="private_docker_v1", status="reserved", attempt_id=uuid4().hex,
                    revision=1, provision_phase="reserved", container_name=NAME_PREFIX + identity,
                    workspace_volume=NAME_PREFIX + identity + "-workspace" if kind == "sandbox" else None,
                    data_volume=NAME_PREFIX + identity + "-data",
                    volume_identities={}, image=config.image if kind == "sandbox" else config.browser_image,
                    route_key="private:prt_" + identity,
                    created_at=_now(), updated_at=_now())
                key = secrets.token_urlsafe(32)
                values.update(api_key_ciphertext=encrypt_secret(key, _aad(values), config.secret_key), api_key_hash=secret_hash(key))
                row = PrivateRuntimeBinding(**values)
                db.add(row)
                await db.flush()
                return _snapshot(row)
        except IntegrityError:
            continue
    raise _error("PENDING", "Another request is reserving the private runtime")


def _same(current, original):
    return current is not None and all(current[key] == original[key] for key in (
        "id", "workspace_id", "actor_user_id", "kind", "isolation_mode", "provider", "attempt_id", "revision", "status",
        "container_name", "container_id", "workspace_volume", "data_volume", "volume_identities",
        "image", "image_id", "route_key", "api_key_ciphertext", "api_key_hash", "host_port", "physical_digest",
        "claim_token", "provision_phase"))


async def _claim(scope, binding, config):
    async with get_db_session() as db:
        if await _scope(db, scope.session_id, scope.user_id, scope.workspace_id, lock=True) != scope:
            raise _error("SCOPE_INVALID", "The Session is no longer private", 403)
        row = await db.scalar(select(PrivateRuntimeBinding).where(PrivateRuntimeBinding.id == binding["id"]).with_for_update())
        current = _snapshot(row) if row else None
        if not _same(current, binding) or current["status"] not in {"reserved", "provisioning"}:
            raise _error("PENDING", "The private runtime reservation changed")
        if current["claim_token"] and current["claim_expires_at"] and _utc(current["claim_expires_at"]) > _now():
            raise _error("PENDING", "Private runtime provisioning is already in progress")
        changed = await db.execute(update(PrivateRuntimeBinding).where(
            PrivateRuntimeBinding.id == binding["id"], PrivateRuntimeBinding.revision == binding["revision"],
            PrivateRuntimeBinding.attempt_id == binding["attempt_id"],
            PrivateRuntimeBinding.claim_token == binding["claim_token"],
            PrivateRuntimeBinding.status == binding["status"]).values(
                claim_token=uuid4().hex, claim_expires_at=_now() + timedelta(seconds=config.lease_seconds),
                status="provisioning", updated_at=_now(), revision=binding["revision"] + 1),
            execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            raise _error("PENDING", "Another request claimed private runtime provisioning")
        await db.refresh(row)
        return _snapshot(row)


async def _current(scope, binding):
    _mode_enabled(binding, _config(scope.user_id, binding["kind"]))
    current = await _load(scope, binding_id=binding["id"], kind=binding["kind"])
    if not _same(current, binding) or not current["claim_expires_at"] or _utc(current["claim_expires_at"]) <= _now():
        raise _error("CLAIM_LOST", "Private runtime provisioning authority changed")


async def _advance(scope, binding, **changes):
    _mode_enabled(binding, _config(scope.user_id, binding["kind"]))
    async with get_db_session() as db:
        if await _scope(db, scope.session_id, scope.user_id, scope.workspace_id, lock=True) != scope:
            raise _error("SCOPE_INVALID", "The Session is no longer private", 403)
        row = await db.scalar(select(PrivateRuntimeBinding).where(PrivateRuntimeBinding.id == binding["id"]).with_for_update())
        current = _snapshot(row) if row else None
        if not _same(current, binding) or not current["claim_expires_at"] or _utc(current["claim_expires_at"]) <= _now():
            raise _error("CLAIM_LOST", "Private runtime provisioning authority changed")
        if changes.get("image_id") and await db.scalar(select(PrivateRuntimeBinding.id).where(
                PrivateRuntimeBinding.workspace_id == scope.workspace_id,
                PrivateRuntimeBinding.actor_user_id == scope.user_id,
                PrivateRuntimeBinding.kind != binding["kind"],
                PrivateRuntimeBinding.image_id == changes["image_id"])):
            raise PrivateDockerIdentityError("Sandbox and browser supervisor images must be distinct")
        result = await db.execute(update(PrivateRuntimeBinding).where(
            PrivateRuntimeBinding.id == binding["id"], PrivateRuntimeBinding.revision == binding["revision"],
            PrivateRuntimeBinding.attempt_id == binding["attempt_id"],
            PrivateRuntimeBinding.claim_token == binding["claim_token"],
            PrivateRuntimeBinding.status == "provisioning").values(
                **changes, revision=binding["revision"] + 1, updated_at=_now()),
            execution_options={"synchronize_session": False})
        if result.rowcount != 1:
            raise _error("CLAIM_LOST", "Private runtime provisioning authority changed")
        await db.refresh(row)
        return _snapshot(row)


async def _release_claim(binding, code, *, blocked=False):
    # Cleanup only the claim we held. Never follow a replacement binding or
    # restore authority after membership, Session or assignment changes.
    async with get_db_session() as db:
        await db.execute(update(PrivateRuntimeBinding).where(
            PrivateRuntimeBinding.id == binding["id"], PrivateRuntimeBinding.attempt_id == binding["attempt_id"],
            PrivateRuntimeBinding.revision == binding["revision"], PrivateRuntimeBinding.claim_token == binding["claim_token"],
            PrivateRuntimeBinding.status == "provisioning").values(claim_token=None, claim_expires_at=None,
                error_code=code, status="blocked" if blocked else "provisioning",
                revision=binding["revision"] + 1, updated_at=_now()))


async def _physical(binding, docker, *, running):
    volumes = {}
    for role in volume_roles(binding):
        actual = volume_identity(await docker.inspect_volume(binding[role + "_volume"]), binding, role)
        if actual != binding["volume_identities"].get(role):
            raise PrivateDockerIdentityError("Private volume identity changed")
        volumes[role] = actual
    reference = binding["container_id"] or binding["container_name"]
    attrs = await docker.inspect_container(reference)
    identity = container_identity(attrs, binding, volumes, require_running=running)
    return attrs, identity


async def _provision(scope, original, config, docker):
    binding = original
    try:
        if not binding["image_id"]:
            await _current(scope, binding)
            image = await docker.image_id(binding["image"])
            binding = await _advance(scope, binding, image_id=image)
        phases = (("data", "reserved"),) if binding["kind"] == "browser_profile" else (("workspace", "reserved"), ("data", "workspace_ready"))
        for role, preceding in phases:
            await _current(scope, binding)
            attrs = await docker.inspect_volume(binding[role + "_volume"])
            known = binding["volume_identities"].get(role)
            if known:
                if volume_identity(attrs, binding, role) != known:
                    raise PrivateDockerIdentityError("Private volume identity changed")
                continue
            if binding["provision_phase"] == preceding:
                if attrs is not None:
                    raise PrivateDockerIdentityError("An unrecorded private volume already exists")
                binding = await _advance(scope, binding, provision_phase=role + "_submitting")
                await _current(scope, binding)
                attrs = await docker.create_volume(binding[role + "_volume"], labels(binding, role=role))
            elif binding["provision_phase"] != role + "_submitting" or attrs is None:
                raise _error("UNAVAILABLE", "An uncertain volume creation cannot be repeated")
            identities = {**binding["volume_identities"], role: volume_identity(attrs, binding, role)}
            binding = await _advance(scope, binding, volume_identities=identities, provision_phase=role + "_ready")
        if not binding["container_id"]:
            await _current(scope, binding)
            attrs = await docker.inspect_container(binding["container_name"])
            if binding["provision_phase"] == "data_ready":
                if attrs is not None:
                    raise PrivateDockerIdentityError("An unrecorded private container already exists")
                binding = await _advance(scope, binding, provision_phase="container_submitting")
                key = decrypt_secret(binding["api_key_ciphertext"], _aad(binding), config.secret_key)
                await _current(scope, binding)
                attrs = await docker.create_container(binding, key)
            elif binding["provision_phase"] != "container_submitting" or attrs is None:
                raise _error("UNAVAILABLE", "An uncertain container creation cannot be repeated")
            cid, _, _ = container_identity(attrs, binding, binding["volume_identities"], require_running=False)
            binding = await _advance(scope, binding, container_id=cid, provision_phase="container_ready")
        await _current(scope, binding)
        attrs, _ = await _physical(binding, docker, running=False)
        if (attrs.get("State") or {}).get("Status") != "running":
            if binding["provision_phase"] != "container_ready":
                raise _error("UNAVAILABLE", "An uncertain container start cannot be repeated automatically")
            binding = await _advance(scope, binding, provision_phase="start_submitting")
            await _current(scope, binding)
            await docker.start_container(binding["container_id"])
        await _current(scope, binding)
        _, (_, port, proof) = await _physical(binding, docker, running=True)
        binding = await _advance(scope, binding, status="ready", provision_phase="ready", host_port=port,
            physical_digest=proof, claim_token=None, claim_expires_at=None, error_code=None)
        return _route(binding, config)
    except BaseException as exc:
        await _release_claim(binding, "identity_changed" if isinstance(exc, PrivateDockerIdentityError) else "provisioning_unconfirmed",
            blocked=isinstance(exc, PrivateDockerIdentityError))
        if isinstance(exc, PrivateDockerIdentityError):
            raise _error("IDENTITY_CHANGED", "Private Docker identity or isolation configuration changed") from None
        if isinstance(exc, PrivateDockerError):
            raise _error("UNAVAILABLE", "Private Docker provisioning needs read-only reconciliation", 503) from None
        raise


async def resolve_private_runtime(*, session_id, user_id, workspace_id=None, kind="sandbox", create=True, docker=None):
    config = _config(user_id, kind)
    scope = await private_session_scope(session_id=session_id, user_id=user_id, workspace_id=workspace_id)
    if scope is None:
        raise _error("SCOPE_INVALID", "This Session does not have a private runtime audience", 403)
    binding = await _load(scope, kind=kind)
    if binding is None and create:
        binding = await _reserve(scope, config, kind)
    if binding is None:
        raise _error("UNAVAILABLE", "No private runtime has been provisioned", 404)
    backend = docker or DockerPrivateBackend(config)
    if binding["status"] == "ready":
        return await validate_private_runtime(_route(binding, config), session_id=session_id, user_id=user_id,
            workspace_id=scope.workspace_id, kind=kind, docker=backend)
    if binding["status"] == "blocked" or not create:
        raise _error("UNAVAILABLE", "The private runtime is not ready; no shared runtime fallback is allowed")
    binding = await _claim(scope, binding, config)
    return await _provision(scope, binding, config, backend)


async def validate_private_runtime(route, *, session_id, user_id, workspace_id=None, kind="sandbox", docker=None):
    config = _config(user_id, kind)
    scope = await private_session_scope(session_id=session_id, user_id=user_id, workspace_id=workspace_id)
    if scope is None or not isinstance(route, PrivateRuntimeRoute):
        raise _error("SCOPE_INVALID", "A current private Session and fixed route are required", 403)
    binding = await _load(scope, binding_id=route.binding_id, kind=kind)
    if binding is None or _route(binding, config) != route:
        raise _error("IDENTITY_CHANGED", "The fixed private runtime route changed")
    try:
        _, (_, _, proof) = await _physical(binding, docker or DockerPrivateBackend(config), running=True)
    except PrivateDockerError:
        raise _error("IDENTITY_CHANGED", "The private runtime physical identity is unavailable or changed") from None
    if proof != binding["physical_digest"]:
        raise _error("IDENTITY_CHANGED", "The private runtime physical identity changed")
    # Do not hold a database snapshot/lock while waiting for Docker. This last
    # read detects revocation during the physical identity observation.
    current_config = _config(user_id, kind)
    after = await _load(scope, binding_id=route.binding_id, kind=kind)
    if not _same(after, binding) or _route(after, current_config) != route:
        raise _error("IDENTITY_CHANGED", "Private runtime authority changed during validation")
    return route
