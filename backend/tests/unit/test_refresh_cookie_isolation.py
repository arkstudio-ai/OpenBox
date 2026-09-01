"""Refresh cookies cannot be shadowed by a sibling preview hostname."""

from http.cookies import SimpleCookie
from types import SimpleNamespace

from fastapi import Response
from starlette.requests import Request

from auth import routes


def _request(cookie: str = "") -> Request:
    headers = [(b"cookie", cookie.encode("latin-1"))] if cookie else []
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/auth/logout",
            "raw_path": b"/api/auth/logout",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("app.example.test", 443),
        }
    )


def _cookie_headers(response: Response) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in response.raw_headers
        if key.lower() == b"set-cookie"
    ]


def test_default_local_mode_keeps_legacy_refresh_cookie(monkeypatch):
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(preview_public_origin="", auth_cookie_secure=False),
    )
    response = Response()
    routes._set_refresh_cookie(response, "legacy-token")

    parsed = SimpleCookie()
    parsed.load(response.headers["set-cookie"])
    cookie = parsed["refresh_token"]
    assert cookie.value == "legacy-token"
    assert cookie["path"] == "/api/auth"
    assert cookie["httponly"] is True
    assert cookie["secure"] == ""
    assert routes._get_refresh_cookie(_request("refresh_token=legacy-token")) == "legacy-token"


def test_isolated_preview_uses_host_prefix_and_ignores_legacy_cookie(monkeypatch):
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(
            preview_public_origin="https://preview.example.test",
            auth_cookie_secure=False,
        ),
    )
    response = Response()
    routes._set_refresh_cookie(response, "host-token")

    parsed = SimpleCookie()
    parsed.load(response.headers["set-cookie"])
    cookie = parsed["__Host-openbox_refresh_token"]
    assert cookie.value == "host-token"
    assert cookie["domain"] == ""
    assert cookie["path"] == "/"
    assert cookie["httponly"] is True
    assert cookie["secure"] is True

    assert routes._get_refresh_cookie(_request("refresh_token=attacker")) is None
    assert routes._get_refresh_cookie(
        _request("refresh_token=attacker; __Host-openbox_refresh_token=host-token")
    ) == "host-token"


async def test_isolated_logout_clears_host_and_transition_cookie(monkeypatch):
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(
            preview_public_origin="https://preview.example.test",
            auth_cookie_secure=False,
        ),
    )

    async def revoke(user_id: str):
        assert user_id == "user-1"
        return 1

    monkeypatch.setattr(routes, "revoke_preview_tokens", revoke)
    monkeypatch.setattr(routes, "_cache", None)
    response = Response()

    await routes.logout(
        _request("__Host-openbox_refresh_token=not-a-jwt"),
        response,
        {"user_id": "user-1"},
    )

    headers = _cookie_headers(response)
    assert any(
        header.startswith("__Host-openbox_refresh_token=")
        and "Path=/" in header
        and "Secure" in header
        and "Max-Age=0" in header
        for header in headers
    )
    assert any(
        header.startswith("refresh_token=")
        and "Path=/api/auth" in header
        and "Max-Age=0" in header
        for header in headers
    )
