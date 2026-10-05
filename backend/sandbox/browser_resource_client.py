"""Client for the independent finite browser-profile supervisor.

This client never acquires a Wuying desktop or a generic sandbox. The caller
owns SQL authorization and supplies an immutable, persisted remote identity.
The service credential belongs only to the backend; a human token alone is
insufficient to reach the supervisor.
"""
from contextlib import asynccontextmanager
import json
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets


PROTOCOL = "browser_resource_v1"


class BrowserResourceError(Exception):
    def __init__(self, status_code, code, detail, payload=None):
        self.status_code, self.code, self.detail = status_code, code, detail
        self.payload = payload or {"error": {"code": code, "detail": detail}}
        super().__init__(f"{code}: {detail}")


class BrowserResourceClient:
    def __init__(self, base_url, api_key, identity=None, *, timeout=15):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("A configured browser supervisor HTTP endpoint is required")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Browser supervisor base URL must not contain a path or query")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("A backend-only service credential is required")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.identity = dict(identity) if identity is not None else None
        self.timeout = timeout

    def _identity(self):
        if self.identity is None:
            raise BrowserResourceError(409, "BROWSER_IDENTITY_REQUIRED", "Persist the original supervisor identity first")
        return dict(self.identity)

    def _verify(self, payload, *, historical=False):
        if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
            raise BrowserResourceError(409, "BROWSER_PROTOCOL_UNAVAILABLE", "Invalid browser supervisor response")
        if not historical and self.identity is not None and payload.get("identity") != self.identity:
            raise BrowserResourceError(409, "BROWSER_IDENTITY_CHANGED", "The original browser runtime changed")
        return payload

    async def _request(self, method, path, *, body=None, historical=False):
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, trust_env=False) as client:
            response = await client.request(method, path, headers={"X-API-Key": self._api_key}, json=body)
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrowserResourceError(response.status_code, "BROWSER_PROTOCOL_UNAVAILABLE", "Invalid browser supervisor response") from exc
        if response.is_error:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            raise BrowserResourceError(response.status_code, error.get("code", "BROWSER_REQUEST_FAILED"),
                error.get("detail", "Browser request failed"), payload)
        return self._verify(payload, historical=historical)

    async def status(self):
        """Discovery is read-only; it never silently pins a new identity."""
        return await self._request("GET", "/v1/status")

    async def control(self, action, *, fence, command_id, actor_id,
                      next_owner_id=None, ttl_seconds=None, human_token=None):
        if action not in {"close", "takeover", "giveback", "heartbeat"}:
            raise ValueError("Unsupported finite browser control command")
        body = {"identity": self._identity(), "fence": dict(fence), "command_id": command_id,
                "actor_id": actor_id}
        for name, value in (("next_owner_id", next_owner_id), ("ttl_seconds", ttl_seconds),
                            ("human_token", human_token)):
            if value is not None:
                body[name] = value
        return await self._request("POST", f"/v1/control/{action}", body=body)

    def _operation(self, *, fence, operation_id, kind, args=None, human_token=None, observation_id=None):
        body = {"identity": self._identity(), "fence": dict(fence), "operation_id": operation_id,
                "kind": kind, "args": args or {}}
        for name, value in (("human_token", human_token), ("observation_id", observation_id)):
            if value is not None:
                body[name] = value
        return body

    async def operate(self, *, fence, operation_id, kind, args=None, human_token=None, observation_id=None):
        body = self._operation(fence=fence, operation_id=operation_id, kind=kind, args=args,
                               human_token=human_token, observation_id=observation_id)
        return await self._request("POST", "/v1/operations", body=body)

    async def operation_receipt(self, operation_id):
        from urllib.parse import quote
        return await self._request("GET", "/v1/operations/" + quote(operation_id, safe=""), historical=True)

    async def control_receipt(self, command_id):
        from urllib.parse import quote
        return await self._request("GET", "/v1/control/receipts/" + quote(command_id, safe=""), historical=True)

    @asynccontextmanager
    async def human_socket(self, *, fence, human_token):
        parsed = urlsplit(self.base_url)
        url = urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc, "/v1/ws", "", ""))
        async with websockets.connect(url, additional_headers={"X-API-Key": self._api_key},
                                      max_size=12 * 1024 * 1024, open_timeout=self.timeout) as socket:
            await socket.send(json.dumps({"identity": self._identity(), "fence": dict(fence),
                                          "human_token": human_token}))
            hello = json.loads(await socket.recv())
            if "error" in hello:
                error = hello["error"]
                raise BrowserResourceError(423, error["code"], error["detail"], hello)
            self._verify(hello)
            yield socket
