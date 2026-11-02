"""省心托管: the template, reading a recipe off a result, the API, and a make-only run."""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from api.light import router
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.video_job import VideoJob
from db.repository.user_repo import PgUserRepo
from light import autopilot, pipeline
from light.autopilot import LightAutopilotTemplate, Recipe, When


async def _user():
    uid = uuid4().hex
    return await PgUserRepo().create(id=uid, username="ap" + uid[:8], password_hash=None)


def _client(user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user["id"], "workspace_id": user["default_workspace_id"]}
    app.dependency_overrides[get_workspace] = lambda: {"id": user["default_workspace_id"], "role": "owner"}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _light_session(user):
    from session.session import create_session, update_session
    session = await create_session(model="m", agent="build", user_id=user["id"], workspace_id=user["default_workspace_id"])
    return await update_session(session.id, user_id=user["id"], light_entry="video",
                                video_model="MiniMax-H3", video_resolution="768p")


async def _clip_with_overlay(user, session):
    """A generated clip and the name overlay cut from it, the way light-video leaves them."""
    now = datetime.now(timezone.utc)
    ws = user["default_workspace_id"]
    base, final = "asset_" + uuid4().hex[:20], "asset_" + uuid4().hex[:20]
    async with get_db_session() as db:
        for asset_id, name in ((base, "clip.mp4"), (final, "named.mp4")):
            db.add(FileAsset(id=asset_id, user_id=user["id"], workspace_id=ws, session_id=session.id, name=name,
                             oss_key=f"assets/{user['id']}/{name}", mime="video/mp4", size=1, status="ready",
                             created_at=now))
        db.add(VideoJob(id="video_" + uuid4().hex[:20], user_id=user["id"], session_id=session.id, kind="segment",
                        idempotency_key="light_video:a", request_hash="h", status="completed", model="MiniMax-H3",
                        prompt="竖屏美食短片：红烧肉特写，热气升腾。这一次镜头从侧面缓慢推近。",
                        output_asset_id=base, request_data={"duration": 15, "ratio": "9:16",
                                                            "input_asset_ids": ["asset_photo"]},
                        result_data={}, attempt=1, created_at=now, updated_at=now, completed_at=now))
        db.add(VideoJob(id="video_" + uuid4().hex[:20], user_id=user["id"], session_id=session.id, kind="compose",
                        idempotency_key="light_video_compose:a", request_hash="h2", status="completed", model="ims",
                        output_asset_id=final, request_data={"timeline": {
                            "shots": [{"asset": f"https://bossip.oss-cn-shanghai.aliyuncs.com/assets/{user['id']}/clip.mp4"}],
                            "texts": [{"text": "巷口面馆"}, {"text": "红烧牛肉面"}]}},
                        result_data={}, attempt=1, created_at=now, updated_at=now, completed_at=now))
    return base, final


def test_template_rules():
    recipe = Recipe(prompt="红烧肉特写")
    template = LightAutopilotTemplate(task="video", credits_cap_per_run=Decimal("10"), session_id="s", recipe=recipe)
    assert not template.needs_desktop and template.when.cron_expr() == "0 10 * * *"
    assert When(repeat="weekdays", time="08:30").cron_expr() == "30 8 * * 1-5"
    assert When(repeat="weekly", time="18:00").label() == "每周一 18:00"
    assert LightAutopilotTemplate(task="video", mode="make_and_publish", credits_cap_per_run=Decimal("10"),
                                  session_id="s", recipe=recipe).needs_desktop
    assert LightAutopilotTemplate(task="reviews", credits_cap_per_run=Decimal("1"), session_id="s").needs_desktop
    with pytest.raises(ValueError):
        LightAutopilotTemplate(task="video", credits_cap_per_run=Decimal("10"), session_id="s")
    with pytest.raises(ValueError):
        LightAutopilotTemplate(task="image", mode="make_and_publish", credits_cap_per_run=Decimal("1"),
                               session_id="s", recipe=recipe)
    with pytest.raises(ValueError):
        When(time="25:00")
    from cron.validation import validate_template
    validate_template(template.public())
    with pytest.raises(ValueError):
        validate_template({**template.public(), "extra": 1})


async def test_recipe_comes_from_the_clip_under_the_name_overlay():
    user = await _user()
    session = await _light_session(user)
    base, final = await _clip_with_overlay(user, session)
    task, recipe = await autopilot.recipe_for_asset(user["id"], session.id, final)
    assert task == "video"
    assert recipe.prompt == "竖屏美食短片：红烧肉特写，热气升腾。"  # yesterday's variation is dropped
    assert (recipe.shop_name, recipe.slogan, recipe.reference_asset_id, recipe.duration) == (
        "巷口面馆", "红烧牛肉面", "asset_photo", 15)
    task, plain = await autopilot.recipe_for_asset(user["id"], session.id, base)
    assert task == "video" and plain.shop_name == ""
    stranger = await _user()
    with pytest.raises(autopilot.LightError):
        await autopilot.recipe_for_asset(stranger["id"], session.id, final)


async def test_api_creates_lists_pauses_and_cancels_a_free_plan_autopilot(monkeypatch):
    from light import mode as light_mode

    async def free(workspace_id):
        return {"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}

    monkeypatch.setattr(light_mode, "_workspace_facts", free)
    user = await _user()
    session = await _light_session(user)
    _, final = await _clip_with_overlay(user, session)
    async with _client(user) as client:
        resp = await client.post("/api/light/autopilot", json={"session_id": session.id, "asset_id": final})
        assert resp.status_code == 200, resp.text
        made = resp.json()
        assert made["created"] and made["task"] == "video" and made["mode"] == "make_only"
        assert (made["repeat"], made["time"], made["credits_cap_per_run"], made["shop_name"]) == (
            "daily", "10:00", "10", "巷口面馆")
        again = (await client.post("/api/light/autopilot", json={"session_id": session.id, "asset_id": final})).json()
        assert again["id"] == made["id"] and again["created"] is False

        # A free plan has no desktop: publishing is refused, with what would lift it.
        refused = await client.post("/api/light/autopilot", json={"session_id": session.id, "asset_id": final,
                                                                   "mode": "make_and_publish"})
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "LIGHT_AUTOPILOT_NEEDS_DESKTOP"
        assert refused.json()["detail"]["availability"] == "needs_plan"

        listed = (await client.get("/api/light/autopilot")).json()["items"]
        assert [item["id"] for item in listed] == [made["id"]]
        paused = (await client.patch(f"/api/light/autopilot/{made['id']}", json={"enabled": False})).json()
        assert paused["enabled"] is False
        changed = (await client.patch(f"/api/light/autopilot/{made['id']}",
                                      json={"time": "08:00", "credits_cap": "20"})).json()
        assert (changed["time"], changed["credits_cap_per_run"]) == ("08:00", "20")
        assert (await client.delete(f"/api/light/autopilot/{made['id']}")).json() == {"ok": True}
        assert (await client.get("/api/light/autopilot")).json()["items"] == []
        assert (await client.delete(f"/api/light/autopilot/{made['id']}")).status_code == 404


async def test_a_make_only_run_posts_a_new_clip_into_the_conversation(monkeypatch):
    user = await _user()
    session = await _light_session(user)
    _, final = await _clip_with_overlay(user, session)
    made = await autopilot.create(user_id=user["id"], workspace_id=user["default_workspace_id"],
                                  session_id=session.id, asset_id=final)
    from cron.service import cron_service
    job = next(j for j in await cron_service.list_jobs(user["id"], workspace_id=user["default_workspace_id"])
               if j["id"] == made["id"])
    assert autopilot.is_light_autopilot(job) and autopilot.runs_without_desktop(job)

    calls = {}

    async def fake_generate(ctx, *, key, prompt, reference, duration, ratio, on_submitted=None):
        calls["generate"] = (ctx.session_id, ctx.message_id, key, prompt, reference, duration)
        return pipeline.Outcome("video_x", "completed", "asset_new", None)

    async def fake_overlay(ctx, asset_id, *, shop_name, slogan, key, duration, filename):
        calls["overlay"] = (asset_id, shop_name, slogan, key)
        return "asset_named"

    async def balance_ok(workspace_id):
        return True

    monkeypatch.setattr(pipeline, "generate", fake_generate)
    monkeypatch.setattr(pipeline, "overlay", fake_overlay)
    monkeypatch.setattr(autopilot, "_has_balance", balance_ok)
    result = await autopilot.run({**job, "workspace_id": user["default_workspace_id"]})
    assert result["status"] == "ok" and "做好了" in result["summary_text"]
    sid, message_id, key, prompt, reference, duration = calls["generate"]
    assert sid == session.id and message_id and key.startswith(autopilot.KEY_PREFIX)
    assert prompt.startswith("竖屏美食短片：红烧肉特写，热气升腾。这一次") and reference == "asset_photo" and duration == 15
    assert calls["overlay"][:3] == ("asset_new", "巷口面馆", "红烧牛肉面")
    assert calls["overlay"][3].startswith(autopilot.KEY_PREFIX)  # the compose sweep leaves it to this run
    async with get_db_session() as db:
        finished = (await db.scalars(select(Message).where(Message.session_id == session.id,
                                                           Message.role == "assistant"))).all()
    assert any(m.id == message_id for m in finished)

    # Over the cap: nothing is generated and the conversation says why.
    calls.clear()
    tight = {**job, "workspace_id": user["default_workspace_id"],
             "template": {**job["template"], "credits_cap_per_run": "1"}}
    skipped = await autopilot.run(tight)
    assert skipped["status"] == "skipped" and skipped["error"] == "OVER_CAP" and "generate" not in calls
