"""Interactive cloud-browser frames for the workbench Browser tab.

The existing ``dev-browser`` WebSocket is a Chrome-extension/CDP transport;
it does not carry screenshots and must remain untouched for Agent browser
automation. This module implements the separate UI protocol used by the
workbench: PNG frames plus validated navigation and input commands.

All commands run as the isolated sandbox runner. User input is validated and
then passed to ``xdotool`` as quoted argv; this is not a root or shell console.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import shlex
from contextlib import asynccontextmanager
from urllib.parse import quote, urlsplit, urlunsplit

from sandbox.browser import CHROME_PORT, ensure_chrome
from sandbox.desktop import ensure_desktop_tools, take_screenshot, to_native, x


FRAME_MAX_BYTES = 8 * 1024 * 1024

_NAMED_KEYS = {
    "Enter": "Return",
    "Backspace": "BackSpace",
    "Tab": "Tab",
    "Escape": "Escape",
    "Delete": "Delete",
    "ArrowLeft": "Left",
    "ArrowRight": "Right",
    "ArrowUp": "Up",
    "ArrowDown": "Down",
    "Home": "Home",
    "End": "End",
    "PageUp": "Prior",
    "PageDown": "Next",
}


class BrowserViewProtocolError(ValueError):
    """A browser-view client sent an unsupported or malformed command."""


def normalize_navigation_url(value: object) -> str:
    """Return one bounded http(s) URL suitable for the managed browser."""
    if not isinstance(value, str):
        raise BrowserViewProtocolError("A navigation URL is required")
    raw = value.strip()
    if not raw or len(raw) > 2048 or any(ord(ch) < 32 for ch in raw):
        raise BrowserViewProtocolError("The navigation URL is invalid")
    if "://" not in raw:
        scheme_like = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:(.*)$", raw)
        if scheme_like and not re.match(r"^\d+(?:/|$)", scheme_like.group(1)):
            raise BrowserViewProtocolError("Only http and https URLs are supported")
        raw = f"https://{raw}"
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise BrowserViewProtocolError("Only http and https URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserViewProtocolError("Credentials are not allowed in browser URLs")
    # Fragments never affect the network request and can carry large local-only
    # payloads, so they do not cross the control boundary.
    path = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&?/:;+,%@!$'()*-._~")
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, path, query, ""))


def _bounded_number(value: object, *, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrowserViewProtocolError(f"{name} must be a number")
    if not math.isfinite(float(value)):
        raise BrowserViewProtocolError(f"{name} must be finite")
    return max(minimum, min(maximum, round(float(value))))


def _xdotool_program(parts: list[str]) -> str:
    program = "set -e; " + "; ".join(parts)
    return x("sh -c " + shlex.quote(program))


class BrowserViewController:
    """One live Browser-tab controller backed by a tenant-scoped sandbox."""

    def __init__(self, client, *, container_key: str, user_scope: str):
        if not user_scope.startswith("u-"):
            raise ValueError("browser view requires a tenant-scoped sandbox client")
        self.client = client
        self.container_key = container_key
        self.user_scope = user_scope
        self.connection_id = secrets.token_hex(8)
        self.owner_session = f"browser-view-{self.connection_id}"
        self.frame_dir = f"/workspace/openbox/users/{user_scope}/.browser-view"
        self.frame_path = f"{self.frame_dir}/frame-{self.connection_id}.png"
        self.geometry: dict | None = None
        self._io_lock = asyncio.Lock()

    async def start(self) -> str | None:
        """Ensure the desktop tools and headed Chrome are available."""
        async with self._desktop_lease("browser-view-start", ttl_seconds=60.0):
            async with self._io_lock:
                await ensure_desktop_tools(self.client, self.container_key)
                await ensure_chrome(self.client, self.container_key)
                result = await self.client.execute(
                    f"install -d -m 0700 -- {shlex.quote(self.frame_dir)}",
                    timeout=20,
                    workdir="/workspace",
                )
                if result.exit_code != 0:
                    raise RuntimeError((result.stderr or "Could not prepare browser frames")[:300])
                # Fixed command: bring the managed Chrome to the foreground. A
                # fresh launch is already active, so failure is harmless.
                await self.client.execute(
                    x("wmctrl -xa google-chrome.Google-chrome || true"),
                    timeout=15,
                    workdir="/workspace",
                )
        return await self.current_url()

    async def current_url(self) -> str | None:
        """Best-effort URL of the first normal Chrome page target."""
        result = await self.client.execute(
            f"curl -s --max-time 2 http://127.0.0.1:{CHROME_PORT}/json/list",
            timeout=10,
            workdir="/workspace",
        )
        if result.exit_code != 0:
            return None
        try:
            targets = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(targets, list):
            return None
        for target in targets:
            if not isinstance(target, dict) or target.get("type") != "page":
                continue
            url = target.get("url")
            if isinstance(url, str) and url:
                return url
        return None

    async def capture(self) -> tuple[dict, bytes]:
        """Capture and download one bounded PNG frame."""
        async with self._desktop_lease("browser-view-capture"):
            async with self._io_lock:
                geometry = await take_screenshot(self.client, self.frame_path)
                frame = await self.client.download_file_bytes(
                    self.frame_path,
                    max_bytes=FRAME_MAX_BYTES,
                )
                if not frame.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise RuntimeError("Cloud desktop returned an invalid browser frame")
                self.geometry = geometry
                return geometry, frame

    @asynccontextmanager
    async def _desktop_lease(self, operation: str, *, ttl_seconds: float = 20.0):
        async with self.client.desktop_lease(
            session_id=self.owner_session,
            tool_call_id=self.connection_id,
            operation=operation,
            wait_timeout=5.0,
            ttl_seconds=ttl_seconds,
        ):
            yield

    async def _execute_input(self, command: str) -> None:
        async with self._desktop_lease("browser-view-input"):
            async with self._io_lock:
                result = await self.client.execute(command, timeout=20, workdir="/workspace")
        if result.exit_code != 0:
            raise RuntimeError((result.stderr or result.stdout or "Browser input failed")[:300])

    async def handle(self, message: object) -> dict | None:
        """Validate and execute one client command."""
        if not isinstance(message, dict):
            raise BrowserViewProtocolError("Browser command must be an object")
        kind = message.get("type")

        if kind == "navigate":
            url = normalize_navigation_url(message.get("url"))
            command = _xdotool_program([
                "xdotool key --clearmodifiers ctrl+l",
                f"xdotool type --clearmodifiers --delay 5 -- {shlex.quote(url)}",
                "xdotool key --clearmodifiers Return",
            ])
            await self._execute_input(command)
            return {"type": "navigated", "url": url}

        if kind == "back":
            await self._execute_input(x("xdotool key --clearmodifiers alt+Left"))
            return None

        if kind == "reload":
            await self._execute_input(x("xdotool key --clearmodifiers ctrl+r"))
            return None

        if kind == "click":
            if self.geometry is None:
                raise BrowserViewProtocolError("No browser frame is ready yet")
            scaled_w, scaled_h = self.geometry["scaled"]
            px = _bounded_number(message.get("x"), minimum=0, maximum=scaled_w - 1, name="x")
            py = _bounded_number(message.get("y"), minimum=0, maximum=scaled_h - 1, name="y")
            button = _bounded_number(message.get("button", 0), minimum=0, maximum=2, name="button") + 1
            nx, ny = to_native(px, py, self.geometry)
            await self._execute_input(x(f"xdotool mousemove {nx} {ny} click {button}"))
            return None

        if kind == "scroll":
            dx = _bounded_number(message.get("dx", 0), minimum=-5000, maximum=5000, name="dx")
            dy = _bounded_number(message.get("dy", 0), minimum=-5000, maximum=5000, name="dy")
            parts: list[str] = []
            if dy:
                button = 5 if dy > 0 else 4
                amount = max(1, min(20, math.ceil(abs(dy) / 100)))
                parts.append(f"xdotool click --repeat {amount} --delay 35 {button}")
            if dx:
                button = 7 if dx > 0 else 6
                amount = max(1, min(20, math.ceil(abs(dx) / 100)))
                parts.append(f"xdotool click --repeat {amount} --delay 35 {button}")
            if parts:
                await self._execute_input(_xdotool_program(parts))
            return None

        if kind == "key":
            key = message.get("key")
            if not isinstance(key, str):
                raise BrowserViewProtocolError("A keyboard key is required")
            named = _NAMED_KEYS.get(key)
            if named:
                command = x(f"xdotool key --clearmodifiers -- {named}")
            elif len(key) == 1 and key.isprintable() and key not in {"\r", "\n"}:
                command = x("xdotool type --clearmodifiers --delay 5 -- " + shlex.quote(key))
            else:
                raise BrowserViewProtocolError("Unsupported keyboard key")
            await self._execute_input(command)
            return None

        raise BrowserViewProtocolError("Unsupported browser command")

    async def close(self) -> None:
        try:
            await self.client.delete_file(self.frame_path)
        except Exception:
            # The file is transient and tenant-confined. A dropped tunnel may
            # leave it until the next connection reuses/cleans the directory.
            pass
