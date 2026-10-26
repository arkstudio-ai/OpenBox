"""The high-frequency sidebar response uses typed serialization and stays private."""
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api import sessions as routes
from session.session import Session


async def test_sidebar_list_uses_typed_serialization_without_private_fields(monkeypatch):
    rows = [Session(id="one", user_id="actor", workspace_id="workspace", title="中文会话",
        memory_policy="assistant_isolated", tool_exposure_state={"private": "must-not-leak"})]
    async def listed(**kwargs):
        assert kwargs == {"project_id": None, "user_id": "actor", "workspace_id": "workspace"}
        return rows
    monkeypatch.setattr(routes.session_mod, "list_sessions", listed)
    def no_recursive_encoder(*args, **kwargs):
        raise AssertionError("Sidebar lists must not walk every field through jsonable_encoder")
    monkeypatch.setattr("fastapi.encoders.jsonable_encoder", no_recursive_encoder)
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/agent")
    app.dependency_overrides[routes.get_current_user] = lambda: {"user_id": "actor", "workspace_id": "workspace"}
    app.dependency_overrides[routes.get_workspace] = lambda: "workspace"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reply = await client.get("/api/agent/session")
    assert reply.status_code == 200
    assert reply.json() == [row.model_dump(mode="json") for row in rows]
    assert "must-not-leak" not in reply.text and "memory_policy" not in reply.text
