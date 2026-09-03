"""Sandbox lifecycle management: User-level container reuse with session-level directory isolation."""
import asyncio
from dataclasses import dataclass, field

from core.log import create_logger
from models.container import ContainerStatus
from sandbox.client import SandboxClient, user_scope_for
from sandbox.provider import build_sandbox_name

log = create_logger("sandbox.manager")


def _map_key(user_id: str, project_id: str = "default") -> str:
    """Build a stable in-memory mapping key for a user's sandbox."""
    return user_id


@dataclass
class SandboxInfo:
    """Information about a user-level sandbox container shared across sessions."""
    container_id: str
    user_id: str
    host: str
    port: int
    api_key: str
    project_id: str
    session_ids: set[str] = field(default_factory=set)
    base_url: str | None = None   # set by URL-addressed providers (wuying)

    @property
    def alive_url(self) -> str:
        return f"{(self.base_url or f'http://{self.host}:{self.port}').rstrip('/')}/alive"


class SandboxManager:
    """Manages user-level sandbox containers with session-level directory isolation.

    All sessions for the same user share ONE container.
    Each session gets its own working directory: /workspace/sessions/{session_id}/
    This avoids creating a new container per session while keeping data isolated.
    """

    def __init__(self):
        self._project_map: dict[str, SandboxInfo] = {}    # user_id -> sandbox
        self._session_project: dict[str, str] = {}        # session_id -> user_id
        self._clients: dict[str, SandboxClient] = {}      # user_id -> client
        self._lock = asyncio.Lock()
        # Provider cold starts run outside the short global map lock. Serialize
        # them per user so two sessions cannot both observe "no sandbox" and
        # create duplicate containers; different tenants still start in parallel.
        self._acquire_locks: dict[str, asyncio.Lock] = {}

    async def _verify_sandbox_alive(self, sandbox: SandboxInfo, key: str) -> bool:
        """Return True when the tracked sandbox still responds to /alive."""
        import httpx

        try:
            # trust_env=False — the sandbox endpoint is direct infrastructure and
            # must not be routed through a developer's HTTP(S)_PROXY.
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as http:
                resp = await http.get(sandbox.alive_url)
            if resp.status_code == 200:
                return True
        except Exception:
            pass

        log.warning(f"Sandbox {sandbox.container_id} not responsive for key {key}")
        return False

    async def _cleanup_stale_sandbox(
        self,
        sandbox: SandboxInfo,
        key: str,
        session_id: str | None = None,
    ) -> None:
        """Clear stale sandbox state from both manager and provider caches."""
        from sandbox import provider

        # A failed liveness probe is not proof that a pre-provisioned remote
        # desktop was deleted. In particular, a Wuying tunnel outage must keep
        # the existing SandboxClient: it owns the last-known-good catalogue
        # projection needed to replay historical canonical tool calls. Replacing
        # that client turns a warm outage into a cold empty catalogue and makes
        # the provider transcript unreplayable before the executor can report
        # the real connectivity error.
        if not getattr(provider, "owns_containers", True):
            log.warning(
                "Retaining unresponsive externally managed sandbox %s for key %s",
                sandbox.container_id,
                key,
            )
            return

        async with self._lock:
            # The health probe ran without the map lock. Another coroutine may
            # already have replaced this entry; never let an old failed probe
            # evict the replacement.
            if self._project_map.get(key) is not sandbox:
                return
            self._project_map.pop(key, None)
            self._clients.pop(key, None)
            stale_sessions = [
                sid for sid, mapped_key in self._session_project.items() if mapped_key == key
            ]
            for sid in stale_sessions:
                self._session_project.pop(sid, None)
            if session_id:
                self._session_project.pop(session_id, None)

        provider._containers.pop(sandbox.container_id, None)
        provider._api_keys.pop(sandbox.container_id, None)
        if hasattr(provider, "_container_owners"):
            provider._container_owners.pop(sandbox.container_id, None)
        if hasattr(provider, "_container_projects"):
            provider._container_projects.pop(sandbox.container_id, None)

    async def check_health(self, project_id: str = "default", user_id: str = "default") -> dict:
        """Check if a sandbox is available and healthy for the given user."""
        key = _map_key(user_id)
        async with self._lock:
            acquire_lock = self._acquire_locks.setdefault(key, asyncio.Lock())
        async with acquire_lock:
            async with self._lock:
                sandbox = self._project_map.get(key)
            if sandbox:
                if await self._verify_sandbox_alive(sandbox, key):
                    return {
                        "available": True,
                        "container_id": sandbox.container_id,
                        "container_name": build_sandbox_name(user_id),
                        "status": "running",
                    }
                await self._cleanup_stale_sandbox(sandbox, key)

        from sandbox import provider
        containers = provider.get_containers_for_user(user_id)
        return {
            "available": False,
            "containers": [
                {
                    "id": c.id,
                    "name": c.name,
                    "status": c.status.value,
                    "host": c.host,
                    "port": c.port,
                }
                for c in containers
            ],
        }

    async def acquire(
        self,
        session_id: str,
        project_id: str = "default",
        *,
        user_id: str,
    ) -> SandboxInfo:
        from sandbox.ownership import owner_for

        owner = await owner_for(user_id)
        key = _map_key(owner)
        async with self._lock:
            acquire_lock = self._acquire_locks.setdefault(key, asyncio.Lock())
        async with acquire_lock:
            return await self._acquire_for_user(
                session_id,
                project_id,
                user_id=user_id,
                owner=owner,
            )

    async def _acquire_for_user(
        self,
        session_id: str,
        project_id: str = "default",
        *,
        user_id: str,
        owner: str | None = None,
    ) -> SandboxInfo:
        """Acquire a sandbox for a session. Reuses the user's existing container if available."""
        if owner is None:
            from sandbox.ownership import owner_for

            owner = await owner_for(user_id)
        key = _map_key(owner)

        from sandbox import provider

        # Per-desktop routing is database-authoritative on every acquisition.
        # This makes a revoke or credential rotation invalidate a cached route
        # immediately instead of waiting for an unauthenticated /alive probe.
        authoritative = None
        per_owner_route = getattr(provider, "routes_per_user", False) is True
        if per_owner_route:
            authoritative = await provider.resolve_user_container(owner, project_id)

        async with self._lock:
            existing_key = self._session_project.get(session_id)
            if existing_key is not None and existing_key != key:
                raise RuntimeError(f"Sandbox session ownership mismatch for {session_id}")
            sandbox = self._project_map.get(key)
            client = self._clients.get(key)

        if authoritative is not None and sandbox is not None:
            route_changed = (
                sandbox.container_id != authoritative.id
                or sandbox.host != authoritative.host
                or sandbox.port != authoritative.port
                or sandbox.api_key != (authoritative.api_key or "")
            )
            if route_changed:
                async with self._lock:
                    if self._project_map.get(key) is sandbox:
                        self._project_map.pop(key, None)
                        self._clients.pop(key, None)
                sandbox = None
                client = None

        # Provider probes and filesystem setup are network operations. The
        # per-user acquire lock already serializes this tenant; holding the
        # global map lock here would make one slow desktop block every user.
        if sandbox:
            if await self._verify_sandbox_alive(sandbox, key):
                if client is None:
                    client = SandboxClient(
                        host=sandbox.host,
                        port=sandbox.port,
                        api_key=sandbox.api_key,
                        base_url=sandbox.base_url,
                        user_scope=user_scope_for(user_id),
                    )
                async with self._lock:
                    if self._project_map.get(key) is not sandbox:
                        sandbox = None
                    else:
                        self._clients[key] = client
                        sandbox.session_ids.add(session_id)
                        self._session_project[session_id] = key
                if sandbox is not None:
                    log.info(
                        f"Reusing sandbox {sandbox.container_id} for session {session_id} "
                        f"(user {user_id})"
                    )
                    await self._ensure_session_dir(client, session_id)
                    return sandbox
            else:
                await self._cleanup_stale_sandbox(sandbox, key, session_id)
                # Externally managed desktops are retained on a transient
                # health failure. Reattach this session to the same client and
                # let catalogue reads use its stale projection. Do not run the
                # directory bootstrap here: the failed probe already proved it
                # would only add latency and another transport error.
                async with self._lock:
                    retained_client = (
                        self._clients.get(key)
                        if self._project_map.get(key) is sandbox
                        else None
                    )
                    if retained_client is not None:
                        sandbox.session_ids.add(session_id)
                        self._session_project[session_id] = key
                if retained_client is not None:
                    return sandbox

        try:
            info = (
                authoritative
                if per_owner_route
                else provider.get_user_container(owner)
            )
            if info:
                if info.status != ContainerStatus.RUNNING:
                    await provider.start_container(info.id, user_id=owner)
                    info = await provider.get_container(info.id, user_id=owner)
            else:
                info = await provider.create_container(
                    name=build_sandbox_name(user_id),
                    image=None,
                    project_id=None,
                    user_id=owner,
                )

            sandbox = SandboxInfo(
                container_id=info.id,
                user_id=user_id,
                host=info.host,
                port=info.port,
                api_key=info.api_key or "",
                project_id=project_id,
                session_ids={session_id},
                base_url=getattr(provider, "client_base_url", None),
            )

            client = SandboxClient(
                host=info.host,
                port=info.port,
                api_key=info.api_key or "",
                base_url=getattr(provider, "client_base_url", None),
                user_scope=user_scope_for(user_id),
            )

            async with self._lock:
                self._project_map[key] = sandbox
                self._session_project[session_id] = key
                self._clients[key] = client

            await self._ensure_session_dir(client, session_id)

            log.info(f"Created sandbox {info.id} for user {user_id}, session {session_id}")
            return sandbox

        except Exception as exc:
            log.error(
                f"Failed to acquire sandbox for session {session_id}: "
                f"{type(exc).__name__}"
            )
            raise

    async def _ensure_session_dir(self, client: SandboxClient, session_id: str) -> None:
        """Create the directory this session will run in.

        That is the project's directory, not one per session: sessions in a
        project share a working tree the way two terminals open on the same
        checkout do, so the agent can pick up where the last conversation left
        off instead of starting in an empty folder every time.
        """
        from project.workspace import (
            INTERNAL_ROOT, project_directory, slug_for, WORKSPACE_ROOT,
        )
        slug = "default"
        try:
            from session.session import project_id_for
            slug = await slug_for(await project_id_for(session_id))
        except Exception as exc:
            log.debug(
                f"Could not resolve project for session {session_id}: "
                f"{type(exc).__name__}"
            )

        workdir = project_directory(slug)
        try:
            await client.execute(
                command=f"mkdir -p {workdir} {INTERNAL_ROOT}",
                timeout=10,
                workdir=WORKSPACE_ROOT,
            )
        except Exception as exc:
            log.warning(
                f"Failed to create project dir {workdir}: {type(exc).__name__}"
            )

    async def get_session_workdir(self, session_id: str) -> str:
        """The directory a session's tools run in — its project's directory."""
        from project.workspace import project_directory, slug_for
        try:
            from session.session import project_id_for
            return project_directory(await slug_for(await project_id_for(session_id)))
        except Exception as exc:
            log.debug(
                f"Could not resolve workdir for {session_id}: "
                f"{type(exc).__name__}"
            )
        return project_directory("default")

    async def release(self, session_id: str, *, user_id: str) -> None:
        """Detach one session without deciding the sandbox's global lifetime.

        ``session_ids`` is process-local. In a multi-replica deployment it
        cannot prove that no other process still uses the execution
        environment. Destruction belongs to the database-guarded idle reaper
        or an explicit owner action, never this local reference release.
        """
        expected_key = _map_key(user_id)
        async with self._lock:
            key = self._session_project.get(session_id)
            if not key:
                log.warning(f"No sandbox found for session {session_id}")
                return
            if key != expected_key:
                raise PermissionError(
                    f"Sandbox session ownership mismatch for {session_id}"
                )
            acquire_lock = self._acquire_locks.setdefault(key, asyncio.Lock())

        # Serialize provider deletion with this user's health check/cold start.
        # Otherwise an acquire can rediscover a container while release is
        # deleting it and cache an endpoint that is already going away.
        async with acquire_lock:
            async with self._lock:
                if self._session_project.get(session_id) != key:
                    return
                self._session_project.pop(session_id, None)
                sandbox = self._project_map.get(key)
                if not sandbox:
                    return

                sandbox.session_ids.discard(session_id)

                if sandbox.session_ids:
                    log.info(
                        f"Session {session_id} released, {len(sandbox.session_ids)} "
                        "local sessions still using sandbox"
                    )
                    return

                self._project_map.pop(key, None)
                self._clients.pop(key, None)
                log.info(
                    f"Session {session_id} released; retained sandbox "
                    f"{sandbox.container_id} for durable/background work"
                )

    async def get_client(self, session_id: str, *, user_id: str) -> SandboxClient:
        """Get the SandboxClient for a session. Acquires sandbox if needed."""
        expected_key = _map_key(user_id)
        # Acquire is also the health-checked, per-user serialized fast path.
        # Using it unconditionally keeps map reads and provider lifecycle in one
        # ownership protocol instead of racing a separate probe here.
        await self.acquire(session_id, user_id=user_id)
        async with self._lock:
            key = self._session_project.get(session_id)
            if key is not None and key != expected_key:
                raise RuntimeError(f"Sandbox session ownership mismatch for {session_id}")
            client = self._clients.get(key) if key else None
        if client is not None:
            return client

        raise RuntimeError(f"No sandbox client for session {session_id}")

    async def get_only_client(self) -> SandboxClient | None:
        """The one sandbox, when this deployment has exactly one.

        For maintenance that belongs to no user — the workspace sweep — where
        borrowing someone's identity to pick a sandbox would be a guess. With
        more than one live sandbox there is no defensible answer, so it
        returns None rather than choosing.
        """
        async with self._lock:
            if len(self._clients) != 1:
                return None
            return next(iter(self._clients.values()))

    async def get_client_any(self, *, user_id: str) -> SandboxClient | None:
        """Get any available SandboxClient (not session-specific).

        Used by management endpoints (skills, MCP) that operate at the container level.
        Returns the first available client. If none exists, auto-acquires a default sandbox.
        """
        try:
            # A global synthetic session id lets user B evict user A's manager
            # mapping on the shared WUYING desktop. Namespace it without
            # exposing the raw owner in filesystem/log identifiers.
            import hashlib

            management_session = (
                "__management__:" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
            )
            await self.acquire(management_session, "default", user_id=user_id)
            async with self._lock:
                key = self._session_project.get(management_session)
                if key:
                    return self._clients.get(key)
        except Exception as exc:
            log.warning(
                f"Failed to auto-acquire sandbox: {type(exc).__name__}"
            )
        return None

    def get_info(self, session_id: str) -> SandboxInfo | None:
        """Get sandbox info for a session (non-async, may return None)."""
        key = self._session_project.get(session_id)
        if key:
            return self._project_map.get(key)
        return None

    async def release_all(self, destroy: bool = True) -> None:
        """Release all sandboxes (used during shutdown)."""
        if destroy:
            keys = list(self._project_map.keys())
            for key in keys:
                sandbox = self._project_map.get(key)
                if sandbox:
                    from sandbox import provider
                    try:
                        await provider.delete_container(sandbox.container_id, user_id=sandbox.user_id)
                    except Exception as exc:
                        log.warning(
                            f"Error releasing sandbox for {key}: "
                            f"{type(exc).__name__}"
                        )
        self._project_map.clear()
        self._session_project.clear()
        self._clients.clear()
        self._acquire_locks.clear()


sandbox_manager = SandboxManager()
