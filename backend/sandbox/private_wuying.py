"""Pin an already enrolled actor on the configured Wuying Action Server.

Only the existing ECD route/channel and original Action Server are used.
There is no cloud provisioning, account creation, shared fallback or Docker IO.
Every validation reads current SQL/config, proves the guest identity, then
reads SQL/config again before returning the original route.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.config import get_config
from db.base import get_db_session
from db.models.private_runtime import PrivateRuntimeBinding
from sandbox.private_runtime import PrivateBrowserPin, PrivateRuntimeRoute, _config, _error, _load, _mode_enabled, _scope, _snapshot


PROVIDER = "private_wuying_v1"
PROTOCOL = "wuying_actor_uid_mount_v1"
SCOPE_HEADER = "X-OpenBox-Private-Scope"
ATTEMPT_HEADER = "X-OpenBox-Private-Attempt"
_CHECKS = {"legacy_executor_is_unprivileged", "legacy_file_worker", "actor_uid", "groups_empty",
           "no_new_privs", "capabilities_empty", "private_mount_namespace", "workspace_mount", "data_mount", "tmp_mount",
           "generic_cannot_read_actor_storage", "generic_cannot_read_browser_storage", "generic_cannot_read_control"}
_CHECKS |= {"actor_cannot_read_control_or_browser", "actor_cannot_signal_supervisor"}
_PUBLIC = {"version", "protocol", "id", "attempt_id", "scope_id", "workspace_id", "actor_user_id",
           "desktop_id", "region_id", "executor_uid", "browser_uid", "browser_resource_id", "identity_digest", "isolation_mode"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(",", ":"), default=lambda item: item.isoformat()).encode()).hexdigest()


def scope_id(scope):
    return hashlib.sha256(("openbox:private-wuying:v1\0" + scope.workspace_id + "\0" + scope.user_id).encode()).hexdigest()


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    host: str
    port: int
    api_key: str = field(repr=False)
    desktop_id: str
    region_id: str
    record_id: str | None
    authority_digest: str

    def public(self):
        return {"base_url": self.base_url, "desktop_id": self.desktop_id, "region_id": self.region_id,
                "record_id": self.record_id, "authority_digest": self.authority_digest}


async def endpoint(scope):
    """Read the original Wuying route without invoking lifecycle/recovery."""
    from db.repository.cloud_desktop_repo import cloud_desktop_repo, _CHANNEL_IDENTITY_FIELDS
    from sandbox.channel import route_for_record, action_key_hash, ChannelNotReady, ChannelConfigError
    settings = get_config()
    _config(scope.user_id)
    routing = settings.wuying_routing
    if routing == "shared":
        base = settings.wuying_endpoint.rstrip("/")
        key, desktop, region = settings.wuying_api_key, settings.wuying_desktop_id, settings.wuying_region_id
        record_id = None
        identity = {"routing": routing, "base_url": base, "key_hash": action_key_hash(key),
                    "desktop_id": desktop, "region_id": region}
    elif routing == "per_desktop":
        from sandbox.entitlement import require_sandbox_subscription
        await require_sandbox_subscription(scope.workspace_id)
        record = await cloud_desktop_repo.get_for_workspace(scope.workspace_id)
        if (record is None or record.get("status") != "running" or record.get("tunnel_state") != "up"
                or record.get("pool_state") != "assigned"):
            raise _error("UNAVAILABLE", "The original Wuying desktop channel is not ready")
        try:
            host, port, key = route_for_record(record)
        except (ChannelNotReady, ChannelConfigError):
            raise _error("UNAVAILABLE", "The original Wuying desktop channel is not ready") from None
        if action_key_hash(key) != record.get("action_api_key_hash"):
            raise _error("IDENTITY_CHANGED", "The original Wuying channel credential changed")
        base = f"http://{host}:{port}"
        desktop, region, record_id = record["desktop_id"], record["region_id"], record["id"]
        identity = {"routing": routing, "channel": {key: record[key] for key in _CHANNEL_IDENTITY_FIELDS}}
    else:
        raise _error("UNAVAILABLE", "Unsupported Wuying route configuration")
    try:
        parsed = urlsplit(base)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        valid = False
    if not valid or not key or not desktop or not region:
        raise _error("UNAVAILABLE", "The configured Wuying route is incomplete")
    return Endpoint(base, parsed.hostname, port, key, desktop, region, record_id, _digest(identity))


async def _request(original, path, *, scope, attempt=None, method="GET"):
    from sandbox.private_http import private_http_transport
    headers = {"X-API-Key": original.api_key, SCOPE_HEADER: scope_id(scope)}
    if attempt is not None:
        headers[ATTEMPT_HEADER] = attempt
    try:
        async with private_http_transport(endpoint=original.base_url, api_key=original.api_key,
                scope=headers[SCOPE_HEADER], attempt=attempt) as transport:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False,
                                         transport=transport) as client:
                response = await client.request(method, original.base_url + path, headers=headers)
                if response.status_code != 200 or len(response.content) > 64 * 1024:
                    raise _error("UNAVAILABLE", "The Wuying guest did not verify the original actor execution identity")
                result = response.json()
    except (httpx.RequestError, ValueError):
        raise _error("UNAVAILABLE", "The Wuying actor execution service is unavailable", 503) from None
    if not isinstance(result, dict):
        raise _error("UNAVAILABLE", "The Wuying actor execution response is invalid")
    return result


def _proof(value, scope, original):
    public = {key: value.get(key) for key in _PUBLIC}
    report = value.get("isolation")
    checks = report.get("checks") if isinstance(report, dict) else None
    if (set(value) - (_PUBLIC | {"isolation", "status"}) or public["version"] != 1
            or type(public["version"]) is not int or public["protocol"] != PROTOCOL
            or public["scope_id"] != scope_id(scope) or public["workspace_id"] != scope.workspace_id
            or public["actor_user_id"] != scope.user_id or public["desktop_id"] != original.desktop_id
            or public["region_id"] != original.region_id or public["isolation_mode"] != "guest_uid_mount"
            or any(not isinstance(public[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", public[key])
                   for key in ("id", "attempt_id"))
            or any(not isinstance(public[key], str) or not re.fullmatch(r"[0-9a-f]{64}", public[key])
                   for key in ("identity_digest", "browser_resource_id"))
            or any(type(public[key]) is not int or public[key] <= 0 for key in ("executor_uid", "browser_uid"))
            or public["executor_uid"] == public["browser_uid"]
            or not isinstance(report, dict) or set(report) != {"mode", "verification", "checks"}
            or report["mode"] != "guest_uid_mount" or report["verification"] != "passed"
            or not isinstance(checks, dict) or set(checks) != _CHECKS or any(checks[key] is not True for key in _CHECKS)):
        raise _error("ISOLATION_UNVERIFIED", "The Wuying guest has not proved actor execution and file isolation")
    return public


async def observe(scope, original, expected=None):
    path = "/private-runtime/resolve" if expected is None else "/private-runtime/" + expected["id"] + "/identity"
    raw = await _request(original, path, scope=scope, attempt=expected["attempt_id"] if expected else None)
    public = _proof(raw, scope, original)
    if expected is not None and public != expected:
        raise _error("IDENTITY_CHANGED", "The original Wuying guest actor identity changed")
    # No SQL snapshot/lock is held over the remote request. Do not disclose
    # its result after a committed membership or original-channel change.
    async with get_db_session() as db:
        if await _scope(db, scope.session_id, scope.user_id, scope.workspace_id) != scope:
            raise _error("SCOPE_INVALID", "The private Session scope changed", 403)
    if await endpoint(scope) != original:
        raise _error("IDENTITY_CHANGED", "The original Wuying channel changed during validation")
    return public


def _route(row, original):
    guest = row["provider_identity"]["guest_binding"]
    prefix = original.base_url + "/private-runtime/" + guest["id"]
    return PrivateRuntimeRoute(row["id"], row["kind"], row["provider"], row["workspace_id"], row["actor_user_id"],
        row["attempt_id"], row["revision"], row["container_id"], row["container_name"], row["image"], row["image_id"],
        row["created_at"].replace(tzinfo=timezone.utc) if row["created_at"].tzinfo is None else row["created_at"],
        original.host, original.port, row["route_key"], original.api_key,
        guest["browser_resource_id"] if row["kind"] == "browser_profile" else None,
        row["isolation_mode"], prefix + ("/browser" if row["kind"] == "browser_profile" else ""),
        guest["scope_id"], original.desktop_id, original.region_id, guest["id"], guest["attempt_id"], row["provider_identity"])


def _matches(row, original, guest):
    return (row is not None and row["provider"] == PROVIDER
        and row["provider_identity"] == {"endpoint": original.public(), "guest_binding": guest}
        and row["physical_digest"] == _digest({"endpoint": original.public(), "guest_binding": guest})
        and row["api_key_hash"] == hashlib.sha256(original.api_key.encode()).hexdigest())


async def _pin(scope, original, guest, kind):
    for _ in range(2):
        try:
            async with get_db_session() as db:
                if await _scope(db, scope.session_id, scope.user_id, scope.workspace_id, lock=True) != scope:
                    raise _error("SCOPE_INVALID", "The private Session scope changed", 403)
                row = await db.scalar(select(PrivateRuntimeBinding).where(
                    PrivateRuntimeBinding.workspace_id == scope.workspace_id,
                    PrivateRuntimeBinding.actor_user_id == scope.user_id,
                    PrivateRuntimeBinding.kind == kind, PrivateRuntimeBinding.provider == PROVIDER))
                if row is not None:
                    return _snapshot(row), False
                stamp, identity = datetime.now(timezone.utc), "wpr_" + uuid4().hex
                proof = {"endpoint": original.public(), "guest_binding": guest}
                row = PrivateRuntimeBinding(id=identity, workspace_id=scope.workspace_id, actor_user_id=scope.user_id,
                    provider=PROVIDER, kind=kind, isolation_mode="guest_uid_mount" if kind == "sandbox" else "wuying_guest_uid",
                    status="ready" if kind == "sandbox" else "reserved", attempt_id=guest["attempt_id"], revision=1,
                    provision_phase="ready" if kind == "sandbox" else "guest_verified", container_id=identity,
                    container_name="wuying-actor-" + identity, workspace_volume=None, data_volume=None,
                    volume_identities={}, image="wuying:" + original.desktop_id, image_id=guest["identity_digest"],
                    host_port=original.port, route_key="private:" + identity, api_key_ciphertext="",
                    api_key_hash=hashlib.sha256(original.api_key.encode()).hexdigest(), provider_identity=proof,
                    physical_digest=_digest(proof), created_at=stamp, updated_at=stamp)
                db.add(row)
                await db.flush()
                return _snapshot(row), True
        except IntegrityError:
            continue
    raise _error("PENDING", "Another request is pinning the Wuying actor identity")


async def _browser_ready(scope, row, original, guest, *, create):
    if row["status"] == "ready":
        return row
    if row["status"] != "reserved" or not create:
        raise _error("UNAVAILABLE", "The original Wuying browser has not been prepared")
    # Only the winner of this durable transition may send prepare. Recovery
    # after a lost response is a read of the original finite service status.
    claimed = False
    async with get_db_session() as db:
        if await _scope(db, scope.session_id, scope.user_id, scope.workspace_id, lock=True) != scope:
            raise _error("SCOPE_INVALID", "The private Session scope changed", 403)
        changed = await db.execute(update(PrivateRuntimeBinding).where(
            PrivateRuntimeBinding.id == row["id"], PrivateRuntimeBinding.status == "reserved",
            PrivateRuntimeBinding.revision == row["revision"], PrivateRuntimeBinding.provision_phase == "guest_verified")
            .values(provision_phase="browser_submitting", updated_at=datetime.now(timezone.utc)))
        claimed = changed.rowcount == 1
    await observe(scope, original, guest)
    path = "/private-runtime/" + guest["id"] + "/browser/"
    response = await _request(original, path + ("prepare" if claimed else "v1/status"), scope=scope,
                              attempt=guest["attempt_id"], method="POST" if claimed else "GET")
    # The finite browser/control layer pins its complete identity. Here only
    # prove that prepare/status belonged to this exact guest actor.
    if (response.get("guest_binding") != guest or not isinstance(response.get("identity"), dict)
            or response["identity"].get("resource_id") != guest["browser_resource_id"]
            or response.get("browser_live") is not True or not isinstance(response.get("isolation"), dict)
            or response["isolation"].get("mode") != "wuying_guest_uid"
            or response["isolation"].get("verification") != "passed"):
        raise _error("IDENTITY_CHANGED", "The original Wuying browser preparation was not confirmed")
    await observe(scope, original, guest)
    async with get_db_session() as db:
        if await _scope(db, scope.session_id, scope.user_id, scope.workspace_id, lock=True) != scope:
            raise _error("SCOPE_INVALID", "The private Session scope changed", 403)
        changed = await db.execute(update(PrivateRuntimeBinding).where(
            PrivateRuntimeBinding.id == row["id"], PrivateRuntimeBinding.status == "reserved",
            PrivateRuntimeBinding.revision == row["revision"], PrivateRuntimeBinding.provision_phase == "browser_submitting",
            PrivateRuntimeBinding.physical_digest == row["physical_digest"])
            .values(status="ready", provision_phase="ready", revision=row["revision"] + 1, updated_at=datetime.now(timezone.utc)))
    current = await _load(scope, binding_id=row["id"], kind=row["kind"])
    if current is None or current["status"] != "ready" or not _matches(current, original, guest):
        raise _error("IDENTITY_CHANGED", "Wuying browser authority changed while preparing")
    return current


async def resolve(scope, *, kind, create):
    row = await _load(scope, kind=kind)
    original = await endpoint(scope)
    expected = row["provider_identity"].get("guest_binding") if row is not None else None
    if row is None and not create:
        raise _error("UNAVAILABLE", "No Wuying actor identity has been pinned", 404)
    if row is not None and (not isinstance(expected, dict) or not _matches(row, original, expected)):
        raise _error("IDENTITY_CHANGED", "The original Wuying route changed")
    guest = await observe(scope, original, expected)
    if row is None:
        row, _ = await _pin(scope, original, guest, kind)
    if not _matches(row, original, guest):
        raise _error("IDENTITY_CHANGED", "The original Wuying actor binding changed")
    if kind == "browser_profile":
        row = await _browser_ready(scope, row, original, guest, create=create)
    if row["status"] != "ready":
        raise _error("UNAVAILABLE", "The original Wuying actor binding is unavailable")
    return await validate(scope, _route(row, original), kind=kind)


async def validate(scope, route, *, kind):
    row = await _load(scope, binding_id=route.binding_id, kind=kind)
    original = await endpoint(scope)
    guest = row["provider_identity"].get("guest_binding") if row else None
    if (row is None or row["status"] != "ready" or not isinstance(guest, dict)
            or not _matches(row, original, guest) or _route(row, original) != route):
        raise _error("IDENTITY_CHANGED", "The fixed Wuying actor route changed")
    await observe(scope, original, guest)
    current = await _load(scope, binding_id=route.binding_id, kind=kind)
    if current != row:
        raise _error("IDENTITY_CHANGED", "Wuying actor authority changed during validation")
    _config(scope.user_id, kind)
    return route


async def read_browser_pin(scope, *, binding_id, revision):
    """SQL-only before/after companion to an actual finite browser status.

    The browser mount checks its root registry on both sides of the request;
    the caller validates that response's full guest and browser proof. This
    function does not replace the generic execution/file identity probe.
    """
    config = _config(scope.user_id, "browser_profile")
    row = await _load(scope, binding_id=binding_id, kind="browser_profile")
    original = await endpoint(scope)
    provider_identity = row["provider_identity"] if row else None
    guest = provider_identity.get("guest_binding") if isinstance(provider_identity, dict) else None
    if (row is None or row["status"] != "ready" or row["revision"] != revision
            or row["provision_phase"] != "ready" or not isinstance(guest, dict)
            or not _matches(row, original, guest)):
        raise _error("IDENTITY_CHANGED", "The fixed Wuying browser route changed")
    _mode_enabled(row, config)
    return PrivateBrowserPin(_route(row, original), scope, _digest(row))


async def revalidate_browser_pin(pin):
    current = await read_browser_pin(pin._scope, binding_id=pin.route.binding_id, revision=pin.route.revision)
    if current.route != pin.route or current._source_hash != pin._source_hash:
        raise _error("IDENTITY_CHANGED", "Wuying browser authority changed during its status request")
