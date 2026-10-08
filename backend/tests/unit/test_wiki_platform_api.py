"""HTTP admission, attachment bytes, malformed declarations and tenant isolation."""
from copy import deepcopy
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from memory.wiki import exchange_api, organization_api, workflow_api
from tests.unit.test_memory_wiki import seed, wiki_database  # noqa: F401
from tests.unit.test_wiki_exchange import bundle
from wiki_compiler.exchange import parse_okf
from wiki_compiler.profile_templates import KNOWLEDGE_REVIEW


@pytest.mark.asyncio
async def test_platform_http_boundaries_and_attachment_content(monkeypatch):
    data = await seed(monkeypatch)
    app = FastAPI()
    for router in (exchange_api.router, organization_api.router, workflow_api.router):
        app.include_router(router)
    actor = {"user_id": data[0], "workspace_id": data[1]}
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_workspace] = lambda: actor["workspace_id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://wiki.test") as client:
        base = "/api/memory-wiki"
        preview = await client.get(base + "/organization/preview", params={"project_id": data[2]})
        assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
        denied = await client.post(base + "/organization/runs", json={"project_id": data[2],
            "input_hash": preview.json()["input_hash"], "request_id": uuid4().hex, "confirm_cost": False})
        assert denied.status_code == 400
        bad = await client.post(base + "/exchange/preview", files={"file": ("invalid.zip", b"invalid")})
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "WIKI_EXCHANGE_INVALID_ZIP"
        imported = await client.post(base + "/exchange/preview", data={"project_id": data[2]},
            files={"file": ("independent.okf.zip", bundle(), "application/zip")})
        assert imported.status_code == 200 and imported.headers["cache-control"] == "no-store"
        document = imported.json()["documents"][0]
        accepted = await client.post(base + "/exchange/documents/" + document["id"] + "/decision", json={
            "expected_revision": document["revision"], "content_hash": document["content_hash"], "action": "approve"})
        assert accepted.status_code == 200
        exported = await client.get(base + "/exchange/export", params={"format": "okf", "project_id": data[2]})
        assert exported.status_code == 200 and exported.headers["cache-control"] == "no-store"
        assert "attachment" in exported.headers["content-disposition"]
        assert parse_okf(exported.content)["documents"][0]["title"] == "Release policy"
        for path, replacement in [(('entities', 'topic', 'lifecycle'), None),
                                  (('workflows', 'organize-review', 'title'), {}),
                                  (('entities', 'topic', 'fields', 'summary', 'type'), [])]:
            definition = deepcopy(KNOWLEDGE_REVIEW)
            target = definition
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = replacement
            result = await client.post(base + "/profiles", json={"project_id": data[2],
                "expected_revision": 0, "definition": definition})
            assert result.status_code == 422
        saved = await client.post(base + "/profiles", json={"project_id": data[2],
            "expected_revision": 0, "definition": KNOWLEDGE_REVIEW})
        assert saved.status_code == 200
        profile = saved.json()
        started = await client.post(base + "/workflows", json={"profile_id": profile["id"],
            "expected_profile_revision": profile["revision"], "workflow_id": "organize-review",
            "inputs": {"goal": "Review the imported team policy"}, "request_id": uuid4().hex})
        run = started.json()
        history = await client.get(base + "/workflows/" + run["id"] + "/history")
        assert history.status_code == 200 and history.json()["events"][0]["action"] == "start"
        other = await seed(monkeypatch)
        actor.update(user_id=other[0], workspace_id=other[1])
        for suffix in ("/exchange/bundles/" + imported.json()["id"], "/workflows/" + run["id"],
                       "/workflows/" + run["id"] + "/history", "/profiles/" + profile["id"]):
            assert (await client.get(base + suffix)).status_code == 404
        assert (await client.get(base + "/concepts", params={"project_id": data[2]})).status_code == 404
