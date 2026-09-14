"""Trajectory admin authorization: local JWT checks, backend viewer facts, the facts cache and revalidation."""
import asyncio
import json
import time
from datetime import timedelta

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt
from starlette.requests import Request as StarletteRequest

import auth.middleware as middleware
import trajectory.auth as trajectory_auth
from auth.jwt import create_refresh_token, decode_access_token
from core.config import OpenBoxConfig
from db.models.push import MobileSession
from tests.unit.test_worker_app_harness import (SECRET, admin_env, auth_stores, business_db,  # noqa: F401
    internal_backend, token)
from trajectory.auth import (AuditRejected, HttpBackend, LocalBackend, assert_admin, authenticate, configure_backend,
    require_trajectory_admin, revalidate_viewer, viewer_facts)
from trajectory.types import now

ADMIN_FACTS = {"role": "admin", "is_active": True, "is_deleted": False, "mobile_session_valid": True,
               "admin_enabled": True}


class FakeBackend:
    def __init__(self):
        self.facts = {"admin": dict(ADMIN_FACTS)}
        self.calls: list[tuple] = []
        self.error: Exception | None = None
        self.hold_first: asyncio.Event | None = None

    async def viewer(self, user_id, *, client, sid, jti):
        self.calls.append((user_id, client, sid, jti))
        facts = dict(self.facts[user_id])
        if self.hold_first is not None and len(self.calls) == 1:
            await self.hold_first.wait()
        if self.error is not None:
            raise self.error
        return {"user_id": user_id, **facts}

    async def audit(self, entries):
        return None

    async def close(self):
        return None


@pytest.fixture
def fake_backend(auth_stores, monkeypatch):
    backend = FakeBackend()
    configure_backend(backend)
    yield backend
    configure_backend(None)


def request(headers: dict | None = None) -> StarletteRequest:
    raw = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    return StarletteRequest({"type": "http", "method": "GET", "path": "/", "headers": raw, "query_string": b""})


def bearer(value: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=value)


async def refused(call) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        await call
    return caught.value


async def test_bearer_tokens_are_verified_locally(auth_stores):
    error = await refused(authenticate(request(), None))
    assert (error.status_code, error.detail) == (401, "Not authenticated")
    expired = jwt.encode({"sub": "admin", "type": "access", "exp": int(time.time()) - 5, "jti": "old"}, SECRET)
    foreign = jwt.encode({"sub": "admin", "type": "access", "exp": int(time.time()) + 60}, "another-secret")
    for value in ("not-a-jwt", create_refresh_token("admin"), expired, foreign):
        error = await refused(authenticate(request(), bearer(value)))
        assert (error.status_code, error.detail) == (401, "Invalid or expired token")
    anonymous = jwt.encode({"type": "access", "exp": int(time.time()) + 60}, SECRET)
    error = await refused(authenticate(request(), bearer(anonymous)))
    assert (error.status_code, error.detail) == (401, "Invalid token payload")

    value = token("admin")
    claims = decode_access_token(value)
    assert await authenticate(request(), bearer(value)) == {"user_id": "admin", "client": "web", "sid": None,
                                                            "jti": claims["jti"], "exp": claims["exp"]}
    error = await refused(authenticate(request({"X-Client-Type": "mobile"}), bearer(value)))
    assert error.status_code == 401 and error.headers == {"X-Error-Code": "AUTH_MOBILE_LOGIN_REQUIRED"}
    phone = await authenticate(request({"X-Client-Type": "mobile"}), bearer(token("admin", client="mobile", sid="s1")))
    assert (phone["client"], phone["sid"]) == ("mobile", "s1")
    await auth_stores.set(f"jwt_bl:{claims['jti']}", True, ttl=60)
    error = await refused(authenticate(request(), bearer(value)))
    assert (error.status_code, error.detail) == (401, "Token has been revoked")


async def test_an_unreachable_blacklist_fails_closed(auth_stores, monkeypatch):
    async def broken(key):
        raise ConnectionError("redis down")

    monkeypatch.setattr(auth_stores, "exists", broken)
    error = await refused(authenticate(request(), bearer(token("admin"))))
    assert (error.status_code, error.detail) == (503, "Trajectory authorization is unavailable")


async def test_backend_facts_decide_and_role_claims_never_grant(fake_backend):
    fake_backend.facts.update({
        "member": {**ADMIN_FACTS, "role": "user"},
        "retired": {**ADMIN_FACTS, "is_deleted": True},
        "suspended": {**ADMIN_FACTS, "is_active": False},
        "phone": {**ADMIN_FACTS, "mobile_session_valid": False},
        "closed": {**ADMIN_FACTS, "admin_enabled": False},
    })
    assert await assert_admin("admin") == {"user_id": "admin", "role": "admin"}
    error = await refused(assert_admin("member"))
    assert (error.status_code, error.detail) == (403, "Platform administrator access required")
    for user_id in ("retired", "suspended"):
        error = await refused(assert_admin(user_id))
        assert (error.status_code, error.detail) == (401, "Account is inactive or deleted")
    replaced = await refused(assert_admin("phone", client="mobile", sid="s1"))
    assert replaced.status_code == 401 and replaced.detail["code"] == "AUTH_MOBILE_SESSION_REPLACED"
    assert replaced.headers == {"X-Error-Code": "AUTH_MOBILE_SESSION_REPLACED"}
    legacy = await refused(assert_admin("phone", client=None))
    assert legacy.status_code == 401 and legacy.detail["code"] == "AUTH_MOBILE_LOGIN_REQUIRED"
    error = await refused(assert_admin("closed"))
    assert (error.status_code, error.detail) == (404, "Trajectory administration is disabled")
    assert fake_backend.calls[0] == ("admin", "web", None, None)
    assert ("phone", "mobile", "s1", None) in fake_backend.calls


@pytest.mark.parametrize("answer,status", [
    (ConnectionError("backend down"), 503),
    ("not an object", 503),
    ({"user_id": "somebody-else", **ADMIN_FACTS}, 503),
    ({"user_id": "admin", "role": "admin"}, 401),  # missing account facts deny
])
async def test_malformed_or_unreachable_facts_fail_closed(fake_backend, answer, status):
    async def viewer(user_id, *, client, sid, jti):
        if isinstance(answer, Exception):
            raise answer
        return answer

    fake_backend.viewer = viewer
    assert (await refused(assert_admin("admin"))).status_code == status


async def test_facts_are_cached_per_viewer_client_and_session(fake_backend, monkeypatch):
    moment = [100.0]
    monkeypatch.setattr(trajectory_auth, "_clock", lambda: moment[0])
    await assert_admin("admin")
    await assert_admin("admin")
    assert len(fake_backend.calls) == 1
    await assert_admin("admin", client="mobile", sid="s1")
    await assert_admin("admin", client="mobile", sid="s2")
    assert len(fake_backend.calls) == 3
    fake_backend.facts["admin"]["role"] = "user"
    moment[0] += 4.9
    assert await assert_admin("admin") == {"user_id": "admin", "role": "admin"}  # within the 5 s window
    moment[0] += 0.2
    assert (await refused(assert_admin("admin"))).status_code == 403
    monkeypatch.setenv("TRAJECTORY_AUTH_CACHE_SECONDS", "1")
    fake_backend.facts["admin"]["role"] = "admin"
    moment[0] += 1.0
    assert await assert_admin("admin") == {"user_id": "admin", "role": "admin"}


async def test_revalidation_bypasses_and_refreshes_the_cache(fake_backend):
    viewer = request({"Authorization": f"Bearer {token('admin')}"})
    await assert_admin("admin")
    fake_backend.facts["admin"]["role"] = "user"
    assert await assert_admin("admin") == {"user_id": "admin", "role": "admin"}
    assert (await refused(revalidate_viewer(viewer, "admin"))).status_code == 403
    assert (await refused(assert_admin("admin"))).status_code == 403  # the fresh facts replaced the cached ones
    fake_backend.facts["admin"]["role"] = "admin"
    assert await revalidate_viewer(viewer, "admin") == {"user_id": "admin", "role": "admin"}
    fake_backend.facts["b"] = dict(ADMIN_FACTS)
    other = request({"Authorization": f"Bearer {token('b', 'admin')}"})
    for candidate in (other, request()):
        error = await refused(revalidate_viewer(candidate, "admin"))
        assert (error.status_code, error.detail) == (401, "Authentication is no longer valid")


async def test_an_older_slower_lookup_never_replaces_newer_facts(fake_backend, monkeypatch):
    moment = [10.0]
    monkeypatch.setattr(trajectory_auth, "_clock", lambda: moment[0])
    fake_backend.hold_first = asyncio.Event()
    slow = asyncio.create_task(viewer_facts("admin"))
    while not fake_backend.calls:
        await asyncio.sleep(0)
    fake_backend.facts["admin"]["role"] = "user"
    moment[0] = 11.0
    assert (await viewer_facts("admin", fresh=True))["role"] == "user"
    fake_backend.hold_first.set()
    assert (await slow)["role"] == "admin"
    assert (await viewer_facts("admin"))["role"] == "user"


async def test_the_dependency_marks_responses_no_store_and_keeps_the_identity(fake_backend):
    app = FastAPI()

    @app.get("/probe")
    async def probe(request: Request, viewer: dict = Depends(require_trajectory_admin)):
        return {"viewer": viewer, "client": request.state.trajectory_identity["client"]}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/probe", headers={"Authorization": f"Bearer {token('admin')}"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"viewer": {"user_id": "admin", "role": "admin"}, "client": "web"}


async def test_desktop_mode_without_jwt_uses_the_default_admin(business_db, admin_env, monkeypatch):
    monkeypatch.setattr(middleware, "_auth_enabled", False)
    monkeypatch.setattr(trajectory_auth, "_backend", None)
    monkeypatch.setattr(trajectory_auth, "_facts", {})
    assert await authenticate(request(), None) == {"user_id": "default", "client": "web", "sid": None, "jti": None,
                                                   "exp": None}
    assert await require_trajectory_admin(request(), Response(), None) == {"user_id": "default", "role": "admin"}
    assert await revalidate_viewer(request(), "default") == {"user_id": "default", "role": "admin"}
    monkeypatch.setenv("TRAJECTORY_ADMIN_ENABLED", "false")
    trajectory_auth.clear_viewer_cache()
    assert (await refused(require_trajectory_admin(request(), Response(), None))).status_code == 404


async def test_http_and_in_process_backends_report_the_same_facts(internal_backend, business_db, admin_env,
                                                                  monkeypatch):
    async with business_db.begin() as db:
        db.add(MobileSession(user_id="a", session_id="sid_a", active=True, expires_at=now() + timedelta(days=1),
                             updated_at=now()))
    monkeypatch.setenv("TRAJECTORY_ADMIN_USER_IDS", "admin,retired")
    http, local = internal_backend.client(), LocalBackend()
    expected = {
        ("admin", "web", None): {"role": "admin", "is_active": True, "is_deleted": False,
                                 "mobile_session_valid": True, "admin_enabled": True},
        ("a", "mobile", "sid_a"): {"role": "user", "is_active": True, "is_deleted": False,
                                   "mobile_session_valid": True, "admin_enabled": False},
        ("a", "mobile", "stale"): {"role": "user", "is_active": True, "is_deleted": False,
                                   "mobile_session_valid": False, "admin_enabled": False},
        ("a", None, None): {"role": "user", "is_active": True, "is_deleted": False,
                            "mobile_session_valid": False, "admin_enabled": False},
        ("b", None, None): {"role": "user", "is_active": True, "is_deleted": False,
                            "mobile_session_valid": True, "admin_enabled": False},
        ("default", "tv", None): {"role": "admin", "is_active": True, "is_deleted": False,
                                  "mobile_session_valid": False, "admin_enabled": False},
        ("retired", "web", None): {"role": "admin", "is_active": True, "is_deleted": True,
                                   "mobile_session_valid": True, "admin_enabled": True},
        ("suspended", "web", None): {"role": "admin", "is_active": False, "is_deleted": False,
                                     "mobile_session_valid": True, "admin_enabled": False},
        ("ghost", "web", None): {"role": None, "is_active": False, "is_deleted": True,
                                 "mobile_session_valid": True, "admin_enabled": False},
    }
    try:
        for (user_id, client, sid), facts in expected.items():
            answer = {"user_id": user_id, **facts}
            assert await http.viewer(user_id, client=client, sid=sid, jti="jti-1") == answer, (user_id, client, sid)
            assert await local.viewer(user_id, client=client, sid=sid, jti="jti-1") == answer
    finally:
        await http.close()
    assert internal_backend.viewer_calls() == len(expected)


async def test_http_backend_request_shape_and_failure_classes(monkeypatch):
    seen = []
    status = {"viewer": 200, "audit": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        kind = request.url.path.rsplit("/", 1)[1]
        seen.append((request.method, request.url.path, request.headers["x-internal-token"], json.loads(request.content)))
        return httpx.Response(status[kind], json={"user_id": "admin", **ADMIN_FACTS} if kind == "viewer" else {})

    backend = HttpBackend("http://backend:8080/", "token-1", transport=httpx.MockTransport(handler))
    assert await backend.viewer("admin", client="mobile", sid="s", jti="j") == {"user_id": "admin", **ADMIN_FACTS}
    await backend.audit([{"id": "e1"}])
    assert seen == [("POST", "/api/internal/trajectory/viewer", "token-1",
                     {"user_id": "admin", "client": "mobile", "sid": "s", "jti": "j"}),
                    ("POST", "/api/internal/trajectory/audit", "token-1", {"entries": [{"id": "e1"}]})]
    status["viewer"] = 403
    with pytest.raises(httpx.HTTPStatusError):
        await backend.viewer("admin", client="web", sid=None, jti=None)
    for code, error in ((400, AuditRejected), (413, AuditRejected), (422, AuditRejected), (401, httpx.HTTPStatusError),
                        (404, httpx.HTTPStatusError), (503, httpx.HTTPStatusError)):
        status["audit"] = code
        with pytest.raises(error):
            await backend.audit([{"id": "e1"}])
    await backend.close()

    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig(internal_api_token="configured"))
    monkeypatch.setenv("TRAJECTORY_BACKEND_INTERNAL_URL", "http://backend.internal:9000/")
    configured = HttpBackend.from_env()
    assert str(configured._client.base_url) == "http://backend.internal:9000"
    assert configured._headers == {"X-Internal-Token": "configured"}
    await configured.close()
    monkeypatch.delenv("TRAJECTORY_BACKEND_INTERNAL_URL")
    default = HttpBackend.from_env()
    assert str(default._client.base_url) == "http://backend:8080"
    await default.close()
