"""Real private-runtime SQL/state machine; only the local Docker SDK is fake."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from docker.errors import NotFound
import pytest
from sqlalchemy import func, select

from core.config import OpenBoxConfig, PrivateRuntimeConfig
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.private_runtime import PrivateRuntimeBinding
from db.models.session import Session
from db.models.user import User
from db.models.workspace import WorkspaceMember
from sandbox import private_runtime as runtime
from sandbox.private_docker import DockerPrivateBackend, PrivateDockerError
from session.session import create_session
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


class FakeDockerSDK:
    """A Docker API boundary, not a replacement for provisioning/validation.

    Inspection uses the actual API field shapes. Tests may mutate returned
    physical state to model replacements, unsafe mounts and lost responses.
    """
    def __init__(self):
        self.calls, self.volume_rows, self.container_rows = [], {}, {}
        self.image = "sha256:" + "a" * 64
        self.image_ids = {}
        self.images = SimpleNamespace(get=self.image_get)
        self.volumes = SimpleNamespace(get=self.volume_get, create=self.volume_create)
        self.containers = SimpleNamespace(get=self.container_get, create=self.container_create)
        self.fail_before, self.fail_after = None, None

    def _call(self, operation, reference):
        self.calls.append((operation, reference))
        if self.fail_before == operation:
            self.fail_before = None
            raise RuntimeError("fixture connection interrupted before daemon mutation")

    def _returned(self, operation):
        if self.fail_after == operation:
            self.fail_after = None
            raise RuntimeError("fixture response lost after daemon mutation")

    def image_get(self, reference):
        self._call("image.get", reference)
        return SimpleNamespace(id=self.image_ids.get(reference, self.image))

    def volume_get(self, name):
        self._call("volume.get", name)
        if name not in self.volume_rows:
            raise NotFound("fixture volume absent")
        return SimpleNamespace(attrs=deepcopy(self.volume_rows[name]))

    def volume_create(self, *, name, driver, labels):
        self._call("volume.create", name)
        if name not in self.volume_rows:
            self.volume_rows[name] = dict(Name=name, Driver=driver, Labels=deepcopy(labels), Scope="local",
                Options=None, CreatedAt=datetime.now(timezone.utc).isoformat(),
                Mountpoint="/var/lib/docker/volumes/" + name + "/_data")
        self._returned("volume.create")
        return SimpleNamespace(attrs=deepcopy(self.volume_rows[name]))

    def container_get(self, reference):
        self._call("container.get", reference)
        found = [row for cid, row in self.container_rows.items()
                 if cid == reference or row["Name"].lstrip("/") == reference or (len(reference) >= 12 and cid.startswith(reference))]
        if len(found) != 1:
            raise NotFound("fixture container absent")
        row = found[0]
        return SimpleNamespace(attrs=deepcopy(row), start=lambda: self.container_start(row["Id"]))

    def container_create(self, image, **spec):
        self._call("container.create", spec["name"])
        assert not any(row["Name"] == "/" + spec["name"] for row in self.container_rows.values())
        cid = uuid4().hex + uuid4().hex
        binds = [name + ":" + options["bind"] + ":" + options["mode"] for name, options in spec["volumes"].items()]
        row = {"Id": cid, "Name": "/" + spec["name"], "Created": datetime.now(timezone.utc).isoformat(), "Image": image,
            "Config": {"Image": image, "Cmd": spec["command"], "Entrypoint": spec.get("entrypoint"), "User": "",
                "Labels": deepcopy(spec["labels"]), "Env": [name + "=" + value for name, value in spec["environment"].items()]},
            "HostConfig": {"ReadonlyRootfs": spec["read_only"], "Privileged": False,
                "NetworkMode": spec["network_mode"], "IpcMode": spec["ipc_mode"], "CgroupnsMode": spec["cgroupns"],
                "PidMode": "", "UTSMode": "", "UsernsMode": "", "Devices": [], "DeviceRequests": None,
                "DeviceCgroupRules": None, "VolumesFrom": None, "Links": None, "Mounts": None,
                "CapDrop": spec["cap_drop"], "CapAdd": spec["cap_add"], "SecurityOpt": spec["security_opt"],
                "Init": spec["init"], "Tmpfs": deepcopy(spec["tmpfs"]), "Binds": binds,
                "PortBindings": {key: [{"HostIp": value[0], "HostPort": "" if value[1] is None else str(value[1])}]
                    for key, value in spec["ports"].items()},
                "Memory": 1024 ** 3, "CpuPeriod": spec["cpu_period"], "CpuQuota": spec["cpu_quota"], "PidsLimit": spec["pids_limit"]},
            "Mounts": [{"Type": "volume", "Name": name, "Source": self.volume_rows[name]["Mountpoint"],
                "Destination": options["bind"], "Mode": options["mode"], "RW": True, "Propagation": ""}
                for name, options in spec["volumes"].items()],
            "State": {"Status": "created"}, "NetworkSettings": {"Networks": {"bridge": {}},
                "Ports": {port: None for port in spec["ports"]}}}
        self.container_rows[cid] = row
        self._returned("container.create")
        return SimpleNamespace(attrs=deepcopy(row))

    def container_start(self, cid):
        self._call("container.start", cid)
        self.container_rows[cid]["State"]["Status"] = "running"
        self.container_rows[cid]["NetworkSettings"]["Ports"] = {
            port: [{"HostIp": "127.0.0.1", "HostPort": str(19000 + list(self.container_rows).index(cid))}]
            for port in self.container_rows[cid]["HostConfig"]["PortBindings"]}
        self._returned("container.start")


@pytest.fixture
async def private_world(monkeypatch):
    pytest.skip("Private Docker provisioning was retired; current Wuying coverage uses wuying_world. Historical evidence is retained.")
    owner, peer, workspace = await accounts()
    session = await create_session(user_id=owner, workspace_id=workspace, visibility="private")
    peer_session = await create_session(user_id=peer, workspace_id=workspace, visibility="private")
    shared = await create_session(user_id=owner, workspace_id=workspace)
    config = OpenBoxConfig(private_runtime=PrivateRuntimeConfig(enabled=True, allowed_user_ids=[owner, peer], secret_key="73" * 32))
    monkeypatch.setattr("core.config.get_config", lambda: config)
    daemon = FakeDockerSDK()
    backend = DockerPrivateBackend(config.private_runtime, client=daemon)
    monkeypatch.setattr(runtime, "DockerPrivateBackend", lambda *_: backend)
    return SimpleNamespace(owner=owner, peer=peer, workspace=workspace, session=session, peer_session=peer_session,
        shared=shared, config=config, daemon=daemon, backend=backend)


async def resolve(world, **changes):
    return await runtime.resolve_private_runtime(session_id=world.session.id, user_id=world.owner,
        workspace_id=world.workspace, **changes)


async def validate(world, route, **changes):
    return await runtime.validate_private_runtime(route, **{
        "session_id": world.session.id, "user_id": world.owner, "workspace_id": world.workspace, **changes})


async def test_actor_private_binding_survives_engine_reopen_and_never_adopts_shared_resources(private_world):
    w = private_world
    route = await resolve(w)
    assert route.host == "127.0.0.1" and len(route.container_id) == 64
    assert route.api_key not in repr(route)
    before = [call for call in w.daemon.calls if call[0].endswith(("create", "start"))]
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    assert await resolve(w, create=False) == await validate(w, route) == route
    child = await create_session(user_id=w.owner, workspace_id=w.workspace, parent_id=w.session.id)
    assert await validate(w, route, session_id=child.id) == route
    assert [call for call in w.daemon.calls if call[0].endswith(("create", "start"))] == before
    other = await runtime.resolve_private_runtime(session_id=w.peer_session.id, user_id=w.peer, workspace_id=w.workspace)
    assert other.binding_id != route.binding_id and other.container_id != route.container_id and other.api_key != route.api_key
    async with get_db_session() as db:
        rows = list((await db.scalars(select(PrivateRuntimeBinding).where(PrivateRuntimeBinding.workspace_id == w.workspace))).all())
        assert len(rows) == 2 and len({row.workspace_volume for row in rows} | {row.data_volume for row in rows}) == 4
        assert all(route.api_key not in row.api_key_ciphertext for row in rows)
    for alias in (route.binding_id, route.route_key, route.container_id, route.container_id[:12], route.name):
        found = await runtime.find_private_binding(alias)
        assert found["id"] == route.binding_id and not any("key" in key and key != "route_key" for key in found)


@pytest.mark.parametrize("mode", ["disabled", "unlisted", "peer", "shared"])
async def test_no_docker_or_binding_without_explicit_current_private_authority(private_world, mode):
    w = private_world
    if mode == "disabled": w.config.private_runtime.enabled = False
    if mode == "unlisted": w.config.private_runtime.allowed_user_ids = []
    args = {"session_id": w.shared.id if mode == "shared" else w.session.id,
            "user_id": w.peer if mode == "peer" else w.owner, "workspace_id": w.workspace}
    with pytest.raises(runtime.PrivateRuntimeError):
        await runtime.resolve_private_runtime(**args)
    assert not w.daemon.calls
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(PrivateRuntimeBinding).where(
            PrivateRuntimeBinding.workspace_id == w.workspace)) == 0


@pytest.mark.parametrize("change", ["membership", "session_deleted", "shared", "user_inactive", "user_deleted", "binding_revision"])
async def test_retained_route_is_rechecked_against_current_sql(private_world, change):
    w = private_world
    route = await resolve(w)
    before = len(w.daemon.calls)
    async with get_db_session() as db:
        if change == "membership": (await db.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
        if change == "session_deleted": (await db.get(Session, w.session.id)).is_deleted = True
        if change == "shared": (await db.get(Session, w.session.id)).visibility = "workspace"
        if change == "user_inactive": (await db.get(User, w.owner)).is_active = False
        if change == "user_deleted": (await db.get(User, w.owner)).is_deleted = True
        if change == "binding_revision": (await db.get(PrivateRuntimeBinding, route.binding_id)).revision += 1
    with pytest.raises(runtime.PrivateRuntimeError):
        await validate(w, route)
    assert len(w.daemon.calls) == before


@pytest.mark.parametrize("change", ["key", "labels", "container", "volume", "mount", "port", "privileged", "namespace"])
async def test_physical_replacement_or_unsafe_runtime_is_never_accepted(private_world, change):
    w = private_world
    route = await resolve(w)
    attrs = w.daemon.container_rows[route.container_id]
    if change == "key": attrs["Config"]["Env"][0] = "SESSION_API_KEY=replacement"
    if change == "labels": attrs["Config"]["Labels"]["openbox.private/actor-id"] = w.peer
    if change == "container": attrs["Id"] = "f" * 64
    if change == "volume": next(iter(w.daemon.volume_rows.values()))["CreatedAt"] = "replacement"
    if change == "mount": attrs["Mounts"][0]["Type"] = "bind"
    if change == "port": attrs["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostIp"] = "0.0.0.0"
    if change == "privileged": attrs["HostConfig"]["Privileged"] = True
    if change == "namespace": attrs["HostConfig"]["PidMode"] = "host"
    created = [call for call in w.daemon.calls if call[0].endswith("create")]
    with pytest.raises(runtime.PrivateRuntimeError) as error:
        await validate(w, route)
    assert error.value.code == "PRIVATE_RUNTIME_IDENTITY_CHANGED"
    with pytest.raises(runtime.PrivateRuntimeError):
        await resolve(w)
    assert [call for call in w.daemon.calls if call[0].endswith("create")] == created


@pytest.mark.parametrize("operation", ["volume.create", "container.create", "container.start"])
async def test_lost_response_recovers_only_the_same_proven_attempt(private_world, operation):
    w = private_world
    w.daemon.fail_after = operation
    with pytest.raises(runtime.PrivateRuntimeError):
        await resolve(w)
    failed_count = sum(call[0] == operation for call in w.daemon.calls)
    route = await resolve(w)
    assert await validate(w, route) == route
    # A lost first volume response still needs one distinct data volume.
    assert sum(call[0] == operation for call in w.daemon.calls) == (2 if operation == "volume.create" else failed_count)
    assert len(w.daemon.container_rows) == 1 and len(w.daemon.volume_rows) == 2


@pytest.mark.parametrize("operation", ["volume.create", "container.create", "container.start"])
async def test_unknown_creation_without_evidence_cannot_repeat_or_replace(private_world, operation):
    w = private_world
    w.daemon.fail_before = operation
    with pytest.raises(runtime.PrivateRuntimeError):
        await resolve(w)
    before = [call for call in w.daemon.calls if call[0].endswith(("create", "start"))]
    with pytest.raises(runtime.PrivateRuntimeError):
        await resolve(w)
    assert [call for call in w.daemon.calls if call[0].endswith(("create", "start"))] == before


async def test_revocation_during_physical_check_rejects_the_held_route(private_world, monkeypatch):
    w = private_world
    route = await resolve(w)
    inspect = w.backend.inspect_container
    async def revoke_after_inspection(reference):
        result = await inspect(reference)
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
        return result
    monkeypatch.setattr(w.backend, "inspect_container", revoke_after_inspection)
    with pytest.raises(runtime.PrivateRuntimeError) as error:
        await validate(w, route)
    assert error.value.code == "PRIVATE_RUNTIME_SCOPE_INVALID"


async def test_concurrent_resolvers_create_one_binding_and_one_physical_attempt(private_world):
    w = private_world
    results = await asyncio.gather(resolve(w), resolve(w), return_exceptions=True)
    assert any(isinstance(result, runtime.PrivateRuntimeRoute) for result in results)
    assert all(isinstance(result, (runtime.PrivateRuntimeRoute, runtime.PrivateRuntimeError)) for result in results)
    final = await resolve(w)
    assert all(result == final for result in results if isinstance(result, runtime.PrivateRuntimeRoute))
    assert len(w.daemon.container_rows) == 1 and len(w.daemon.volume_rows) == 2
    assert sum(operation == "container.create" for operation, _ in w.daemon.calls) == 1


async def test_forged_route_is_not_authority(private_world):
    w = private_world
    route = await resolve(w)
    before = len(w.daemon.calls)
    for changed in (replace(route, port=route.port + 1), replace(route, actor_user_id=w.peer),
                    replace(route, api_key="forged"), replace(route, host="remote.invalid")):
        with pytest.raises(runtime.PrivateRuntimeError):
            await validate(w, changed)
    assert len(w.daemon.calls) == before


async def test_unordered_docker_inspect_fields_preserve_identity_but_not_changed_mounts(private_world):
    w = private_world
    route = await resolve(w)
    attrs = w.daemon.container_rows[route.container_id]
    for _ in range(3):
        attrs["Mounts"].reverse()
        for key in ("Binds", "CapAdd", "CapDrop", "SecurityOpt"):
            attrs["HostConfig"][key].reverse()
        assert await validate(w, route) == route
        assert await resolve(w, create=False) == route
    # Canonicalization only removes order variance, never a field or value.
    attrs["Mounts"][0]["RW"] = False
    with pytest.raises(runtime.PrivateRuntimeError) as error:
        await validate(w, route)
    assert error.value.code == "PRIVATE_RUNTIME_IDENTITY_CHANGED"


async def test_revocation_during_volume_creation_stops_all_later_actions(private_world, monkeypatch):
    w = private_world
    create = w.backend.create_volume
    async def revoked(name, properties):
        result = await create(name, properties)
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
        return result
    monkeypatch.setattr(w.backend, "create_volume", revoked)
    with pytest.raises(runtime.PrivateRuntimeError) as error:
        await resolve(w)
    assert error.value.code == "PRIVATE_RUNTIME_SCOPE_INVALID"
    assert len(w.daemon.volume_rows) == 1 and not w.daemon.container_rows
    async with get_db_session() as db:
        binding = await db.scalar(select(PrivateRuntimeBinding).where(PrivateRuntimeBinding.workspace_id == w.workspace))
        assert binding.provision_phase == "workspace_submitting" and binding.container_id is None


async def test_unrecorded_volume_and_replaced_uncertain_container_are_not_adopted(private_world):
    w = private_world
    scope = await runtime.private_session_scope(session_id=w.session.id, user_id=w.owner)
    binding = await runtime._reserve(scope, w.config.private_runtime)
    from sandbox.private_docker import labels
    w.daemon.volume_create(name=binding["workspace_volume"], driver="local", labels=labels(binding, role="workspace"))
    with pytest.raises(runtime.PrivateRuntimeError) as error:
        await resolve(w)
    assert error.value.code == "PRIVATE_RUNTIME_IDENTITY_CHANGED" and not w.daemon.container_rows
    async with get_db_session() as db:
        assert (await db.get(PrivateRuntimeBinding, binding["id"])).status == "blocked"


async def test_uncertain_created_container_with_changed_identity_is_blocked(private_world):
    w = private_world
    w.daemon.fail_after = "container.create"
    with pytest.raises(runtime.PrivateRuntimeError):
        await resolve(w)
    next(iter(w.daemon.container_rows.values()))["Config"]["Labels"]["openbox.private/attempt-id"] = "replacement"
    before = list(w.daemon.calls)
    with pytest.raises(runtime.PrivateRuntimeError) as error:
        await resolve(w)
    assert error.value.code == "PRIVATE_RUNTIME_IDENTITY_CHANGED"
    assert not any(operation in {"container.create", "container.start"} for operation, _ in w.daemon.calls[len(before):])
