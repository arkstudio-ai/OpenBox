"""Local Docker boundary for newly provisioned, actor-private sandboxes.

No discovery/adoption by legacy names, image pulls, removal, host mounts or
host namespaces are supported. Daemon I/O is the only replaceable test seam.
"""
import asyncio
import hashlib
import json
import re


LABEL_PREFIX = "openbox.private/"
NAME_PREFIX = "openbox-private-"
ACTION_PORT = "8000/tcp"
CAPABILITIES = {"SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "FOWNER"}
TMPFS = {
    "/tmp": "rw,nosuid,nodev,mode=1777",
    "/run": "rw,nosuid,nodev,mode=0755",
    "/home/sandbox": "rw,nosuid,nodev,uid=1000,gid=1000,mode=0700",
}
COMMAND = ["python", "/opt/action_server/action_server.py", "--port", "8000"]


class PrivateDockerError(RuntimeError):
    pass


class PrivateDockerIdentityError(PrivateDockerError):
    pass


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def labels(binding, *, role=None):
    result = {LABEL_PREFIX + key: binding[field] for key, field in (
        ("runtime-id", "id"), ("attempt-id", "attempt_id"), ("kind", "kind"),
        ("workspace-id", "workspace_id"), ("actor-id", "actor_user_id"))}
    if role:
        result[LABEL_PREFIX + "volume-role"] = role
    return result


def volume_roles(binding):
    return ("workspace", "data")


def container_spec(binding):
    _require(binding["kind"] == "sandbox")
    _require(binding["isolation_mode"] == "process_uid")
    return {"port": ACTION_PORT, "key_env": "SESSION_API_KEY", "command": COMMAND, "tmpfs": TMPFS, "capabilities": CAPABILITIES,
        "environment": {"OPENBOX_EXECUTOR_USER": "sandbox",
                        "OPENBOX_RESOURCE_CONTROL_DB": "/data/openbox-control/control.sqlite3"}}


def _require(condition):
    if not condition:
        raise PrivateDockerIdentityError("Private Docker identity or isolation configuration changed")


def volume_identity(attrs, binding, role):
    _require(isinstance(attrs, dict))
    expected = binding[role + "_volume"]
    _require(attrs.get("Name") == expected and expected.startswith(NAME_PREFIX))
    _require(attrs.get("Driver") == "local" and attrs.get("Scope") == "local")
    _require(not attrs.get("Options") and attrs.get("Labels") == labels(binding, role=role))
    _require(isinstance(attrs.get("CreatedAt"), str) and bool(attrs["CreatedAt"]))
    _require(isinstance(attrs.get("Mountpoint"), str) and bool(attrs["Mountpoint"]))
    return {key: attrs.get(key) for key in ("Name", "Driver", "Scope", "CreatedAt", "Mountpoint", "Labels", "Options")}


def container_identity(attrs, binding, volumes, *, require_running):
    _require(isinstance(attrs, dict))
    spec = container_spec(binding)
    roles = volume_roles(binding)
    _require(set(volumes) == set(roles))
    cid = attrs.get("Id")
    _require(isinstance(cid, str) and re.fullmatch(r"[0-9a-f]{64}", cid))
    _require(not binding.get("container_id") or binding["container_id"] == cid)
    _require(attrs.get("Name") == "/" + binding["container_name"])
    config, host = attrs.get("Config") or {}, attrs.get("HostConfig") or {}
    _require(attrs.get("Image") == binding["image_id"] and config.get("Image") == binding["image_id"])
    _require(config.get("Cmd") == spec["command"] and not config.get("Entrypoint"))
    _require(isinstance(attrs.get("Created"), str) and bool(attrs["Created"]))
    _require(config.get("User") in (None, "", "0", "root"))
    current_labels = config.get("Labels") or {}
    _require({k: v for k, v in current_labels.items() if k.startswith(LABEL_PREFIX)} == labels(binding))
    _require(not any(k.startswith("openbox.dev/") for k in current_labels))
    env = config.get("Env") or []
    keys = [value.split("=", 1)[1] for value in env if value.startswith(spec["key_env"] + "=")]
    _require(len(keys) == 1 and hashlib.sha256(keys[0].encode()).hexdigest() == binding["api_key_hash"])
    for key, value in spec["environment"].items():
        _require(env.count(key + "=" + value) == 1)
    _require(host.get("ReadonlyRootfs") is True and not host.get("Privileged"))
    _require(host.get("NetworkMode") == "bridge" and host.get("IpcMode") == "private")
    _require(host.get("CgroupnsMode") == "private")
    _require(all(host.get(key) in (None, "") for key in ("PidMode", "UTSMode", "UsernsMode")))
    _require(not any(host.get(key) for key in ("Devices", "DeviceRequests", "DeviceCgroupRules", "VolumesFrom", "Links")))
    _require(set(host.get("CapDrop") or []) == {"ALL"} and set(host.get("CapAdd") or []) == spec["capabilities"])
    _require(set(host.get("SecurityOpt") or []) == {"no-new-privileges:true"})
    _require(host.get("Init") is True and host.get("Tmpfs") == spec["tmpfs"])
    _require(set(host.get("Binds") or []) == {binding[role + "_volume"] + ":/" + role + ":rw" for role in roles})
    _require(not host.get("Mounts"))  # SDK Binds above are the only mount configuration.
    mounts = attrs.get("Mounts") or []
    _require(len(mounts) == len(roles))
    for role in roles:
        matching = [entry for entry in mounts if entry.get("Destination") == "/" + role]
        _require(len(matching) == 1)
        mount = matching[0]
        _require(mount.get("Type") == "volume" and mount.get("Name") == binding[role + "_volume"])
        _require(mount.get("Source") == volumes[role]["Mountpoint"] and mount.get("RW") is True)
        _require(mount.get("Propagation") in (None, "", "rprivate"))
    port_bindings = host.get("PortBindings") or {}
    _require(set(port_bindings) == {spec["port"]})
    configured = port_bindings[spec["port"]]
    _require(isinstance(configured, list) and len(configured) == 1 and configured[0].get("HostIp") == "127.0.0.1")
    state = (attrs.get("State") or {}).get("Status")
    _require(state in (("running",) if require_running else ("created", "running", "exited")))
    network = attrs.get("NetworkSettings") or {}
    _require(set(network.get("Networks") or {}) <= {"bridge"})
    ports = network.get("Ports") or {}
    exposed = {key: value for key, value in ports.items() if value}
    port = None
    if state == "running":
        _require(set(exposed) == {spec["port"]})
        values = exposed[spec["port"]]
        _require(len(values) == 1 and values[0].get("HostIp") == "127.0.0.1")
        try:
            port = int(values[0]["HostPort"])
        except (ValueError, KeyError, TypeError):
            raise PrivateDockerIdentityError("Private Docker route is unavailable") from None
        _require(0 < port < 65536 and (not binding.get("host_port") or binding["host_port"] == port))
    # Docker inspect may reorder sets such as Mounts between reads. Retain
    # every field and duplicate while normalizing only order-free collections;
    # environment, DNS and other order-sensitive arrays remain untouched.
    canonical_host = dict(host)
    for key in ("Binds", "CapAdd", "CapDrop", "SecurityOpt"):
        if isinstance(canonical_host.get(key), list):
            canonical_host[key] = sorted(canonical_host[key])
    proof = {"id": cid, "created": attrs.get("Created"), "image": binding["image_id"], "labels": current_labels,
             "env_hash": digest(env), "host": canonical_host,
             "mounts": sorted(mounts, key=lambda item: item["Destination"]), "volumes": volumes, "port": port}
    return cid, port, digest(proof)


class DockerPrivateBackend:
    def __init__(self, config, *, client=None):
        self.config = config
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import docker
            self._client = docker.DockerClient(base_url=self.config.docker_host,
                timeout=self.config.operation_timeout_seconds)
        return self._client

    async def _call(self, function):
        try:
            return await asyncio.wait_for(asyncio.to_thread(function), self.config.operation_timeout_seconds + 1)
        except Exception:
            # Never expose a Docker request/error containing env credentials.
            raise PrivateDockerError("Private Docker operation did not return a verified result") from None

    async def image_id(self, reference):
        result = await self._call(lambda: self.client.images.get(reference).id)
        _require(isinstance(result, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", result))
        return result

    async def inspect_volume(self, name):
        def inspect():
            from docker.errors import NotFound
            try:
                return self.client.volumes.get(name).attrs
            except NotFound:
                return None
        return await self._call(inspect)

    async def create_volume(self, name, properties):
        return await self._call(lambda: self.client.volumes.create(name=name, driver="local", labels=properties).attrs)

    async def inspect_container(self, reference):
        def inspect():
            from docker.errors import NotFound
            try:
                return self.client.containers.get(reference).attrs
            except NotFound:
                return None
        return await self._call(inspect)

    async def create_container(self, binding, api_key):
        spec = container_spec(binding)
        return await self._call(lambda: self.client.containers.create(
            binding["image_id"], name=binding["container_name"], detach=True, command=spec["command"], entrypoint=[],
            environment={spec["key_env"]: api_key, **spec["environment"]},
            ports={spec["port"]: ("127.0.0.1", None)},
            volumes={binding[role + "_volume"]: {"bind": "/" + role, "mode": "rw"} for role in volume_roles(binding)},
            network_mode="bridge", ipc_mode="private", cgroupns="private", read_only=True,
            cap_drop=["ALL"], cap_add=sorted(spec["capabilities"]), security_opt=["no-new-privileges:true"],
            tmpfs=spec["tmpfs"], init=True, mem_limit="1g", cpu_period=100000, cpu_quota=100000,
            pids_limit=256, labels=labels(binding),
        ).attrs)

    async def start_container(self, container_id):
        await self._call(lambda: self.client.containers.get(container_id).start())
