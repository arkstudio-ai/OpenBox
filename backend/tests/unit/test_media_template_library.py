"""Cross-entry publication using the real guided form and review API."""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select

from api import admin_examples, light, media_templates
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.file_asset import FileAsset
from db.models.light_example import LightExample
from db.models.media_template import MediaTemplateImport
from db.models.message import Message
from db.models.notification import Notification
from db.models.part import Part
from db.models.session import Session
from db.models.user import User
from db.models.video_job import VideoJob
from db.models.workspace import Workspace
from light import production, taxonomy
from notifications.testing import live_admin
from session.session import create_session
from tool.media_template import execute
from tool.tool import ToolContext


class FixtureObjects:
    def __init__(self):
        self.keys = {}

    def presign_get(self, key, *_):
        return f"https://media.invalid/{key}"

    def _presign(self, _method, key, *_args, **_kw):
        return self.presign_get(key)

    async def copy(self, source, destination):
        if source not in self.keys:
            return None
        self.keys[destination] = self.keys[source]
        return {"size": self.keys[source]}


@pytest.fixture
async def isolated_media_db():
    from db import base

    previous = base._engine, base._session_factory
    engine = base.init_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(base.Base.metadata.create_all)
        yield
    finally:
        taxonomy.forget()
        await base.close_engine()
        base._engine, base._session_factory = previous


@pytest.fixture
async def scenario(monkeypatch, isolated_media_db):
    from core import oss
    from fastapi import HTTPException

    await get_test_config(monkeypatch)
    objects = FixtureObjects()
    monkeypatch.setattr(oss, "get_oss", lambda: objects)
    monkeypatch.setattr(production, "schedule_inbox_wake", lambda *_args, **_kw: None)
    prefix = uuid4().hex[:10]
    author, other, admin = (f"{prefix}_{label}" for label in ("owner", "buyer", "admin"))
    scopes = {key: {"user_id": key, "workspace_id": f"ws_{key}", "ui_language": "zh-CN"}
              for key in (author, other, admin)}
    stamp = datetime.now(timezone.utc)
    async with get_db_session() as db:
        for key in scopes:
            db.add(User(id=key, username=key, role="admin" if key == admin else "user",
                        created_at=stamp, updated_at=stamp))
        await db.flush()
        for key in scopes:
            db.add(Workspace(id=scopes[key]["workspace_id"], name="Test", owner_user_id=key,
                             created_at=stamp, updated_at=stamp))
        await db.flush()
        for key in scopes:
            user = await db.get(User, key)
            user.default_workspace_id = scopes[key]["workspace_id"]

    def authenticate(request: Request):
        identity = scopes.get(request.headers.get("x-test-user"))
        if not identity:
            raise HTTPException(401)
        return identity

    def admin_only(request: Request):
        identity = authenticate(request)
        if identity["user_id"] != admin:
            raise HTTPException(403)
        return identity

    app = FastAPI()
    app.include_router(media_templates.router)
    app.include_router(light.router)
    app.include_router(admin_examples.router)
    app.dependency_overrides.update({get_current_user: authenticate, get_workspace: authenticate, live_admin: admin_only})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://openbox.test") as client:
        client.headers["X-Test-User"] = author
        yield SimpleNamespace(client=client, scopes=scopes, author=author, other=other, admin=admin, objects=objects)


async def get_test_config(monkeypatch):
    from core.config import OpenBoxConfig
    from light import mode

    async def workspace_facts(*_):
        return {}
    monkeypatch.setattr(mode, "_workspace_facts", workspace_facts)
    monkeypatch.setattr(mode, "availability", lambda *_: "ok")
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig())
    taxonomy.forget()


async def asset(scenario, session, mime, *, source="agent"):
    key = "asset_" + uuid4().hex
    stamp = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(FileAsset(id=key, user_id=session.user_id, workspace_id=session.workspace_id, session_id=session.id,
                         name="result.mp4" if mime.startswith("video") else "result.png", oss_key=key,
                         mime=mime, status="ready", source=source, size=40, created_at=stamp))
    scenario.objects.keys[key] = 40
    return key


async def deliver(scenario, session, asset_id, mime, *, guided):
    stamp = datetime.now(timezone.utc)
    async with get_db_session() as db:
        row = await db.get(Session, session.id)
        run = row.light_production["run"] if guided else {}
        receipt = await db.get(AgentInboxItem, run["id"]) if guided else None
        user_msg = Message(id="message_" + uuid4().hex, session_id=row.id, user_id=row.user_id,
                           role="user", created_at=stamp)
        answer = Message(id="message_" + uuid4().hex, session_id=row.id, user_id=row.user_id,
                         role="assistant", parent_id=user_msg.id, created_at=stamp)
        db.add_all([user_msg, answer])
        await db.flush()
        db.add(Part(id="part_" + uuid4().hex, session_id=row.id, user_id=row.user_id, message_id=answer.id, type="file",
                    data={"asset_id": asset_id, "mime_type": mime, "relation": {"role": "final"}}, created_at=stamp))
        if not guided:
            db.add(Part(id="part_" + uuid4().hex, session_id=row.id, user_id=row.user_id, message_id=user_msg.id,
                        type="text", data={"origin": "human"}, created_at=stamp))
        if receipt:
            receipt.message_id, receipt.result_message_id, receipt.state = user_msg.id, answer.id, "settled"
            receipt.run_id, receipt.generation = "run_" + uuid4().hex, 1
            receipt.turn_id, receipt.step_id = "turn_" + uuid4().hex, "step"
            receipt.claim_token, receipt.claim_owner = "test-claim", "test-worker"
            receipt.claimed_at, receipt.settled_at = stamp, stamp
            row.status = "idle"
        if guided and mime.startswith("video/"):
            db.add(VideoJob(id="job_" + uuid4().hex, session_id=row.id, user_id=row.user_id, kind="compose",
                            status="completed", idempotency_key=run["job_prefix"] + ":final", output_asset_id=asset_id,
                            created_at=stamp, updated_at=stamp, completed_at=stamp))


async def test_retired_soda_template_is_hidden_but_saved_production_can_continue(scenario):
    client = scenario.client
    retired_id = "mtb_soda_story"
    other_id = "mtb_product_story"
    for url, headers in (
        ("/api/media-templates?entry_id=video&scene=story", None),
        ("/api/media-templates/admin?entry_id=video&scene=story", {"X-Test-User": scenario.admin}),
    ):
        response = await client.get(url, headers=headers)
        assert response.status_code == 200
        ids = {card["id"] for card in response.json()["items"]}
        assert retired_id not in ids and other_id in ids
    for kind in ("cover", "video"):
        assert (await client.get(f"/api/media-templates/sample/soda_story/{kind}")).status_code == 404

    form = (await client.post("/api/light/productions", json={"entry_id": "video"})).json()
    url = f"/api/light/productions/{form['session_id']}"
    choice = {"revision": form["revision"], "reference_example_id": retired_id}
    rejected = await client.patch(url, json=choice)
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "LIGHT_PRODUCTION_TEMPLATE_RETIRED"
    assert (await client.patch(url, json={"revision": form["revision"], "template": "soda_story"})).status_code == 409

    async with get_db_session() as db:
        session = await db.get(Session, form["session_id"])
        session.light_example_id = retired_id
        session.light_production = {**session.light_production, "template": "soda_story"}
    saved = await client.get(url)
    assert saved.status_code == 200
    assert saved.json()["reference_example_title"] == "苏打水广告"
    edited = await client.patch(url, json={**choice, "shop": "Continue the saved draft"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["reference_example_id"] == retired_id


@pytest.mark.parametrize(("entry", "scene", "mime"), (
    ("video", "story", "video/mp4"), ("footage_edit", "daily", "video/mp4"),
    ("menu", "menu_red", "image/png"), ("poster", "opening", "image/png"),
))
async def test_create_trial_review_and_reuse(scenario, entry, scene, mime):
    client = scenario.client
    scope = scenario.scopes[scenario.author]
    pro = await create_session(user_id=scenario.author, workspace_id=scope["workspace_id"], title="Professional")
    original = await asset(scenario, pro, mime)
    await deliver(scenario, pro, original, mime, guided=False)
    draft = media_templates.library.Draft(entry_id=entry, asset_id=original, title="Reusable structure",
                                         recipe="Keep the composition and visual hierarchy; adapt to current assets.")
    result = await execute(draft, ToolContext(user_id=pro.user_id, workspace_id=pro.workspace_id, session_id=pro.id))
    edition_id = result.metadata["edition_id"]
    edition = f"/api/media-templates/{edition_id}"
    catalog = f"/api/media-templates?entry_id={entry}&scene={scene}"
    admin = {"X-Test-User": scenario.admin}
    other = {"X-Test-User": scenario.other}
    assert edition_id not in (await client.get(catalog, headers=other)).text
    assert (await client.get("/api/media-templates/admin")).status_code == 403
    assert (await client.post(edition + "/trial", headers=other)).status_code == 404
    response = await client.post(edition + "/trial")
    assert response.status_code == 201, response.text
    form = response.json()
    session_id = form["session_id"]
    url = f"/api/light/productions/{session_id}"
    assert form["reference_example_id"] == edition_id
    assert "recipe" not in form
    response = await client.patch(url, json={"revision": form["revision"], "reference_example_id": ""})
    assert response.status_code == 409
    confirmation = {"session_id": session_id, "output_asset_id": original}
    assert (await client.post(edition + "/confirm", json=confirmation)).status_code == 409
    trial = await production.get(pro.user_id, pro.workspace_id, session_id)
    async with get_db_session() as db:
        row = await db.get(Session, session_id)
    material = await asset(scenario, row, "image/png" if entry != "footage_edit" else "video/mp4", source="user")
    fields = {"revision": trial["revision"], "shop": "New shop", "asset_ids": [material]}
    fields.update({"video": {"script": "Sell a different product"}, "footage_edit": {},
                   "menu": {"dishes": [{"name": "Fresh noodles", "price": "5"}]},
                   "poster": {"poster_title": "New poster headline"}}[entry])
    response = await client.patch(url, json=fields)
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]
    response = await client.post(url + "/actions", json={"revision": revision, "action": "render", "request_id": uuid4().hex})
    assert response.status_code == 200, response.text
    async with get_db_session() as db:
        row = await db.get(Session, session_id)
    assert draft.recipe in production.forms.turn_instruction(row, "render")
    output = await asset(scenario, row, mime)
    await deliver(scenario, row, output, mime, guided=True)
    confirmation["output_asset_id"] = output
    submission = {"session_id": session_id, "scene": scene, "share_finished_preview": True}
    assert (await client.post(edition + "/submit", json=submission)).status_code == 409
    confirmed = await client.post(edition + "/confirm", json=confirmation)
    assert confirmed.status_code == 200, confirmed.text
    wrong = await client.post(edition + "/submit", json={**submission, "scene": "does_not_exist"})
    assert wrong.status_code == 422, wrong.text
    pending = await client.post(edition + "/submit", json=submission)
    assert pending.status_code == 200 and pending.json()["status"] == "pending", pending.text
    assert (await client.post(edition + "/submit", json=submission)).json()["status"] == "pending"
    assert edition_id not in (await client.get(catalog, headers=other)).text
    queue = await client.get("/api/media-templates/admin?status=pending", headers=admin)
    assert queue.status_code == 200 and edition_id in queue.text, queue.text
    review = f"/api/media-templates/admin/{edition_id}"
    evidence = await client.get(review, headers=admin)
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["input"]["shop"] == "New shop"
    assert material in evidence.json()["materials"][0]["media_url"]
    assert output in evidence.json()["trial_result"]["media_url"]
    assert "/preview." in evidence.json()["original"]["media_url"]
    assert (await client.post(review + "/decision", json={"decision": "rejected", "reason": ""}, headers=admin)).status_code == 422
    approved = await client.post(review + "/decision", json={"decision": "listed"}, headers=admin)
    assert approved.status_code == 200, approved.text
    shelf = await client.get(catalog, headers=other)
    published = next(item for item in shelf.json()["items"] if item["id"] == edition_id)
    assert "recipe" not in published and "owner_id" not in published
    assert output not in shelf.text
    buyer = await client.post("/api/light/productions", json={"entry_id": entry}, headers=other)
    assert buyer.status_code == 201, buyer.text
    form = buyer.json()
    selected = await client.patch(f"/api/light/productions/{form['session_id']}", headers=other,
                                  json={"revision": form["revision"], "reference_example_id": edition_id})
    assert selected.status_code == 200 and selected.json()["reference_example_id"] == edition_id, selected.text
    revision = await execute(draft.model_copy(update={"recipe": "Different reusable structure for the new revision.",
                                                       "revises_edition_id": edition_id}),
                             ToolContext(user_id=pro.user_id, workspace_id=pro.workspace_id, session_id=pro.id))
    new_id = revision.metadata["edition_id"]
    assert new_id != edition_id
    assert (await client.post(f"/api/media-templates/{new_id}/confirm", json=confirmation)).status_code == 404
    assert edition_id in (await client.get(catalog, headers=other)).text
    async with get_db_session() as db:
        kinds = (await db.scalars(select(Notification.kind).where(
            Notification.source_key.endswith(edition_id),
        ))).all()
        assert set(kinds) == {"media_template_pending", "media_template_review"}


async def test_historical_examples_remain_classified_and_selectable(scenario):
    example_id = "legacy_" + uuid4().hex
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(LightExample(id=example_id, title="Historical poster", entry_id="poster", industry="food",
                            scene="opening", status="active", outputs=[{"oss_key": "legacy/poster.png", "mime": "image/png"}],
                            created_at=now, updated_at=now))
        await db.flush()
        db.add(MediaTemplateImport(id=example_id, example_id=example_id))
    client = scenario.client
    response = await client.get("/api/media-templates?entry_id=poster&scene=opening")
    assert response.status_code == 200
    assert example_id in response.text
    response = await client.get("/api/media-templates?entry_id=menu")
    assert example_id not in response.text


async def test_admin_can_create_children_for_all_entries(scenario):
    client = scenario.client
    admin = {"X-Test-User": scenario.admin}
    original = (await client.get("/api/admin/examples/taxonomy", headers=admin)).json()
    assert "entries" in original
    scenes = {
        entry["id"]: [*entry["scenes"], {"label": f"Custom {index}"}]
        for index, entry in enumerate(original["entries"], start=1)
    }
    body = {"scenes": scenes, "industries": original["industries"]}
    assert (await client.put("/api/admin/examples/taxonomy", json=body)).status_code == 403
    saved = await client.put("/api/admin/examples/taxonomy", json=body, headers=admin)
    assert saved.status_code == 200, saved.text
    public = (await client.get("/api/media-templates/categories")).json()
    additions = [{scene["id"] for scene in entry["scenes"] if scene["label"].startswith("Custom ")}
                 for entry in public["entries"]]
    assert len(additions) == 4 and all(len(ids) == 1 for ids in additions)
    reset = {"scenes": {entry["id"]: entry["scenes"] for entry in original["entries"]},
             "industries": original["industries"]}
    assert (await client.put("/api/admin/examples/taxonomy", json=reset, headers=admin)).status_code == 200
