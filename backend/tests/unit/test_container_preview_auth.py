import json
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from api import containers
from auth.preview_token import (
    create_preview_token,
    init_preview_store,
    revoke_preview_tokens,
    verify_preview_token,
)
from cache.memory_cache import MemoryCache
from models.container import ContainerInfo, ContainerListResponse, ContainerStatus


def _request(
    path: str,
    *,
    query: str = "",
    cookie: str = "",
    scheme: str = "http",
    extra_headers: dict[str, str] | None = None,
) -> Request:
    headers = []
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    for key, value in (extra_headers or {}).items():
        headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443 if scheme == "https" else 80),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


class _FakeProvider:
    def __init__(self):
        self.calls: list[tuple[str, str | None]] = []
        self.denied_user: str | None = None

    async def get_container(self, container_id: str, user_id: str | None = None):
        self.calls.append((container_id, user_id))
        if user_id == self.denied_user:
            raise PermissionError
        return ContainerInfo(
            id=container_id,
            name="preview-test",
            status=ContainerStatus.RUNNING,
            image="test",
            created_at=datetime.now(timezone.utc),
            host="sandbox.internal",
            port=9000,
            api_key="sandbox-key",
        )


class _FakeUpstreamClient:
    def __init__(self, captures: list[dict]):
        self._captures = captures

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, **kwargs):
        self._captures.append(kwargs)
        return SimpleNamespace(
            status_code=200,
            headers={
                "content-type": "text/plain",
                "set-cookie": "sandbox_session=must-not-escape; Path=/",
                "clear-site-data": '"cookies"',
                "service-worker-allowed": "/",
            },
            content=b"preview ok",
        )


def test_public_container_response_never_serializes_transport_credentials():
    internal = ContainerInfo(
        id="c1",
        name="preview-test",
        status=ContainerStatus.RUNNING,
        image="test",
        created_at=datetime.now(timezone.utc),
        host="sandbox.internal",
        port=9000,
        api_key="sandbox-secret",
    )

    payload = ContainerListResponse(
        containers=[internal],
        total=1,
    ).model_dump(mode="json")

    assert payload["containers"][0] == {
        "id": "c1",
        "name": "preview-test",
        "status": "running",
        "image": "test",
        "created_at": internal.created_at.isoformat().replace("+00:00", "Z"),
        "port": 9000,
    }


@pytest.fixture
def preview_env(monkeypatch):
    cache = MemoryCache()
    init_preview_store(cache)

    fake_provider = _FakeProvider()
    captures: list[dict] = []
    monkeypatch.setattr(containers, "provider", fake_provider)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **_kwargs: _FakeUpstreamClient(captures),
    )
    return cache, fake_provider, captures


async def test_preview_without_token_is_unauthorized(preview_env):
    request = _request("/api/containers/c1/preview/3000/")

    with pytest.raises(HTTPException) as exc_info:
        await containers.preview_proxy(request, "c1", 3000)

    assert exc_info.value.status_code == 401
    _cache, provider, captures = preview_env
    assert provider.calls == []
    assert captures == []


async def test_preview_rejects_token_bound_to_another_target(preview_env):
    token = await create_preview_token("user-1", "c1", 3000)
    request = _request(
        "/api/containers/c2/preview/3000/",
        cookie=f"{containers.PREVIEW_COOKIE_NAME}={token}",
    )

    with pytest.raises(HTTPException) as exc_info:
        await containers.preview_proxy(request, "c2", 3000)

    assert exc_info.value.status_code == 403
    _cache, provider, captures = preview_env
    assert provider.calls == []
    assert captures == []


async def test_preview_rejects_unknown_or_wrong_owner_token(preview_env):
    unknown_request = _request(
        "/api/containers/c1/preview/3000/",
        query="_pt=not-a-real-token",
    )
    with pytest.raises(HTTPException) as exc_info:
        await containers.preview_proxy(unknown_request, "c1", 3000)
    assert exc_info.value.status_code == 401

    _cache, provider, captures = preview_env
    provider.denied_user = "user-2"
    token = await create_preview_token("user-2", "c1", 3000)
    owner_request = _request(
        "/api/containers/c1/preview/3000/",
        cookie=f"{containers.PREVIEW_COOKIE_NAME}={token}",
    )
    with pytest.raises(HTTPException) as exc_info:
        await containers.preview_proxy(owner_request, "c1", 3000)
    assert exc_info.value.status_code == 403
    assert provider.calls == [("c1", "user-2")]
    assert captures == []


async def test_management_endpoint_sets_scoped_cookie_and_cookie_authorizes_subresource(
    preview_env,
):
    management_request = _request(
        "/api/containers/c1/preview-token",
    )

    response = await containers.create_preview_access_token(
        management_request,
        "c1",
        3000,
        {"user_id": "user-1"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = json.loads(response.body)
    assert payload == {
        "url": "/api/containers/c1/preview/3000/",
        "mode": "sandboxed_same_origin",
    }
    assert "_pt" not in response.body.decode()
    parsed_cookie = SimpleCookie()
    parsed_cookie.load(response.headers["set-cookie"])
    preview_cookie = parsed_cookie[containers.PREVIEW_COOKIE_NAME]
    token = preview_cookie.value
    assert preview_cookie["httponly"] is True
    assert preview_cookie["samesite"].lower() == "lax"
    assert preview_cookie["path"] == "/api/containers/c1/preview/3000"

    _cache, provider, captures = preview_env
    assert provider.calls == [("c1", "user-1")]
    assert captures == []

    child_request = _request(
        "/api/containers/c1/preview/3000/assets/app.js",
        cookie=(
            f"{containers.PREVIEW_COOKIE_NAME}={preview_cookie.value}; "
            "app_session=abc"
        ),
        extra_headers={
            "authorization": "Bearer must-not-reach-sandbox",
            "referer": (
                "http://testserver/api/containers/c1/preview/3000/"
                f"?_pt={token}&theme=dark"
            ),
        },
    )
    child_response = await containers.preview_proxy(
        child_request,
        "c1",
        3000,
        "assets/app.js",
    )

    assert child_response.status_code == 200
    assert "set-cookie" not in child_response.headers
    assert "clear-site-data" not in child_response.headers
    assert "service-worker-allowed" not in child_response.headers
    assert child_response.headers["cache-control"] == "private, no-store"
    assert provider.calls == [("c1", "user-1"), ("c1", "user-1")]
    assert captures[0]["headers"]["cookie"] == "app_session=abc"
    assert "authorization" not in captures[0]["headers"]
    assert captures[0]["headers"]["referer"].endswith("?theme=dark")
    assert token not in repr(captures[0])
    assert captures[0]["url"].endswith("/proxy/3000/assets/app.js")


async def test_https_preview_cookie_is_secure(preview_env):
    request = _request(
        "/api/containers/c1/preview-token",
        scheme="https",
    )

    response = await containers.create_preview_access_token(
        request,
        "c1",
        3000,
        {"user_id": "user-1"},
    )

    assert "secure" in response.headers["set-cookie"].lower()


async def test_preview_revocation_epoch_invalidates_existing_tokens(preview_env):
    token = await create_preview_token("user-1", "c1", 3000)
    assert await verify_preview_token(token, "c1", 3000) is not None

    assert await revoke_preview_tokens("user-1") == 1
    assert await verify_preview_token(token, "c1", 3000) is None

    replacement = await create_preview_token("user-1", "c1", 3000)
    assert await verify_preview_token(replacement, "c1", 3000) is not None


async def test_preview_rejects_legacy_claims_without_revocation_epoch(preview_env):
    cache, _provider, _captures = preview_env
    await cache.set(
        "pt:legacy",
        json.dumps({"user_id": "user-1", "container_id": "c1", "port": 3000}),
        ttl=3600,
    )

    assert await verify_preview_token("legacy", "c1", 3000) is None


async def test_logout_revokes_preview_tokens_for_both_auth_modes(preview_env):
    from auth.routes import init_auth_routes, logout as authenticated_logout
    from auth.single_user import logout as single_user_logout

    cache, _provider, _captures = preview_env
    init_auth_routes(cache)

    user_token = await create_preview_token("user-1", "c1", 3000)
    await authenticated_logout(
        _request("/api/auth/logout"),
        Response(),
        {"user_id": "user-1"},
    )
    assert await verify_preview_token(user_token, "c1", 3000) is None

    default_token = await create_preview_token("default", "c1", 3000)
    await single_user_logout()
    assert await verify_preview_token(default_token, "c1", 3000) is None


async def test_single_user_startup_initializes_one_preview_token_store(monkeypatch):
    from cache import get_cache, set_cache
    from main import _cleanup_infrastructure, _init_infrastructure

    previous_cache = get_cache()
    set_cache(None)
    config = SimpleNamespace(jwt_secret=None)

    async def no_op_close_engine():
        return None

    monkeypatch.setattr("db.base.close_engine", no_op_close_engine)
    try:
        _init_infrastructure(config)
        cache = config._cache
        assert isinstance(cache, MemoryCache)
        assert get_cache() is cache

        # Re-initializing the same application config must reuse, not replace,
        # its cache-backed token store.
        _init_infrastructure(config)
        assert config._cache is cache

        token = await create_preview_token("default", "shared", 3000)

        assert await verify_preview_token(token, "shared", 3000) == {
            "user_id": "default",
            "container_id": "shared",
            "port": 3000,
        }

        await _cleanup_infrastructure(config)
        assert config._cache is None
        assert get_cache() is None
        with pytest.raises(RuntimeError, match="store not initialized"):
            await create_preview_token("default", "closed", 3000)
        with pytest.raises(RuntimeError, match="store not initialized"):
            await revoke_preview_tokens("default")
        assert await verify_preview_token(token, "shared", 3000) is None

        # Cleanup is safe when shutdown hooks are invoked more than once.
        await _cleanup_infrastructure(config)
    finally:
        set_cache(previous_cache)
        init_preview_store(previous_cache)
