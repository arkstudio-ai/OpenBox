"""HTTP client for communicating with the Action Server inside a sandbox container."""
import asyncio
import contextvars
import json
import os
import secrets
import shlex
import socket
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

from core.log import create_logger

log = create_logger("sandbox.client")

#: Backstop for an MCP tool call, in seconds.
#: Deliberately well above the container's own per-server timeout (60s by
#: default) so that the container decides when a server has taken too long and
#: can say so. An outer budget equal to the inner one just races it and
#: replaces a useful message with a timeout.
MCP_CALL_TIMEOUT_SECONDS = 180.0

_PROCESS_INSTANCE_ID = (
    os.environ.get("OPENBOX_INSTANCE_ID", "").strip()
    or f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"
)


@dataclass
class ExecuteResult:
    """Result of a command execution."""
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class OutputChunk:
    """A streaming output chunk from execute_stream."""
    type: str  # "stdout", "stderr", "system"
    content: str


@dataclass
class IdleNotification:
    """Notification that the command has produced no output for a while."""
    idle_seconds: int
    total_seconds: int
    pid: int


@dataclass(frozen=True)
class RequestTrace:
    session_id: str = ""
    tool_call_id: str = ""
    operation: str = ""
    lease_token: str = ""


class SandboxClient:
    """HTTP client for the Action Server running inside a sandbox container.

    All file and command operations go through this client to the sandbox.
    """

    def __init__(self, host: str, port: int, api_key: str, base_url: str | None = None):
        # base_url wins when set — remote providers (wuying) address the action
        # server through a tunnel endpoint rather than a host/port pair.
        self.base_url = base_url.rstrip("/") if base_url else f"http://{host}:{port}"
        self.api_key = api_key
        self._headers = {"X-API-Key": api_key}
        self._trace: contextvars.ContextVar[RequestTrace] = contextvars.ContextVar(
            f"sandbox_request_trace_{id(self)}", default=RequestTrace()
        )

    @staticmethod
    def _header_value(value: str, limit: int = 120) -> str:
        """Bound request metadata to visible ASCII safe for HTTP headers."""
        return "".join(ch for ch in (value or "") if 32 <= ord(ch) < 127)[:limit]

    def _request_headers(self) -> dict[str, str]:
        trace = self._trace.get()
        headers = {
            "X-OpenBox-Instance": self._header_value(_PROCESS_INSTANCE_ID),
            "X-OpenBox-Request": secrets.token_hex(8),
        }
        if trace.session_id:
            headers["X-OpenBox-Session"] = self._header_value(trace.session_id)
        if trace.tool_call_id:
            headers["X-OpenBox-Tool-Call"] = self._header_value(trace.tool_call_id)
        if trace.operation:
            headers["X-OpenBox-Operation"] = self._header_value(trace.operation, 48)
        if trace.lease_token:
            headers["X-OpenBox-Desktop-Lease"] = self._header_value(trace.lease_token, 160)
        return headers

    @asynccontextmanager
    async def request_context(
        self,
        *,
        session_id: str = "",
        tool_call_id: str = "",
        operation: str = "",
    ):
        """Attach caller identity to sandbox requests in the current task."""
        current = self._trace.get()
        token = self._trace.set(RequestTrace(
            session_id=session_id or current.session_id,
            tool_call_id=tool_call_id or current.tool_call_id,
            operation=operation or current.operation,
            lease_token=current.lease_token,
        ))
        try:
            yield
        finally:
            self._trace.reset(token)

    @asynccontextmanager
    async def desktop_lease(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        operation: str = "computer",
        wait_timeout: float = 90.0,
        ttl_seconds: float = 180.0,
    ):
        """Hold the remote desktop across input, capture and OSS upload.

        The lease lives on the Action Server, so it serializes separate users,
        backend processes and even separate OpenBox deployments that point at
        one long-lived WUYING desktop.
        """
        current = self._trace.get()
        if current.lease_token:
            yield {"token": current.lease_token, "wait_ms": 0, "nested": True}
            return

        async with self.request_context(
            session_id=session_id,
            tool_call_id=tool_call_id,
            operation=operation,
        ):
            owner = ":".join(part for part in (
                _PROCESS_INSTANCE_ID, session_id, tool_call_id or secrets.token_hex(6)
            ) if part)
            async with self._client(timeout=wait_timeout + 15) as client:
                response = await client.post(
                    "/desktop/lease/acquire",
                    headers=self._request_headers(),
                    json={
                        "owner": owner,
                        "wait_timeout": wait_timeout,
                        "ttl_seconds": ttl_seconds,
                    },
                )
                response.raise_for_status()
                lease = response.json()

            active = self._trace.get()
            lease_context = self._trace.set(RequestTrace(
                session_id=active.session_id,
                tool_call_id=active.tool_call_id,
                operation=active.operation,
                lease_token=lease["token"],
            ))
            try:
                yield lease
            finally:
                try:
                    async with self._client(timeout=10) as client:
                        response = await client.post(
                            "/desktop/lease/release",
                            headers=self._request_headers(),
                            json={"token": lease["token"]},
                        )
                        response.raise_for_status()
                except Exception as exc:
                    # The server-side TTL releases a lease after a crashed or
                    # disconnected backend. Do not mask the actual tool result.
                    log.warning(f"Failed to release desktop lease: {exc}")
                finally:
                    self._trace.reset(lease_context)

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        """Create an httpx async client.

        trust_env=False is deliberate: the sandbox endpoint is always directly
        reachable infrastructure (local container, in-cluster service, or an
        SSH tunnel on loopback). Honouring HTTP_PROXY/HTTPS_PROXY from the
        environment sends those requests through a developer's proxy, which
        typically cannot reach them and fails with an opaque timeout.
        """
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=timeout,
            trust_env=False,
        )

    async def execute(
        self,
        command: str,
        timeout: int = 120,
        workdir: str = "/workspace",
    ) -> ExecuteResult:
        """Execute a command in the sandbox."""
        async with self._client(timeout=timeout + 10) as client:
            resp = await client.post("/execute", headers=self._request_headers(), json={
                "command": command,
                "timeout": timeout,
                "workdir": workdir,
            })
            resp.raise_for_status()
            data = resp.json()
            return ExecuteResult(
                exit_code=data["exit_code"],
                stdout=data["stdout"],
                stderr=data["stderr"],
            )

    async def submit_media_job(self, payload: dict) -> dict:
        """Idempotently enqueue a render on this sandbox's durable media queue."""
        async with self._client(timeout=30.0) as client:
            response = await client.post(
                "/media/jobs",
                headers=self._request_headers(),
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def get_media_job(self, job_id: str, owner: str) -> dict:
        async with self._client(timeout=15.0) as client:
            response = await client.get(
                f"/media/jobs/{job_id}",
                headers=self._request_headers(),
                params={"owner": owner},
            )
            response.raise_for_status()
            return response.json()

    async def wait_media_job(
        self,
        job_id: str,
        owner: str,
        *,
        after_version: int = 0,
        timeout: float = 25.0,
    ) -> dict:
        """Long-poll one render without tying up an unbounded backend request."""
        bounded = max(0.0, min(25.0, float(timeout)))
        async with self._client(timeout=bounded + 15.0) as client:
            response = await client.get(
                f"/media/jobs/{job_id}/wait",
                headers=self._request_headers(),
                params={
                    "owner": owner,
                    "after_version": max(0, int(after_version)),
                    "timeout": bounded,
                },
            )
            response.raise_for_status()
            return response.json()

    async def cancel_media_job(self, job_id: str, owner: str) -> dict:
        async with self._client(timeout=15.0) as client:
            response = await client.post(
                f"/media/jobs/{job_id}/cancel",
                headers=self._request_headers(),
                json={"owner": owner},
            )
            response.raise_for_status()
            return response.json()

    async def retry_media_job(
        self, job_id: str, owner: str, replacement_payload: dict | None = None
    ) -> dict:
        body: dict = {"owner": owner}
        if replacement_payload is not None:
            body["payload"] = replacement_payload
        async with self._client(timeout=15.0) as client:
            response = await client.post(
                f"/media/jobs/{job_id}/retry",
                headers=self._request_headers(),
                json=body,
            )
            response.raise_for_status()
            return response.json()

    async def media_queue_status(self) -> dict:
        async with self._client(timeout=15.0) as client:
            response = await client.get(
                "/media/jobs/status",
                headers=self._request_headers(),
            )
            response.raise_for_status()
            return response.json()

    async def execute_stream(
        self,
        command: str,
        timeout: int = 120,
        idle_timeout: int = 60,
        workdir: str = "/workspace",
    ) -> AsyncIterator[OutputChunk | IdleNotification | int]:
        """Execute a command with streaming output.

        Yields OutputChunk for each line of output,
        IdleNotification when no output for idle_timeout seconds,
        and finally the exit code (int) at the end.
        """
        pid = 0
        async with self._client(timeout=timeout + 10) as client:
            async with client.stream("POST", "/execute_stream", headers=self._request_headers(), json={
                "command": command,
                "timeout": timeout,
                "idle_timeout": idle_timeout,
                "workdir": workdir,
            }) as resp:
                resp.raise_for_status()
                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data_str = line[6:]
                            try:
                                data = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            if "type" in data and "content" in data:
                                yield OutputChunk(
                                    type=data["type"],
                                    content=data["content"],
                                )
                            elif "exit_code" in data:
                                yield data["exit_code"]
                            elif "pid" in data and "idle_seconds" not in data:
                                pid = data["pid"]
                            elif "idle_seconds" in data:
                                yield IdleNotification(
                                    idle_seconds=data["idle_seconds"],
                                    total_seconds=data["total_seconds"],
                                    pid=pid,
                                )

    async def read_file(
        self,
        path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> str:
        """Read a file from the sandbox with line numbers."""
        async with self._client() as client:
            resp = await client.post("/read_file", json={
                "path": path,
                "offset": offset,
                "limit": limit,
            })
            resp.raise_for_status()
            data = resp.json()
            return data["content"]

    async def write_file(self, path: str, content: str) -> None:
        """Write content to a file in the sandbox."""
        async with self._client() as client:
            resp = await client.post("/write_file", json={
                "path": path,
                "content": content,
            })
            resp.raise_for_status()

    async def glob(self, pattern: str, path: str = "/workspace") -> list[str]:
        """Find files matching a glob pattern in the sandbox."""
        async with self._client() as client:
            resp = await client.post("/glob", json={
                "pattern": pattern,
                "path": path,
            })
            resp.raise_for_status()
            data = resp.json()
            return data["files"]

    async def grep(
        self,
        pattern: str,
        path: str = "/workspace",
        file_type: str | None = None,
        max_results: int = 100,
    ) -> str:
        """Search file contents in the sandbox."""
        async with self._client() as client:
            resp = await client.post("/grep", json={
                "pattern": pattern,
                "path": path,
                "type": file_type,
                "max_results": max_results,
            })
            resp.raise_for_status()
            data = resp.json()
            return data["output"]

    async def list_files(self, path: str = "/workspace") -> list[dict]:
        """List directory contents in the sandbox."""
        async with self._client() as client:
            resp = await client.post("/list_files", json={"path": path})
            resp.raise_for_status()
            data = resp.json()
            return data["entries"]

    async def read_file_raw(self, path: str) -> str:
        """Read raw file content from the sandbox (no line numbers)."""
        result = await self.execute(f"cat {shlex.quote(path)}", timeout=10)
        if result.exit_code != 0:
            raise FileNotFoundError(f"File not found: {path}")
        return result.stdout

    async def kill_command(self, pid: int) -> None:
        """Kill a running command by PID via the action server."""
        try:
            async with self._client(timeout=5) as client:
                resp = await client.post("/kill", json={"pid": pid})
                resp.raise_for_status()
        except Exception as e:
            log.warning(f"Failed to kill process {pid}: {e}")

    async def alive(self) -> bool:
        """Check if the sandbox is alive."""
        try:
            async with self._client(timeout=5) as client:
                resp = await client.get("/alive")
                return resp.status_code == 200
        except Exception:
            return False

    # ---- Generic HTTP helpers ----

    async def _get(self, path: str, timeout: float = 15.0):
        """Generic GET request to action server."""
        async with self._client(timeout=timeout) as client:
            resp = await client.get(path)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, timeout: float = 30.0, **kwargs):
        """Generic POST request to action server."""
        async with self._client(timeout=timeout) as client:
            resp = await client.post(path, **kwargs)
            resp.raise_for_status()
            return resp.json()

    async def _delete(self, path: str, timeout: float = 15.0):
        """Generic DELETE request to action server."""
        async with self._client(timeout=timeout) as client:
            resp = await client.request("DELETE", path)
            resp.raise_for_status()
            return resp.json()

    # ---- Dev-browser management ----

    async def start_dev_browser(self) -> dict:
        """Start the dev-browser relay server inside the container."""
        return await self._post("/dev-browser/start", timeout=15.0)

    async def stop_dev_browser(self) -> dict:
        """Stop the dev-browser relay server."""
        return await self._post("/dev-browser/stop")

    async def get_dev_browser_status(self) -> dict:
        """Get dev-browser relay status and extension connection state."""
        return await self._get("/dev-browser/status")

    # ---- Skill management ----

    async def list_skills(self) -> list[dict]:
        """List all installed skills in the container."""
        return await self._get("/skills")

    async def get_skill(self, name: str) -> dict:
        """Get a specific skill by name."""
        return await self._get(f"/skills/{name}")

    async def install_skill(
        self,
        url: str | None = None,
        name: str | None = None,
        content: str | None = None,
    ) -> dict:
        """Install a skill in the container."""
        return await self._post(
            "/skills/install",
            timeout=90.0,
            json={"url": url, "name": name, "content": content},
        )

    async def upload_skill_archive(self, file_bytes: bytes, filename: str, name: str = "") -> dict:
        """Upload a skill archive (zip/tar/tar.gz/rar) to the container."""
        import httpx
        files = {"file": (filename, file_bytes)}
        data = {"name": name or ""}
        async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
            url = f"{self.base_url}/skills/upload"
            resp = await client.post(url, files=files, data=data, headers=self._headers)
            if resp.status_code != 200:
                detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type", "").startswith("application/json") else resp.text
                raise Exception(detail)
            return resp.json()

    async def uninstall_skill(self, name: str) -> dict:
        """Uninstall a skill from the container."""
        return await self._delete(f"/skills/{name}")

    # ---- MCP server management ----

    async def list_mcp_servers(self) -> list[dict]:
        """List all MCP servers configured in the container."""
        return await self._get("/mcp/servers")

    async def add_mcp_server(self, name: str, config: dict) -> dict:
        """Add a new MCP server configuration."""
        return await self._post("/mcp/servers", json={"name": name, **config})

    async def remove_mcp_server(self, name: str) -> dict:
        """Remove an MCP server configuration."""
        return await self._delete(f"/mcp/servers/{name}")

    async def connect_mcp(self, name: str) -> dict:
        """Connect to an MCP server."""
        return await self._post(f"/mcp/servers/{name}/connect")

    async def disconnect_mcp(self, name: str) -> dict:
        """Disconnect from an MCP server."""
        return await self._post(f"/mcp/servers/{name}/disconnect")

    async def list_mcp_tools(self) -> list[dict]:
        """List all tools from connected MCP servers."""
        return await self._get("/mcp/tools")

    async def call_mcp_tool(self, server: str, tool: str, arguments: dict) -> dict:
        """Call a tool on a connected MCP server.

        The budget here has to exceed the container's own per-server timeout,
        which defaults to 60s. When both were 60s the two expired together, so
        the container's explanation never made it back — the caller got a bare
        httpx.ReadTimeout, which stringifies to nothing.

        The container is the layer that knows which server it is talking to and
        what that server's limit is, so it should be the one to give up first.
        This is only a backstop against the container itself hanging. A remote
        call also costs two round trips under the per-operation session model
        (initialize, then the call), so 60s was not even the real ceiling.
        """
        return await self._post(
            f"/mcp/tools/{server}/{tool}",
            timeout=MCP_CALL_TIMEOUT_SECONDS,
            json={"arguments": arguments},
        )

    async def list_mcp_resources(self) -> list[dict]:
        """List all resources from connected MCP servers."""
        return await self._get("/mcp/resources")

    async def read_mcp_resource(self, server: str, uri: str) -> dict:
        """Read a specific MCP resource."""
        return await self._post("/mcp/resources/read", timeout=30.0, json={"server": server, "uri": uri})

    async def list_mcp_prompts(self) -> list[dict]:
        """List all prompts from connected MCP servers."""
        return await self._get("/mcp/prompts")

    async def get_mcp_prompt(self, server: str, name: str, arguments: dict | None = None) -> dict:
        """Get a specific MCP prompt."""
        return await self._post("/mcp/prompts/get", timeout=30.0, json={"server": server, "name": name, "arguments": arguments})

    async def refresh_mcp_server(self, name: str) -> dict:
        """Refresh tools/resources/prompts for a connected MCP server."""
        return await self._post(f"/mcp/servers/{name}/refresh", timeout=30.0)
