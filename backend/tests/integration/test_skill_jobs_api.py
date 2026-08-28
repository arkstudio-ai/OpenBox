"""Skill job REST surface with real auth: admission, IDOR, settings gate."""
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("JWT_SECRET", "test-integration-secret-32bytes!!")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


_test_cache = None


@pytest.fixture(scope="module")
async def app():
    global _test_cache
    from cache.memory_cache import MemoryCache

    _test_cache = MemoryCache()

    from core.config import reload_config

    config = reload_config()

    from db.base import Base, close_engine, init_engine

    engine = init_engine("sqlite+aiosqlite:///:memory:")
    import db.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from auth import setup_auth

    setup_auth(config, _test_cache)

    from main import create_app

    application = create_app()
    yield application

    await close_engine()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _register(client, name):
    resp = await client.post(
        "/api/auth/register",
        json={"username": name, "password": "pw123456!", "email": f"{name}@t.dev"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    token = data.get("access_token") or data.get("accessToken")
    assert token
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_job_lifecycle_and_isolation(client):
    alice = await _register(client, "alice_" + uuid.uuid4().hex[:6])
    bob = await _register(client, "bob_" + uuid.uuid4().hex[:6])

    # Admission
    resp = await client.post(
        "/api/skill-jobs",
        headers=alice,
        json={
            "skill": "builtin:demo-echo",
            "operation": "echo",
            "input": {"text": "hi"},
            "idempotency_key": "api-key-1",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is True
    job_id = body["job"]["jobId"]
    assert body["job"]["status"] == "queued"

    # Idempotent replay
    resp = await client.post(
        "/api/skill-jobs",
        headers=alice,
        json={
            "skill": "builtin:demo-echo",
            "operation": "echo",
            "input": {"text": "hi"},
            "idempotency_key": "api-key-1",
        },
    )
    assert resp.json()["created"] is False
    assert resp.json()["job"]["jobId"] == job_id

    # Conflict on same key, different payload
    resp = await client.post(
        "/api/skill-jobs",
        headers=alice,
        json={
            "skill": "builtin:demo-echo",
            "operation": "echo",
            "input": {"text": "DIFFERENT"},
            "idempotency_key": "api-key-1",
        },
    )
    assert resp.status_code == 409

    # Owner reads
    resp = await client.get(f"/api/skill-jobs/{job_id}", headers=alice)
    assert resp.status_code == 200
    resp = await client.get(f"/api/skill-jobs/{job_id}/events", headers=alice)
    assert resp.status_code == 200
    assert [e["seq"] for e in resp.json()["events"]] == [1]
    resp = await client.get("/api/skill-jobs", headers=alice)
    assert any(j["jobId"] == job_id for j in resp.json()["jobs"])

    # IDOR: Bob sees nothing
    for path in (f"/api/skill-jobs/{job_id}", f"/api/skill-jobs/{job_id}/events",
                 f"/api/skill-jobs/{job_id}/artifacts"):
        resp = await client.get(path, headers=bob)
        assert resp.status_code == 404, path
    resp = await client.post(f"/api/skill-jobs/{job_id}/cancel", headers=bob)
    assert resp.status_code == 404
    resp = await client.post(
        f"/api/skill-jobs/{job_id}/inputs", headers=bob, json={"payload": {"x": 1}}
    )
    assert resp.status_code == 404
    resp = await client.get("/api/skill-jobs", headers=bob)
    assert all(j["jobId"] != job_id for j in resp.json()["jobs"])

    # Cancel settles the unclaimed job
    resp = await client.post(f"/api/skill-jobs/{job_id}/cancel", headers=alice)
    assert resp.status_code == 200
    assert resp.json()["job"]["status"] == "cancelled"


@pytest.mark.anyio
async def test_settings_gate_blocks_new_jobs(client):
    user = await _register(client, "carol_" + uuid.uuid4().hex[:6])

    resp = await client.get("/api/skills/settings", headers=user)
    assert resp.status_code == 200
    skills = {s["skillKey"]: s for s in resp.json()["skills"]}
    assert skills["builtin:demo-echo"]["enabled"] is True

    resp = await client.put(
        "/api/skills/builtin:demo-echo/settings", headers=user, json={"enabled": False}
    )
    assert resp.status_code == 200

    resp = await client.post(
        "/api/skill-jobs",
        headers=user,
        json={"skill": "builtin:demo-echo", "operation": "echo", "input": {"text": "x"}},
    )
    assert resp.status_code == 403

    resp = await client.put(
        "/api/skills/builtin:demo-echo/settings", headers=user, json={"enabled": True}
    )
    assert resp.status_code == 200
    resp = await client.post(
        "/api/skill-jobs",
        headers=user,
        json={"skill": "builtin:demo-echo", "operation": "echo", "input": {"text": "x"}},
    )
    assert resp.status_code == 200

    resp = await client.put(
        "/api/skills/builtin:nope/settings", headers=user, json={"enabled": False}
    )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_unauthenticated_rejected(client):
    resp = await client.get("/api/skill-jobs")
    assert resp.status_code in (401, 403)
