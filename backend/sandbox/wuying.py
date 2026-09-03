"""WUYING (无影云电脑) sandbox provider.

Unlike the Docker and Kubernetes providers, this one does not own a container
lifecycle. The sandbox is a long-lived Alibaba Cloud WUYING cloud desktop that
was provisioned out of band; the action server runs there as a systemd unit and
is reached over a tunnel endpoint (``wuying_endpoint``).

Consequences that matter:

* ``create_container`` is idempotent — it returns the one desktop we know about.
* ``delete_container`` / ``stop_container`` are deliberate no-ops. The session
  manager destroys a container once its last session is released; doing that to
  someone's cloud desktop would be destructive and is never what we want here.
* Isolation between sessions is by working directory only
  (``/workspace/sessions/<id>``), the same as the shared-container path the
  Docker provider already takes. There is no per-session boundary beyond that.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from core.log import create_logger
from models.container import ContainerInfo, ContainerStatus
from sandbox.channel import ChannelNotReady, route_for_record, wuying_channel
from sandbox.client import USER_SCOPE_HEADER, user_scope_for
from sandbox.provider import SandboxProvider

log = create_logger("sandbox.wuying")

CONTAINER_ID = "wuying-desktop"


class WuyingProvider(SandboxProvider):
    """Shared legacy route or one independently authenticated route per owner."""

    supports_build = False
    owns_containers = False
    # One desktop for everyone: this provider reads a single tunnel endpoint at
    # construction and never consults the caller. wuying_mode="per_user" gives
    # each user their own desktop for the cloud-desktop *view* only — until
    # this becomes True, the two planes must not be allowed to diverge.
    routes_per_user = False

    def __init__(self) -> None:
        from core.config import get_config

        config = get_config()
        self.routing = getattr(config, "wuying_routing", "shared")
        if self.routing not in ("shared", "per_desktop"):
            raise ValueError("WUYING_ROUTING must be shared or per_desktop")
        self.routes_per_user = self.routing == "per_desktop"
        self.endpoint: str = (getattr(config, "wuying_endpoint", "") or "http://127.0.0.1:18000").rstrip("/")
        self.desktop_id: str = getattr(config, "wuying_desktop_id", "") or CONTAINER_ID
        api_key: str = getattr(config, "wuying_api_key", "") or ""

        parsed = urlparse(self.endpoint)
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        # SandboxManager builds its client from this rather than host/port, so a
        # tunnelled or TLS endpoint survives the round trip intact.
        self.client_base_url = self.endpoint if self.routing == "shared" else None

        self._api_key = api_key
        self._containers: dict[str, ContainerInfo] = {CONTAINER_ID: self._build_desktop()}
        self._api_keys: dict[str, str] = {CONTAINER_ID: api_key}
        # Shared desktop: every user maps onto the same sandbox.
        self._container_owners: dict[str, str] = {}
        self._container_projects: dict[str, str] = {}

        if not api_key:
            log.warning("WUYING_API_KEY is empty — the action server will reject every request")
        if config.jwt_secret and self.routing == "shared":
            log.error(
                "WUYING maps every authenticated user to one physical desktop; "
                "owner/workspace labels are routing metadata, not a tenant security "
                "boundary. Use this provider only for trusted development or place "
                "each tenant behind an independently isolated desktop/container."
            )
        if self.routing == "shared":
            log.info(f"WUYING sandbox provider -> {self.endpoint} (desktop {self.desktop_id})")
        else:
            log.info("WUYING sandbox provider -> per-desktop database routing")

    def _build_desktop(self) -> ContainerInfo:
        """The registry entry standing for the cloud desktop."""
        return ContainerInfo(
            id=CONTAINER_ID,
            name=self.desktop_id,
            status=ContainerStatus.RUNNING,
            image=f"wuying:{self.desktop_id}",
            created_at=datetime.now(timezone.utc),
            host=self._host,
            port=self._port,
            api_key=self._api_key,
        )

    def _desktop(self) -> ContainerInfo:
        """The one desktop, rebuilt if something evicted it.

        The registry holds exactly one entry and OpenBox does not own it: the
        desktop exists whether or not this process believes it does. Callers
        that recycle a dead sandbox clear it from the provider's caches —
        correct for containers OpenBox creates, fatal here, because nothing
        outside __init__ ever put it back. One failed liveness check used to
        strand every later request on KeyError until the process restarted.
        """
        info = self._containers.get(CONTAINER_ID)
        if info is None:
            log.warning("desktop entry was evicted from the registry; restoring it")
            info = self._build_desktop()
            self._containers[CONTAINER_ID] = info
            self._api_keys.setdefault(CONTAINER_ID, info.api_key or "")
        return info

    # -- lifecycle: the desktop already exists, so these are mostly inert --

    async def create_container(
        self, name: str, image: str | None = None,
        project_id: str | None = None, user_id: str | None = None,
    ) -> ContainerInfo:
        if self.routing == "shared":
            return self._desktop()
        from sandbox.wuying_desktop_service import DesktopNotReady

        raise DesktopNotReady({"state": "not_provisioned"})

    async def delete_container(self, container_id: str, user_id: str | None = None) -> None:
        log.info("delete_container ignored — the WUYING desktop is not managed by OpenBox")

    async def start_container(self, container_id: str, user_id: str | None = None) -> None:
        return None

    async def stop_container(self, container_id: str, user_id: str | None = None) -> None:
        log.info("stop_container ignored — the WUYING desktop is not managed by OpenBox")

    async def get_container(self, container_id: str, user_id: str | None = None) -> ContainerInfo:
        if self.routing == "shared":
            return self._desktop()
        from db.repository.cloud_desktop_repo import cloud_desktop_repo

        record = await cloud_desktop_repo.get_by_desktop_id(container_id)
        if not record:
            raise KeyError(container_id)
        if user_id and record["user_id"] != user_id:
            raise PermissionError(f"Desktop {container_id} does not belong to {user_id}")
        return self._record_container(record)

    async def list_containers(self) -> list[ContainerInfo]:
        if self.routing == "shared":
            return list(self._containers.values())
        from db.repository.cloud_desktop_repo import cloud_desktop_repo

        result = []
        for record in await cloud_desktop_repo.list_active():
            try:
                result.append(self._record_container(record))
            except ChannelNotReady:
                continue
        return result

    # -- ownership: one shared desktop, so every user resolves to it --

    def get_user_container(self, user_id: str, project_id: str | None = None) -> ContainerInfo | None:
        if self.routing == "shared":
            return self._desktop()
        # Database-backed routing must use resolve_user_container().  Returning
        # only a cache hit here prevents synchronous legacy callers from
        # accidentally treating the shared desktop as a fallback.
        return next(
            (
                info
                for container_id, info in self._containers.items()
                if self._container_owners.get(container_id) == user_id
            ),
            None,
        )

    async def resolve_user_container(
        self, owner: str, project_id: str | None = None
    ) -> ContainerInfo | None:
        if self.routing == "shared":
            return self._desktop()
        from db.repository.cloud_desktop_repo import cloud_desktop_repo
        from sandbox.wuying_desktop_service import DesktopNotReady

        record = await cloud_desktop_repo.get_for_user(owner)
        if not record:
            raise DesktopNotReady({"state": "not_provisioned"})
        if record.get("status") != "running" or record.get("tunnel_state") != "up":
            raise DesktopNotReady(
                {
                    "state": record.get("status") or "not_provisioned",
                    "desktopId": record.get("desktop_id"),
                    "channel": {
                        "state": record.get("tunnel_state") or "pending",
                        "last_seen_at": (
                            record["last_seen_at"].isoformat()
                            if hasattr(record.get("last_seen_at"), "isoformat")
                            else record.get("last_seen_at")
                        ),
                    },
                }
            )
        info = self._record_container(record)
        self._containers[info.id] = info
        self._api_keys[info.id] = info.api_key or ""
        self._container_owners[info.id] = owner
        self._container_projects[info.id] = project_id or "default"
        return info

    def _record_container(self, record: dict) -> ContainerInfo:
        host, port, api_key = route_for_record(record)
        desktop_id = record.get("desktop_id")
        if not desktop_id:
            raise ChannelNotReady("desktop id is missing")
        return ContainerInfo(
            id=desktop_id,
            name=desktop_id,
            status=ContainerStatus.RUNNING,
            image=f"wuying:{desktop_id}",
            created_at=record.get("created_at") or datetime.now(timezone.utc),
            host=host,
            port=port,
            api_key=api_key,
        )

    def get_containers_for_user(self, user_id: str) -> list[ContainerInfo]:
        if self.routing == "shared":
            return list(self._containers.values())
        return super().get_containers_for_user(user_id)

    def ensure_container_access(self, container_id: str, user_id: str | None = None) -> None:
        if self.routing == "shared":
            return None
        return super().ensure_container_access(container_id, user_id)

    async def ensure_user_container(self, user_id: str, project_id: str = "default") -> ContainerInfo:
        if self.routing == "shared":
            return self._desktop()
        return await self.resolve_user_container(user_id, project_id)

    # -- transport --

    async def forward_to_container(
        self, container_id: str, method: str, path: str,
        user_id: str | None = None, **kwargs,
    ) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        endpoint = self.endpoint
        api_key = self._api_keys.get(CONTAINER_ID) or (self._desktop().api_key or "")
        if self.routing == "per_desktop":
            from db.repository.cloud_desktop_repo import cloud_desktop_repo

            record = await cloud_desktop_repo.get_by_desktop_id(container_id)
            if not record:
                raise ChannelNotReady("desktop is not assigned")
            host, port, api_key = route_for_record(record)
            endpoint = f"http://{host}:{port}"
        headers["X-API-Key"] = api_key
        if user_id:
            headers[USER_SCOPE_HEADER] = user_scope_for(user_id)
        timeout = kwargs.pop("timeout", 35.0)
        # trust_env=False: see SandboxClient._client — a developer proxy must not
        # intercept traffic to the tunnel endpoint.
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            return await client.request(method, f"{endpoint}{path}", headers=headers, **kwargs)

    async def reconcile(self) -> None:
        """Confirm the action server answers; log loudly rather than failing startup."""
        if self.routing == "per_desktop":
            from db.repository.cloud_desktop_repo import cloud_desktop_repo

            records = await cloud_desktop_repo.list_active()
            for record in records:
                if record.get("status") == "running" and record.get("tunnel_state") != "revoked":
                    await wuying_channel.probe(record)
            log.info("WUYING per-desktop reconcile checked %d assigned desktop(s)", len(records))
            return
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
                resp = await client.get(f"{self.endpoint}/alive")
            if resp.status_code == 200:
                log.info(f"WUYING sandbox reachable: {resp.json()}")
                return
            log.error(f"WUYING sandbox returned HTTP {resp.status_code} from {self.endpoint}/alive")
        except Exception as exc:
            log.error(
                f"WUYING sandbox unreachable at {self.endpoint} "
                f"({type(exc).__name__}). "
                "Is the SSH tunnel up? See scripts/wuying_tunnel.sh"
            )

    async def cleanup_all(self) -> None:
        return None
