import asyncio
import json
import logging
import secrets
import socket
from collections.abc import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import docker
import httpx
from docker.errors import APIError, ImageNotFound, NotFound

from core.config import get_config
from models.container import ContainerInfo, ContainerStatus
from sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONTAINER_BUILD_DIR = PROJECT_ROOT / "container"


class DockerManager(SandboxProvider):
    supports_build = True

    def __init__(self):
        config = get_config()
        self.config = config
        self.docker_client: docker.DockerClient = docker.from_env()
        self._containers: dict[str, ContainerInfo] = {}
        self._api_keys: dict[str, str] = {}
        self._container_owners: dict[str, str] = {}
        self._container_projects: dict[str, str] = {}
        self._executor = ThreadPoolExecutor(max_workers=4)

    @staticmethod
    def _labels(container) -> dict[str, str]:
        return ((container.attrs.get("Config") or {}).get("Labels") or {})

    @staticmethod
    def _api_key_from_container(container) -> str:
        for item in ((container.attrs.get("Config") or {}).get("Env") or []):
            if item.startswith("SESSION_API_KEY="):
                return item.split("=", 1)[1]
        return ""

    def _host_port_from_container(self, container) -> int:
        ports = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
        bindings = ports.get(f"{self.config.action_server_port}/tcp") or []
        if not bindings or not bindings[0].get("HostPort"):
            raise RuntimeError(
                f"Docker sandbox {container.name} has no published action-server port"
            )
        return int(bindings[0]["HostPort"])

    @staticmethod
    def _created_at_from_container(container) -> datetime:
        raw = str(container.attrs.get("Created") or "")
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)

    def _register_existing(
        self,
        container,
        *,
        display_name: str,
        user_id: str,
        project_id: str | None = None,
    ) -> ContainerInfo:
        """Rebuild process-local routing from self-describing Docker state."""
        labels = self._labels(container)
        owner = labels.get("openbox.dev/user-id-raw")
        if owner and owner != user_id:
            raise PermissionError(
                f"Docker sandbox {container.name} belongs to a different owner"
            )
        api_key = self._api_key_from_container(container)
        if not api_key:
            raise RuntimeError(
                f"Docker sandbox {container.name} has no recoverable API key"
            )
        state = str((container.attrs.get("State") or {}).get("Status") or container.status)
        status = (
            ContainerStatus.RUNNING
            if state == "running"
            else ContainerStatus.STOPPED
            if state in {"created", "exited"}
            else ContainerStatus.ERROR
        )
        tags = getattr(container.image, "tags", None) or []
        info = ContainerInfo(
            id=container.short_id,
            name=labels.get("openbox.dev/display-name") or display_name,
            status=status,
            image=tags[0] if tags else self.config.sandbox_image,
            created_at=self._created_at_from_container(container),
            host="localhost",
            port=self._host_port_from_container(container),
            api_key=api_key,
        )
        self._containers[info.id] = info
        self._api_keys[info.id] = api_key
        self._container_owners[info.id] = user_id
        if project_id:
            self._container_projects[info.id] = project_id
        return info

    async def _reuse_named_container(
        self,
        container,
        *,
        display_name: str,
        user_id: str,
        project_id: str | None,
    ) -> ContainerInfo:
        """Adopt the deterministic-name winner instead of deleting it."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, container.reload)
        info = self._register_existing(
            container,
            display_name=display_name,
            user_id=user_id,
            project_id=project_id,
        )
        if info.status != ContainerStatus.RUNNING:
            await loop.run_in_executor(self._executor, container.start)
            await loop.run_in_executor(self._executor, container.reload)
            info = self._register_existing(
                container,
                display_name=display_name,
                user_id=user_id,
                project_id=project_id,
            )
        await self._wait_until_ready(info.id, int(info.port or 0))
        info.status = ContainerStatus.RUNNING
        logger.info(f"Reused Docker sandbox {container.name} for owner {user_id}")
        return info

    def image_exists(self, image: str | None = None) -> bool:
        image = image or self.config.sandbox_image
        try:
            self.docker_client.images.get(image)
            return True
        except ImageNotFound:
            return False

    async def build_sandbox_image(self) -> AsyncGenerator[dict, None]:
        """Build the sandbox image from container/ dir, yielding SSE-friendly dicts."""
        image = self.config.sandbox_image

        if not CONTAINER_BUILD_DIR.exists():
            yield {"step": "error", "message": f"Build context not found: {CONTAINER_BUILD_DIR}"}
            return

        yield {"step": "building", "message": "Sending build context to Docker daemon..."}

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        def _run_build():
            try:
                build_gen = self.docker_client.api.build(
                    path=str(CONTAINER_BUILD_DIR),
                    tag=image,
                    rm=True,
                    decode=True,
                )
                for event in build_gen:
                    if "stream" in event:
                        line = event["stream"].rstrip("\n")
                        if line:
                            loop.call_soon_threadsafe(queue.put_nowait, {"step": "building", "message": line})
                    elif "error" in event:
                        loop.call_soon_threadsafe(queue.put_nowait, {"step": "error", "message": event["error"].strip()})
                        loop.call_soon_threadsafe(queue.put_nowait, None)
                        return
                loop.call_soon_threadsafe(queue.put_nowait, {"step": "complete", "message": "Sandbox image built successfully"})
            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, {"step": "error", "message": str(e)})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(self._executor, _run_build)

        while True:
            event = await queue.get()
            if event is None:
                break
            yield event

    def _find_available_port(self) -> int:
        min_port, max_port = self.config.container_port_range
        for port in range(min_port, max_port + 1):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("", port))
                    return port
            except OSError:
                continue
        raise RuntimeError("No available ports in range")

    async def create_container(self, name: str, image: str | None = None, project_id: str | None = None, user_id: str | None = None) -> ContainerInfo:
        image = image or self.config.sandbox_image
        resolved_user_id = user_id or name
        if user_id:
            existing_for_user = self.get_user_container(user_id)
            if existing_for_user:
                if existing_for_user.status != ContainerStatus.RUNNING:
                    await self.start_container(existing_for_user.id, user_id=user_id)
                    return await self.get_container(existing_for_user.id, user_id=user_id)
                logger.info(f"Reusing existing user container for {user_id}")
                return existing_for_user
        container_name = f"{self.config.container_name_prefix}{name}"
        loop = asyncio.get_event_loop()

        # If we already track this container in-memory, return it
        for container_id, info in self._containers.items():
            if info.name == name and info.status == ContainerStatus.RUNNING:
                if self._container_owners.get(container_id) != resolved_user_id:
                    raise PermissionError(
                        f"Docker sandbox name {container_name} belongs to a different owner"
                    )
                logger.info(f"Reusing existing tracked container {container_name}")
                return info

        # Remove stale in-memory entry if present (stopped/error)
        stale_ids = [
            cid
            for cid, info in self._containers.items()
            if info.name == name
            and self._container_owners.get(cid) == resolved_user_id
        ]
        for cid in stale_ids:
            self._containers.pop(cid, None)
            self._api_keys.pop(cid, None)

        # Check if another API/worker process already owns the deterministic
        # name. The old implementation force-removed that container, which let
        # a standalone Worker kill the Web process's live sandbox and rotate
        # its API key underneath active calls.
        try:
            existing = await loop.run_in_executor(
                self._executor, self.docker_client.containers.get, container_name
            )
            return await self._reuse_named_container(
                existing,
                display_name=name,
                user_id=resolved_user_id,
                project_id=project_id,
            )
        except NotFound:
            pass  # No conflict — proceed with creation

        host_port = self._find_available_port()
        api_key = secrets.token_urlsafe(32)

        # Mount Named Volumes for persistent data
        volumes = {}
        if user_id:
            # Data volume: skills, MCP configs, user settings
            volumes[f"openbox-data-{user_id}"] = {"bind": "/data", "mode": "rw"}
            # Workspace volume: agent-generated code/files across sessions
            volumes[f"openbox-workspace-{user_id}"] = {"bind": "/workspace", "mode": "rw"}

        container = None
        try:
            container = await loop.run_in_executor(
                self._executor,
                partial(
                    self.docker_client.containers.run,
                    image,
                    detach=True,
                    name=container_name,
                    ports={f"{self.config.action_server_port}/tcp": host_port},
                    environment={"SESSION_API_KEY": api_key},
                    volumes=volumes if volumes else None,
                    mem_limit="512m",
                    cpu_period=100000,
                    cpu_quota=50000,
                    init=True,
                    labels={
                        "openbox.dev/user-id-raw": resolved_user_id,
                        "openbox.dev/display-name": name,
                    },
                ),
            )

            info = ContainerInfo(
                id=container.short_id,
                name=name,
                status=ContainerStatus.CREATING,
                image=image,
                created_at=datetime.now(timezone.utc),
                host="localhost",
                port=host_port,
                api_key=api_key,
            )

            self._containers[container.short_id] = info
            self._api_keys[container.short_id] = api_key
            if user_id:
                self._container_owners[container.short_id] = user_id
            if project_id:
                self._container_projects[container.short_id] = project_id

            await self._wait_until_ready(container.short_id, host_port)
            info.status = ContainerStatus.RUNNING

            logger.info(f"Container {container_name} created on port {host_port}")
            return info

        except APIError as exc:
            # A second process may win after our preflight lookup. Re-read the
            # named container and use its persisted credentials; never delete
            # a resource this attempt did not create.
            status_code = getattr(exc, "status_code", None) or getattr(
                getattr(exc, "response", None), "status_code", None
            )
            if status_code == 409:
                winner = await loop.run_in_executor(
                    self._executor,
                    self.docker_client.containers.get,
                    container_name,
                )
                return await self._reuse_named_container(
                    winner,
                    display_name=name,
                    user_id=resolved_user_id,
                    project_id=project_id,
                )
            logger.error(
                f"Failed to create Docker sandbox: {type(exc).__name__}"
            )
            if container is not None:
                try:
                    await loop.run_in_executor(
                        self._executor, partial(container.remove, force=True)
                    )
                except Exception:
                    pass
            raise
        except Exception as exc:
            logger.error(f"Failed to create Docker sandbox: {type(exc).__name__}")
            if container is not None:
                try:
                    await loop.run_in_executor(
                        self._executor, partial(container.remove, force=True)
                    )
                except Exception:
                    pass
            raise

    async def _wait_until_ready(self, container_id: str, port: int, host: str = "localhost"):
        url = f"http://{host}:{port}/alive"
        max_attempts = int(self.config.container_ready_timeout / self.config.container_ready_interval)
        async with httpx.AsyncClient() as client:
            for attempt in range(max_attempts):
                try:
                    resp = await client.get(url, timeout=2.0)
                    if resp.status_code == 200:
                        logger.info(f"Container {container_id} ready after {attempt + 1} attempts")
                        return
                except httpx.RemoteProtocolError:
                    logger.debug(f"Attempt {attempt + 1}: protocol error")
                except (httpx.ConnectError, httpx.ReadTimeout) as e:
                    logger.debug(f"Attempt {attempt + 1}: {type(e).__name__}")
                except Exception as exc:
                    logger.debug(
                        f"Attempt {attempt + 1}: unexpected {type(exc).__name__}"
                    )
                await asyncio.sleep(self.config.container_ready_interval)
        raise TimeoutError(f"Container {container_id} did not become ready")

    async def delete_container(self, container_id: str, user_id: str | None = None) -> None:
        info = self._containers.get(container_id)
        if not info:
            raise ValueError(f"Container {container_id} not found")
        self.ensure_container_access(container_id, user_id)
        container_name = f"{self.config.container_name_prefix}{info.name}"
        loop = asyncio.get_event_loop()
        try:
            container = await loop.run_in_executor(
                self._executor, self.docker_client.containers.get, container_name
            )
            await loop.run_in_executor(self._executor, partial(container.remove, force=True))
        except NotFound:
            pass
        self._containers.pop(container_id, None)
        self._api_keys.pop(container_id, None)
        self._container_owners.pop(container_id, None)
        self._container_projects.pop(container_id, None)
        logger.info(f"Container {container_name} deleted")

    async def list_containers(self) -> list[ContainerInfo]:
        loop = asyncio.get_event_loop()
        # 同步 Docker 实际状态
        for cid, info in list(self._containers.items()):
            container_name = f"{self.config.container_name_prefix}{info.name}"
            try:
                container = await loop.run_in_executor(
                    self._executor, self.docker_client.containers.get, container_name
                )
                docker_status = container.status
                if docker_status == "running":
                    info.status = ContainerStatus.RUNNING
                elif docker_status == "exited":
                    info.status = ContainerStatus.STOPPED
                else:
                    info.status = ContainerStatus.ERROR
            except NotFound:
                self._containers.pop(cid, None)
                self._api_keys.pop(cid, None)
                self._container_owners.pop(cid, None)
                self._container_projects.pop(cid, None)
        return list(self._containers.values())

    async def get_container(self, container_id: str, user_id: str | None = None) -> ContainerInfo:
        info = self._containers.get(container_id)
        if not info:
            raise ValueError(f"Container {container_id} not found")
        self.ensure_container_access(container_id, user_id)
        return info

    async def stop_container(self, container_id: str, user_id: str | None = None) -> None:
        info = self._containers.get(container_id)
        if not info:
            raise ValueError(f"Container {container_id} not found")
        self.ensure_container_access(container_id, user_id)
        container_name = f"{self.config.container_name_prefix}{info.name}"
        loop = asyncio.get_event_loop()
        try:
            container = await loop.run_in_executor(
                self._executor, self.docker_client.containers.get, container_name
            )
            await loop.run_in_executor(self._executor, partial(container.stop, timeout=10))
            info.status = ContainerStatus.STOPPED
        except NotFound:
            raise ValueError(f"Container {container_id} not found in Docker")

    async def start_container(self, container_id: str, user_id: str | None = None) -> None:
        info = self._containers.get(container_id)
        if not info:
            raise ValueError(f"Container {container_id} not found")
        self.ensure_container_access(container_id, user_id)
        container_name = f"{self.config.container_name_prefix}{info.name}"
        loop = asyncio.get_event_loop()
        try:
            container = await loop.run_in_executor(
                self._executor, self.docker_client.containers.get, container_name
            )
            await loop.run_in_executor(self._executor, container.start)
            await self._wait_until_ready(container_id, info.port)
            info.status = ContainerStatus.RUNNING
        except NotFound:
            raise ValueError(f"Container {container_id} not found in Docker")

    async def forward_to_container(
        self, container_id: str, method: str, path: str, user_id: str | None = None, **kwargs
    ) -> httpx.Response:
        info = self._containers.get(container_id)
        if not info:
            raise ValueError(f"Container {container_id} not found")
        self.ensure_container_access(container_id, user_id)
        if info.status != ContainerStatus.RUNNING:
            raise ValueError(f"Container {container_id} is not running")

        api_key = self._api_keys.get(container_id, "")
        url = f"http://{info.host}:{info.port}{path}"
        headers = kwargs.pop("headers", {})
        headers["X-API-Key"] = api_key

        timeout = kwargs.pop("timeout", 35.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(method, url, headers=headers, **kwargs)

    async def reconcile(self) -> None:
        """Restore routing state without destroying another process's work."""
        loop = asyncio.get_event_loop()
        containers = await loop.run_in_executor(
            self._executor,
            partial(
                self.docker_client.containers.list,
                all=True,
                filters={"name": self.config.container_name_prefix},
            ),
        )
        restored = 0
        for container in containers:
            try:
                await loop.run_in_executor(self._executor, container.reload)
                labels = self._labels(container)
                owner = labels.get("openbox.dev/user-id-raw")
                display_name = labels.get("openbox.dev/display-name")
                if not owner or not display_name:
                    logger.warning(
                        f"Skipped legacy Docker sandbox {container.name}: "
                        "raw owner metadata missing"
                    )
                    continue
                self._register_existing(
                    container,
                    display_name=display_name,
                    user_id=owner,
                )
                restored += 1
            except Exception as exc:
                logger.warning(
                    f"Skipped Docker sandbox {container.name}: "
                    f"{type(exc).__name__}"
                )
        logger.info(f"Reconciled {restored} Docker sandbox container(s)")

    async def cleanup_all(self) -> None:
        loop = asyncio.get_event_loop()
        containers = await loop.run_in_executor(
            self._executor,
            partial(
                self.docker_client.containers.list,
                all=True,
                filters={"name": self.config.container_name_prefix},
            ),
        )
        for c in containers:
            try:
                await loop.run_in_executor(self._executor, partial(c.remove, force=True))
                logger.info(f"Cleaned up container {c.name}")
            except Exception as exc:
                logger.warning(
                    f"Failed to remove container {c.name}: {type(exc).__name__}"
                )
        self._containers.clear()
        self._api_keys.clear()
        self._container_owners.clear()
        self._container_projects.clear()
