"""Dedicated-origin boundary for untrusted sandbox application previews."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


_PREVIEW_TOKEN_PATH = re.compile(
    r"^/api/containers/[^/]+/preview-token/?$"
)
_PREVIEW_PROXY_PATH = re.compile(
    r"^/api/containers/[^/]+/preview/[1-9][0-9]{0,4}(?:/.*)?$"
)
_PREVIEW_PROXY_METHODS = frozenset(
    {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"}
)


def canonical_http_origin(value: str) -> str:
    """Return an exact scheme/authority origin or raise for an unsafe URL."""
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("origin must be an absolute http(s) origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("origin must not contain user info")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("origin must not contain a path, query, or fragment")
    # urlsplit validates bracketed IPv6 and exposes invalid ports lazily.
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("origin contains an invalid port") from exc
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("origin must include a hostname")
    if ":" in hostname:
        authority = f"[{hostname.lower()}]"
    else:
        try:
            authority = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("origin contains an invalid hostname") from exc
    port = parsed.port
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme.lower()}://{authority}"


def preview_origin_host(preview_public_origin: str) -> str:
    if not preview_public_origin:
        return ""
    return urlsplit(canonical_http_origin(preview_public_origin)).netloc


def origin_hostname(origin: str) -> str:
    hostname = urlsplit(canonical_http_origin(origin)).hostname
    if hostname is None:  # canonical_http_origin already rejects this
        raise ValueError("origin must include a hostname")
    return hostname.lower()


def request_host(headers: Iterable[tuple[bytes, bytes]]) -> str:
    """Read Host directly; forwarded host headers do not widen this boundary."""
    for key, value in headers:
        if key.lower() == b"host":
            return value.decode("latin-1").strip().lower()
    return ""


def is_preview_token_path(path: str) -> bool:
    return _PREVIEW_TOKEN_PATH.fullmatch(path) is not None


def is_preview_proxy_path(path: str) -> bool:
    return _PREVIEW_PROXY_PATH.fullmatch(path) is not None


def origin_is_allowed(origin: str, allowed_origins: Iterable[str]) -> bool:
    """Use exact origins only; wildcard/regex CORS is not an auth signal."""
    if not origin:
        return False
    try:
        candidate = canonical_http_origin(origin)
    except ValueError:
        return False
    for allowed in allowed_origins:
        if allowed == "*":
            continue
        try:
            if canonical_http_origin(allowed) == candidate:
                return True
        except ValueError:
            continue
    return False


class PreviewOriginIsolationMiddleware:
    """Make the configured preview Host a fail-closed, preview-only plane.

    The same FastAPI process can sit behind two virtual hosts.  On the preview
    host only the cookie-seeding POST, the authenticated proxy, and liveness
    are reachable.  On every other host those preview routes are unavailable,
    preventing an accidental same-origin fallback when deployment routing is
    incomplete.
    """

    def __init__(self, app, *, preview_public_origin: str):
        self.app = app
        self.preview_host = preview_origin_host(preview_public_origin)

    async def __call__(self, scope, receive, send):
        if not self.preview_host or scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        host = request_host(scope.get("headers", ()))
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        on_preview_host = host == self.preview_host

        if on_preview_host:
            allowed = (
                (path == "/health" and method in {"GET", "HEAD"})
                or (is_preview_token_path(path) and method in {"POST", "OPTIONS"})
                or (is_preview_proxy_path(path) and method in _PREVIEW_PROXY_METHODS)
            )
        else:
            # Once a dedicated origin is configured, credentials and sandbox
            # bytes must never be served from an app/control-plane origin.
            allowed = not (
                is_preview_token_path(path) or is_preview_proxy_path(path)
            )

        if allowed:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return

        response = JSONResponse(
            status_code=404,
            content={"detail": "Not found"},
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)


class ControlPlaneFrameGuardMiddleware:
    """Prevent a preview frame from navigating itself onto the control plane."""

    def __init__(self, app, *, preview_public_origin: str = ""):
        self.app = app
        self.preview_host = preview_origin_host(preview_public_origin)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        host = request_host(scope.get("headers", ()))
        path = scope.get("path", "")
        # The preview document itself must be frameable. All other backend
        # documents/API responses are control-plane surfaces and are denied.
        guard = host != self.preview_host and not is_preview_proxy_path(path)

        async def send_guarded(message):
            if guard and message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                headers.extend(
                    [
                        (b"content-security-policy", b"frame-ancestors 'none'"),
                        (b"x-frame-options", b"DENY"),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_guarded)
