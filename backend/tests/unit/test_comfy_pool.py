import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from core.config import OpenBoxConfig, _apply_env_overrides
from light import h3_recipes, production_form as forms, remake_case, video_tiers
from tool import comfy_pool, video_comfyui as h3, video_providers


@pytest.fixture
def pool(tmp_path, monkeypatch):
    workers = []
    for name in ("old", "new"):
        token = tmp_path / name
        token.write_text(name + "-test")
        workers.append({"id": name, "url": f"https://{name}.test/proxy/18189", "token_file": str(token)})
    path = tmp_path / "pool.json"
    path.write_text(json.dumps(workers))
    for key in ("COMFYUI_API_URL", "COMFYUI_API_TOKEN", "COMFYUI_API_TOKEN_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COMFYUI_WORKERS_FILE", str(path))
    monkeypatch.setenv("COMFYUI_VIDEO_FAST_TIER", "true")
    cfg = OpenBoxConfig.model_validate(_apply_env_overrides({}))
    monkeypatch.setattr(comfy_pool, "get_config", lambda: cfg)
    return video_providers.resolve_route(h3.MODEL, cfg), cfg


@pytest.mark.asyncio
@pytest.mark.parametrize("old_queue,uploading", [({"external"}, False), (None, False), (set(), True)])
async def test_dispatch_counts_external_jobs_and_unacknowledged_uploads(pool, monkeypatch, old_queue, uploading):
    route, _ = pool
    old, new = route.comfy_workers
    job = SimpleNamespace(request_data={}, model=h3.MODEL)
    previous = SimpleNamespace(request_data={"comfy_worker": old, "comfy_prompt_id": "reserved"},
                               status="submitting", updated_at=datetime.now(timezone.utc))

    class DB:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, query):
            assert "pg_advisory_xact_lock" in str(query)

        async def get(self, *args, **kwargs):
            assert kwargs["with_for_update"]
            return job

        async def scalars(self, query):
            return SimpleNamespace(all=lambda: [previous] if uploading else [])

    @asynccontextmanager
    async def session():
        yield DB()

    async def queue(w):
        return old_queue if w.provider == old else set()

    monkeypatch.setattr(comfy_pool, "get_db_session", session)
    monkeypatch.setattr(comfy_pool, "_queue", queue)
    worker, pid = await comfy_pool.reserve(route, "test")
    assert worker.provider == new and worker.workbench
    assert job.request_data["provider_route_fingerprint"] == video_providers.provider_route_fingerprint(worker)
    assert await comfy_pool.reserve(route, "test") == (worker, pid)
    assert comfy_pool.owner(route, {}) == route


@pytest.mark.parametrize("key", sorted(h3_recipes.TEMPLATES))
def test_builtin_fast_template_exposes_normal_agent_tools_and_own_creative_direction(pool, key):
    _, config = pool
    form = {**forms.initial_form("video", "当前品牌", "当前商品"), "builtin_h3": True,
            "template": key, "template_recipe": h3_recipes.recipe(key), "run": {"job_prefix": "light:test:2"}}
    session = SimpleNamespace(light_entry="video", light_production=form)
    assert "fast" in video_tiers.available_tiers(config, template=key)
    assert not remake_case.applies("video", form)
    all_tools = {k: SimpleNamespace(id=k) for k in (
        "light_wan_story", "light_gallery", "video_generate", "video_compose", "image_gen", "light_production_form")}
    assert set(forms.restrict_guided_tools(all_tools, session, "render")) == {"video_generate", "video_compose", "image_gen"}
    assert "light_production_form" in forms.restrict_guided_tools(all_tools, session, "prepare")
    for action in ("prepare", "render"):
        text = forms.turn_instruction(session, action)
        assert form["template_recipe"]["title"] in text and "当前品牌" in text
        assert "gallery-stage" not in text
    references = h3_recipes.reference_files(key)
    assert all(path.is_file() for path in references)
    assert ("prepare_template" in text) == bool(references)
    assert "IMS" in text and "await_ready" in text
    if key == "product_story":
        assert sum(b["frames"] for b in form["template_recipe"]["cuts"]) == 1277
    if key == "silk_road":
        assert form["template_recipe"]["canvas"]["width"] == 1680


@pytest.mark.asyncio
async def test_lost_submit_ack_preserves_pinned_prompt_id_and_rechecks_owner_queue(pool, monkeypatch):
    route, _ = pool
    worker = comfy_pool.worker_route(route, route.comfy_workers[0])
    pid = "e7cfd5ac-9c4d-443f-8c14-602ba67101f0"
    recorded = []
    async def record(value):
        recorded.append(value)
    def transport(req):
        if req.url.path.endswith("/prompt"):
            assert recorded == [pid]
            raise httpx.ReadTimeout("response lost", request=req)
        if req.url.path.endswith("/queue"):
            return httpx.Response(200, json={"queue_running": [[1, pid]], "queue_pending": []})
        return httpx.Response(200, json={})
    monkeypatch.setattr(h3, "client", lambda r, t: httpx.AsyncClient(base_url=r.base_url, transport=httpx.MockTransport(transport)))
    req = {"comfy_prompt_id": pid, "refs": [], "prompt": "A quiet room", "seed": 5, "resolution": "720p",
           "ratio": "16:9", "duration": 5}
    assert (await h3.submit(worker, req, before_post=record))["id"] == pid
    assert await h3.status(worker, pid) == {"id": pid, "status": "in_progress"}


def test_workbench_mixed_references_feed_original_sampler_and_both_frame_guides():
    task_id = "48dca02f-9bc6-490e-a4cb-2a5af44b6809"
    body = {"prompt": "A turning head", "duration": 5, "resolution": "720p", "ratio": "9:16", "seed": 42}
    refs = [("first_frame", "open.jpg", 0), ("reference_video", "turn.mp4", 5), ("last_frame", "end.jpg", 0)]
    graph = h3.pool_workflow(body, refs, task_id)
    assert graph["13"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert graph["13"]["inputs"]["ref_videos.ref_video_0"] == ["31", 0]
    assert [graph[n]["inputs"]["frame_idx"] for n in ("70", "71")] == [0, -1]
    assert graph["71"]["inputs"]["positive"] == ["70", 0]
    assert graph["17"]["inputs"]["positive"] == graph["16"]["inputs"]["conditioning"] == ["71", 0]
    assert graph["17"]["inputs"]["seed"] == 42 and graph["15"]["inputs"]["steps"] == 10


@pytest.mark.asyncio
async def test_pool_status_allows_publication_time_then_flags_missing_task(monkeypatch):
    from datetime import timedelta
    from tool import video_production

    async def missing(*args):
        return {"status": "in_progress", "comfy_unseen": True}

    monkeypatch.setattr(video_production, "_provider_status", missing)
    job = SimpleNamespace(provider_task_id="task", started_at=datetime.now(timezone.utc))
    assert (await video_production._job_provider_status(None, job))["status"] == "in_progress"
    job.started_at -= timedelta(minutes=3)
    assert (await video_production._job_provider_status(None, job))["status"] == "failed"


def test_guided_window_returns_attached_audio_correction_from_the_same_cut():
    from datetime import timedelta
    from light.production import _output_candidates

    now = datetime.now(timezone.utc)
    s = SimpleNamespace(light_entry="video", created_at=now,
                        light_production={"template": "soda_story", "builtin_h3": True,
                                          "run": {"action": "render", "job_prefix": "light:x:2"}})
    def job(kind, key, asset, at, run="first", offset=0):
        return SimpleNamespace(kind=kind, status="completed", idempotency_key="light:x:2:"+key,
                               output_asset_id=asset, completed_at=now+timedelta(seconds=at),
                               request_data={"submit_run_id": run, "timeline": {
                                   "canvas": {"width": 720, "height": 1280},
                                   "shots": [{"asset": "asset_shot", "in_sec": offset, "duration_sec": 3}]}})
    jobs = [job("segment", "shot1", "asset_shot", 1), job("compose", "final", "asset_original", 2),
            job("compose", "audio-repair", "asset_fixed", 3), job("compose", "other", "asset_other", 4, run="other"),
            job("compose", "test-cut", "asset_preview", 5, offset=1)]
    cards = [SimpleNamespace(message_id="guided", created_at=j.completed_at,
                             data={"asset_id": j.output_asset_id, "relation": {"role": "final"}}) for j in jobs[1:]]
    assert _output_candidates(s, jobs, cards) == ["asset_fixed", "asset_original"]
