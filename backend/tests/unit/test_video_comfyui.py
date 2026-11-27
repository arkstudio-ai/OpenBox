import json
from types import SimpleNamespace

import httpx
import pytest

from api.metadata import _model_tiers, _video_models
from core.config import OpenBoxConfig, _apply_env_overrides
from light import production_form, video_tiers
from tool import video_comfyui as h3, video_providers, video_template
from tool.video_production import resolve_spoken_line
from tool.video_providers import VideoRequestError


@pytest.mark.asyncio
async def test_fast_tier_uses_comfyui_and_paused_runninghub_keeps_existing_route(monkeypatch):
    monkeypatch.setenv("COMFYUI_API_URL", "https://example.test/proxy/18189")
    monkeypatch.setenv("COMFYUI_API_TOKEN", "test-comfy-token")
    monkeypatch.setenv("COMFYUI_VIDEO_FAST_TIER", "true")
    monkeypatch.setenv("RUNNINGHUB_VIDEO_ENABLED", "false")
    old_model = "MiniMax-H3-Max-Turbo"
    data = {
        "provider": {"runninghub": {"base_url": "https://runninghub.example", "api_key": "test-rh-token"}},
        "video_generation": {
            "model": old_model,
            "allowed_models": [old_model, "wan3.0-video"],
            "models": [
                {"id": old_model, "channel": "runninghub", "provider": "runninghub"},
                {"id": "wan3.0-video", "channel": "ark", "resolutions": ["720p"]},
            ],
        },
        "model_tiers": {"video": [
            {"tier": "high", "model": "wan3.0-video"},
            {"tier": "fast", "model": old_model},
        ]},
    }
    original = OpenBoxConfig.model_validate(data)
    historical_route = video_providers.resolve_route(old_model, original)
    config = OpenBoxConfig.model_validate(_apply_env_overrides(data))
    models = _video_models(config)
    assert {model["id"] for model in models} == {"ComfyUI-MiniMax-H3", "wan3.0-video"}
    assert video_providers.effective_reference_support(video_providers.declared_model(h3.MODEL, config))["audio"]
    tiers = {tier["tier"]: tier for tier in _model_tiers(config)["video"]}
    assert tiers["fast"]["model"] == config.video_generation.model == "ComfyUI-MiniMax-H3"
    assert tiers["fast"]["resolution"] == "720p"
    assert tiers["high"]["model"] == "wan3.0-video"
    assert video_tiers.comfyui_fast_available(config)
    assert "fast" in video_tiers.available_tiers(config)
    assert "fast" not in video_tiers.available_tiers(config, template="silk_road")
    assert "fast" not in video_tiers.available_tiers(config, template="soda_story")
    monkeypatch.setattr(video_tiers, "get_config", lambda: config)
    assert video_tiers.resolve_tier("fast") == ("ComfyUI-MiniMax-H3", "720p")
    paused_route = video_providers.resolve_route(old_model, config)
    assert video_providers.provider_route_fingerprint(paused_route) == video_providers.provider_route_fingerprint(historical_route)
    with pytest.raises(VideoRequestError, match="RunningHub 视频暂时停用"):
        await video_providers.submit(paused_route, "/not-sent", {})


@pytest.mark.asyncio
async def test_gallery_storyboard_uses_current_non_food_campaign(monkeypatch):
    from core import oss

    registered = []

    async def register(name, ctx, project_id, *, oss):
        registered.append(name)
        return name.removesuffix(".jpg")

    monkeypatch.setattr(video_template, "_asset", register)
    monkeypatch.setattr(oss, "get_oss", lambda: object())
    template = video_template.GALLERY_WISHES
    ctx = SimpleNamespace(user_id="user", workspace_id="workspace", session_id="session", project_id=None)
    storyboard = await video_template.prepare(template, ctx)
    assert set(registered) == {"gallery-open.jpg", "gallery-genie.jpg", "opening.mp4", "first-wish.mp4",
                               "second-wish.mp4", "third-wish.mp4"}
    assert sum(shot["duration"] for shot in storyboard["shots"]) == 38
    assert all(any(asset["role"] == "reference_video" for asset in shot["input_assets"])
               for shot in storyboard["shots"][:4])
    assert storyboard["shots"][3]["product_frame"] == "reference_image"
    assert all("向日葵" not in shot["label"] + shot["action"] for shot in storyboard["shots"])
    assert storyboard["shots"][4]["product_frame"] == "first_frame"
    assert not storyboard["shots"][4]["input_assets"]

    facts = {"shop": "北岸装备", "product_name": "保温杯", "asset_ids": ["owner-thermos-image"],
             "promotion_brief": {"offer": "89 元/只", "call_to_action": "暖意随行"},
             "reference_copy": "灯神说「喝口热的再出发」。", "third_wish_line": "一路都喝热的。"}
    form = {**production_form.initial_form("video", "", ""), **facts, "template": template,
            "run": {"job_prefix": "light:qa:1", "video_language": "zh"}}
    session = SimpleNamespace(light_entry="video", light_production=form)
    prompt = production_form.turn_instruction(session, "render")
    for text in ("北岸装备", "保温杯", "owner-thermos-image", "89 元/只", "暖意随行", "喝口热的再出发", "一路都喝热的"):
        assert text in prompt
    assert "小龙虾" not in prompt
    assert "45元" not in prompt
    assert "third_wish_line" not in prompt
    assert "subject_definitions:" in prompt and "retention_analysis:" in prompt
    assert "阿里云 IMS" in prompt and "light_gallery" in prompt
    tool_ids = ("video_generate", "video_compose", "share_file", "bash", "skill",
                "video_analyze", "footage_analyze", "unrelated", "light_gallery")
    tools = {name: SimpleNamespace(id=name) for name in tool_ids}
    assert production_form.restrict_guided_tools(tools, session, "render").keys() == {"light_gallery"}
    assert "light_gallery" not in production_form.restrict_guided_tools(tools, session, None)


def test_gallery_resolves_promotional_artwork_from_current_form_and_cuts_it_in_ims():
    from light import remake_case
    from light.mode import LightError
    from tool import light_gallery as gallery

    frames = gallery.Shot(duration=8, reference="gallery_first_and_last", prompt="Animate the lamp from the pond.")
    motion = gallery.Shot(duration=7, reference="motion", prompt="Follow the motion of the original camera.")
    photo = gallery.Shot(duration=7, reference="product_first_frame", prompt="Show the finish of the owner's product.")
    wish = gallery.Shot(duration=10, reference="gallery_and_product", still="genie",
                         prompt="(S1) says <d>[Chinese] 一路都喝热的。</d> and lifts the flask.")
    poster = gallery.Shot(duration=7, reference="brand_poster", poster_direction="Warm product close-up.",
                           prompt="Keep the printed ad steady as the background glows.")
    plan = gallery.Plan(shots=[frames, motion, photo, wish, poster])
    form = {"template": video_template.GALLERY_WISHES, "shop": "北岸装备", "product_name": "保温杯",
            "reference_copy": "第三个愿望的指定台词：「一路都喝热的。」", "promotion_brief": {"offer": "89元/只"}}
    refs = gallery.source_plan(plan, form)
    assert refs[0] == [("first_frame", "gallery-open.jpg"), ("last_frame", "gallery-genie.jpg")]
    assert refs[1] == [("reference_video", "first-wish.mp4")]
    assert refs[3] == [("reference_image", "gallery-genie.jpg"), ("reference_image", gallery.PRODUCT)]
    assert refs[4] == [("first_frame", gallery.PRODUCT)]
    with pytest.raises(LightError, match="指定台词"):
        gallery.source_plan(plan, {**form, "reference_copy": "第三个愿望的指定台词：「别的台词。」"})
    artwork = gallery.poster_prompt(poster, form)
    assert '["北岸装备", "保温杯", "89元/只"]' in artwork
    assert "Warm product close-up." in artwork
    assert "小龙虾" not in artwork and "45元" not in artwork
    offer = poster.model_copy(update={"reference": "offer_poster"})
    assert '["保温杯", "89元/只"]' in gallery.poster_prompt(offer, form)
    assert '["北岸装备", "保温杯"]' in gallery.poster_prompt(poster, {**form, "promotion_brief": {}})
    timeline = gallery.timeline_for(plan, ["a", "b", "c", "d", "e"], "720p")
    assert "texts" not in timeline and "overlays" not in timeline
    assert [shot["asset"] for shot in timeline["shots"]] == ["a", "b", "c", "d", "e"]
    assert all(shot["volume"] == 1 for shot in timeline["shots"])
    assert remake_case.owns_job(SimpleNamespace(
        idempotency_key=f"light:session:1:{video_template.GALLERY_WISHES_VERSION}:shot1"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_shot", [None, 4])
async def test_gallery_streams_child_job_progress_and_keeps_it_in_the_result(monkeypatch, failed_shot):
    from core import oss
    from light import production, remake
    from tool import light_gallery as gallery, video_compose, video_production
    from tool.tool import ToolContext, ToolResult

    plan = gallery.Plan(shots=[
        gallery.Shot(duration=7, reference=reference, prompt="Follow the action and framing of the reference.")
        for reference in ("motion", "motion", "motion", "motion_with_product", "brand_poster")
    ])
    form = {"shop": "北岸装备", "product_name": "保温杯", "asset_ids": ["product-photo"]}
    run = {"video_language": "zh", "job_prefix": "gallery:trace-test", "video_resolution": "720p", "id": "trace"}
    reports = []

    async def record(output):
        reports.append(output)

    async def begin(_ctx, _proposed):
        return form, run, plan

    async def noop(*_args, **_kwargs):
        return None

    async def asset(name, *_args, **_kwargs):
        return name

    async def poster(_ctx, _prefix, _index, _shot, saved_form):
        assert saved_form["asset_ids"] == ["product-photo"]
        return "approved-artwork"

    async def execute(request, ctx):
        await ctx.update_output("raw provider update")
        if request.action == "validate":
            return ToolResult(output="valid=true")
        if request.action == "submit":
            return ToolResult(metadata={"job_id": request.idempotency_key})
        assert request.action == "wait"
        if failed_shot is not None and request.job_id.endswith(f":shot{failed_shot}"):
            return ToolResult(metadata={"status": "failed"})
        return ToolResult(metadata={"status": "completed", "asset_id": request.job_id})

    monkeypatch.setattr(production, "begin_gallery", begin)
    monkeypatch.setattr(production, "remake_stage", noop)
    monkeypatch.setattr(remake, "_existing_job", noop)
    monkeypatch.setattr(remake, "_check", noop)
    monkeypatch.setattr(oss, "get_oss", lambda: object())
    monkeypatch.setattr(video_template, "_asset", asset)
    monkeypatch.setattr(gallery, "prepare_poster", poster)
    monkeypatch.setattr(video_production, "execute_generate", execute)
    monkeypatch.setattr(video_compose, "execute_compose", execute)

    result = await gallery.execute(plan, ToolContext(_on_output=record))
    assert any("ComfyUI · 第 1 段 · R2V 7s · 校验参数" in update for update in reports)
    assert any("ComfyUI · 第 1 段 · R2V 7s · 提交中" in update for update in reports)
    assert any("ComfyUI · 第 1 段 · R2V 7s · 等待结果" in update for update in reports)
    assert "raw provider update" not in "\n".join(reports)
    assert len(reports[-1]) < 2000
    assert result.output.startswith(reports[-1])
    if failed_shot is None:
        assert any("第 5 段宣传原画 · 处理中" in update for update in reports)
        assert "ComfyUI · 第 5 段 · FL2V 7s · 已完成" in result.output
        assert any("阿里云 IMS 剪辑 · 等待结果" in update for update in reports)
        assert "阿里云 IMS 剪辑 · 已完成" in result.output
        assert result.metadata["asset_id"] == "gallery:trace-test:final"
    else:
        assert "ComfyUI · 第 4 段 · R2V 7s · 已暂停" in result.output
        assert "第 5 段宣传原画" not in result.output
        assert "阿里云 IMS 剪辑" not in result.output
        assert result.metadata["error"]


@pytest.mark.asyncio
async def test_gallery_waits_for_the_original_job_past_the_standard_video_deadline(monkeypatch):
    from light import remake
    from tool import light_gallery, video_production
    from tool.tool import ToolContext, ToolResult

    times = iter([0, remake.MAX_VIDEO_WAIT + 60])
    monkeypatch.setattr(remake, "time", SimpleNamespace(monotonic=lambda: next(times)))

    async def existing(_ctx, _kind, key):
        return SimpleNamespace(id="already-queued", idempotency_key=key, status="in_progress")

    async def active(_ctx):
        return None

    async def generate(request, _ctx):
        assert request.action == "wait"
        assert request.job_id == "already-queued"
        return ToolResult(metadata={"status": "completed", "asset_id": "finished-shot"})

    monkeypatch.setattr(remake, "_existing_job", existing)
    monkeypatch.setattr(remake, "_check", active)
    monkeypatch.setattr(video_production, "execute_generate", generate)
    request = video_production.VideoGenerateArgs(
        action="submit", idempotency_key="gallery:shot1", prompt="Open on the gallery.")

    states = []

    async def report(state):
        states.append(state)

    assert await remake._job(ToolContext(), request, kind="segment", label="开场", run={"id": "app-render"},
                             wait_budget=light_gallery.FILM_WAIT, on_progress=report) == "finished-shot"
    assert states == ["resuming"]


def test_gallery_approved_third_wish_cannot_be_replaced_by_stage_direction():
    form = {"template": video_template.GALLERY_WISHES, "third_wish_line": "第三个愿望，吃得够爽！"}
    correct = "(S1) says <d>[Chinese] 第三个愿望,吃得够爽!</d>"
    wrong = "(S1) says <d>[Chinese] 第三，我要大家都冲我来!</d>"
    video_template.verify_third_wish_line(form, 4, correct)
    video_template.verify_third_wish_line(form, 3, wrong)
    video_template.verify_third_wish_line({**form, "third_wish_line": ""}, 4, wrong)
    with pytest.raises(VideoRequestError, match="指定台词"):
        video_template.verify_third_wish_line(form, 4, wrong)


def test_legacy_gallery_line_moves_into_editable_copy():
    from light import production

    story = "画家和隔壁邻居都留在画面中。"
    line = "第三个愿望，吃得够爽！"
    original = {**production_form.initial_form("video", "门店", ""),
                "template": video_template.GALLERY_WISHES, "reference_copy": story, "third_wish_line": line}
    row = SimpleNamespace(light_entry="video", light_production=original, light_example_id=None)
    copy = production._form(row)
    assert copy["reference_copy"] == f"{story}\n\n第三个愿望的指定台词：「{line}」"
    assert "third_wish_line" not in copy
    assert original["reference_copy"] == story and original["third_wish_line"] == line
    assert "third_wish_line" not in production_form.FormFields.model_fields
    production._apply(row, {"reference_copy": "画家说：「今天一起分享。」"})
    assert row.light_production["reference_copy"] == "画家说：「今天一起分享。」"
    assert "third_wish_line" not in row.light_production

    full = {**original, "reference_copy": "剧" * 2400, "third_wish_line": "词" * 100}
    migrated = video_template.promote_legacy_gallery_copy(full)["reference_copy"]
    assert production_form.FormFields(reference_copy=migrated).reference_copy == migrated


def test_pinned_graph_keeps_native_audio_and_optional_end_frame():
    task = "b466c46b-4691-4c21-ae8e-f6ab73d314ce"
    settings = {"resolution": "720p", "ratio": "9:16", "duration": 7, "seed": 33,
                "prompt": "(S1) says <d>[Chinese] 四十五元一斤.</d>"}
    graph = h3.workflow(settings, {"first_frame": "first.jpg", "last_frame": "last.jpg"}, task)
    media = graph["13"]["inputs"]
    assert (media["width"], media["height"], media["length"]) == (640, 1120, 175)
    assert graph["11"]["inputs"]["image"] == "first.jpg"
    assert graph["12"]["inputs"]["image"] == "last.jpg"
    assert graph["17"]["inputs"]["seed"] == 33
    assert graph["21"]["inputs"]["filename_prefix"] == f"openbox-h3/{task}/clip"
    assert any(node["class_type"] == "VAEDecodeAudio" for node in graph.values())
    prompt = graph["23"]["inputs"]["h3_prompt"]
    assert prompt.splitlines()[0].endswith("7.29-second mark of the target video.")
    assert f"integrated_multimodal_description: [Shot 1] {settings['prompt']}" in prompt
    assert "non_diegetic_music: N/A" in prompt
    single = h3.workflow(settings, {"first_frame": "first.jpg"}, task)
    assert "12" not in single and "last_frame" not in single["13"]["inputs"]
    assert "last_frame" in media
    assert "0.00 seconds" in single["23"]["inputs"]["h3_prompt"].splitlines()[0]


def test_poster_profile_reaches_the_actual_provider_graph_only_on_request():
    refs = [{"mime": "image/png", "role": role, "object_key": "approved.png"}
            for role in ("first_frame", "last_frame")]
    props = {"prompt": "Locked artwork, fixed type, tiny glint.", "refs": refs, "resolution": "720p",
             "ratio": "9:16", "duration": 7, "generate_audio": True, "watermark": False, "seed": 42}
    frames = {"first_frame": "approved.png", "last_frame": "approved.png"}
    task = "ae76da31-b468-402f-8650-d5ccb05f204a"
    _, base = h3.build_payload(**props)
    _, poster = h3.build_payload(**props, h3_profile="poster")
    standard_graph = h3.workflow(base, frames, task)
    graph = h3.workflow(poster, frames, task)
    assert "h3_profile" not in base and poster["h3_profile"] == "poster"
    assert graph["15"]["inputs"]["steps"] == 12 and standard_graph["15"]["inputs"]["steps"] == 10
    assert graph["6"]["inputs"]["model"] == ["81", 0]
    assert graph["81"]["inputs"]["model"] == ["80", 0]
    assert graph["80"]["inputs"]["model"] == standard_graph["6"]["inputs"]["model"]
    assert [graph[k]["inputs"]["strength_model"] for k in ("80", "81")] == [0.5, 0.8]
    assert graph["23"]["inputs"]["realism"] and graph["24"]["inputs"]["enabled"]
    for key in ("5", "11", "12", "13", "17"):
        assert graph[key] == standard_graph[key]


def test_direct_poster_motion_region_preserves_artwork_and_native_audio_in_sampler():
    route = SimpleNamespace(channel="comfyui", base_url="https://comfy.example:31876")
    refs = [{"mime": "image/png", "role": role, "object_key": "art.png"} for role in ("first_frame", "last_frame")]
    props = dict(prompt="Locked camera, steam rises inside the dish.", refs=refs, resolution="720p", ratio="9:16",
                 duration=6, generate_audio=True, watermark=False, seed=24, h3_profile="poster")
    region = [0.25, 0.50, 0.50, 0.25]
    _, body = video_providers.build_payload(route, **props, poster_motion_region=region)
    uploaded = [(r["role"], "art.png", 0) for r in refs]
    graph = h3.turbo_workflow(body, uploaded, "ae76da31-b468-402f-8650-d5ccb05f204a")
    assert body["poster_motion_region"] == region
    assert graph["17"]["inputs"]["latent_image"] == ["99", 0] and graph["99"]["inputs"]["mask"] == ["98", 0]
    assert graph["90"]["inputs"]["image"] == ["11", 0] and graph["91"]["inputs"]["amount"] == graph["13"]["inputs"]["length"]
    assert graph["94"]["inputs"] == {"video_latent": ["92", 0], "audio_latent": ["93", 1]}
    assert graph["93"]["inputs"]["av_latent"] == ["13", 1]
    assert graph["95"]["inputs"] == {"value": 0.0, "width": 640, "height": 1120}
    assert graph["96"]["inputs"] == {"value": 1.0, "width": 320, "height": 280}
    assert (graph["98"]["inputs"]["x"], graph["98"]["inputs"]["y"]) == (160, 560)
    assert graph["15"]["inputs"]["steps"] == 12 and graph["20"]["inputs"]["images"] == ["18", 0]
    _, ordinary = video_providers.build_payload(route, **props)
    assert "99" not in h3.turbo_workflow(ordinary, uploaded, "ae76da31-b468-402f-8650-d5ccb05f204a")


@pytest.mark.parametrize(("region", "profile", "direct", "last"), [
    ([0, 0, 0, 0.5], "poster", True, "a"), ([0.8, 0, 0.5, 1], "poster", True, "a"),
    ([0, 0, 1, 1], "standard", True, "a"), ([0, 0, 1, 1], "poster", False, "a"),
    ([0, 0, 1, 1], "poster", True, "b"), ([0, float("nan"), 1, 1], "poster", True, "a"),
])
def test_rejects_unusable_poster_motion_region(region, profile, direct, last):
    with pytest.raises(VideoRequestError):
        h3.validate_poster_motion_region(region, profile, direct, {"first_frame": "a", "last_frame": last})


@pytest.mark.asyncio
async def test_reference_submit_uses_workbench_and_binds_video_without_source_audio(monkeypatch):
    args = {"resolution": "720p", "ratio": "9:16", "duration": 9, "seed": 33,
            "prompt": "Keep the motion from <Video 1> and show the object from <Picture 1>."}
    roles = ("reference_video", "reference_image")
    refs = [
        {"role": "reference_video", "mime": "video/mp4", "object_key": "move.mp4"},
        {"role": "reference_image", "mime": "image/jpeg", "object_key": "product.jpg"},
    ]
    _, body = h3.build_payload(**args, refs=refs, generate_audio=True, watermark=False)
    submitted = {}

    async def upload(_connection, ref):
        return ref["role"], ref["object_key"], 0.0

    def accept(request):
        assert request.method == "POST"
        assert request.url.path == "/proxy/18189/prompt"
        submitted.update(json.loads(request.content))
        return httpx.Response(200, json={"prompt_id": submitted["prompt_id"]})

    monkeypatch.setattr(h3, "_upload_reference", upload)
    monkeypatch.setattr(h3, "client", lambda route, timeout: httpx.AsyncClient(
        base_url=route.base_url, timeout=timeout, transport=httpx.MockTransport(accept)))
    route = SimpleNamespace(base_url="https://example.test/proxy/18189", submit_timeout_seconds=5)
    queued = await h3.submit(route, body)
    assert queued == {"id": submitted["prompt_id"], "status": "queued"}

    graph = submitted["prompt"]
    inputs = graph["13"]["inputs"]
    assert graph["13"]["class_type"] == "MiniMaxH3ReferenceToVideo"
    assert (inputs["width"], inputs["height"], inputs["length"]) == (640, 1120, 226)
    assert inputs["prompt"] == ["23", 0]
    assert graph["23"]["inputs"]["h3_prompt"] == args["prompt"]
    assert graph["4"]["inputs"]["unet_name"] == "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors"
    assert graph["7"]["class_type"] == "BlockSparseAttention"
    assert graph["9"]["class_type"] == "MiniMaxChunkFeedForward"
    assert graph["14"]["inputs"]["sampler_name"] == "euler"
    assert graph["15"]["inputs"] == {"model": ["10", 0], "scheduler": "simple", "steps": 10, "denoise": 1.0}
    sampler = graph["17"]
    assert sampler["class_type"] == "SelfLiftH3Sampler"
    assert sampler["inputs"]["model"] == ["10", 0]
    assert sampler["inputs"]["positive"] == ["13", 0]
    assert sampler["inputs"]["latent_image"] == ["13", 1]
    assert sampler["inputs"]["seed"] == 33
    assert inputs["ref_videos.ref_video_0"] == ["31", 0]
    assert graph["31"]["inputs"]["video"] == ["30", 0]
    assert graph["30"]["inputs"]["file"] == "move.mp4"
    assert inputs["ref_images.ref_image_0"] == ["32", 0]
    assert not any(key.startswith("ref_video_audios") for key in inputs)
    assert graph["19"]["class_type"] == "VAEDecodeAudio"
    assert graph["20"]["inputs"]["audio"] == ["19", 0]
    assert graph["21"]["inputs"]["filename_prefix"] == f"openbox-h3/{queued['id']}/clip"
    with pytest.raises(VideoRequestError, match="对应"):
        h3.validate(resolution="720p", ratio="9:16", duration=9, prompt="Follow <Video 2>.",
                    roles=roles, input_mimes=["video/mp4", "image/jpeg"], generate_audio=True, watermark=False)


def test_native_dialogue_is_preserved_for_prompt_and_extracted_for_review():
    text = "(S1) <d>[Chinese] 四十五元一斤.</d> (S2) <d>[Chinese] 够便宜!</d>"
    assert resolve_spoken_line(text, spoken_line=None, no_speech=False, generate_audio=True,
                               native_dialogue=True) == (text, "四十五元一斤. 够便宜!")
    with pytest.raises(VideoRequestError, match="台词"):
        h3.dialogue("<d>没有语言标记</d>")
    crossing = "[Shot 1] (S1) <d>[Chinese] 转到<scenetrans></d> [Shot 2] (S1) <d>[Chinese] <scenetrans>这里!</d>"
    assert resolve_spoken_line(crossing, spoken_line=None, no_speech=False, generate_audio=True,
                               native_dialogue=True) == (crossing, "转到 这里!")
    assert h3.dialogue("<d>[Chinese] 来<cutoff></d>") == ["来"]
    for invalid in ("<d>[Chinese] <wrong>词</d>", "<d>[Chinese] <cutoff></d>"):
        with pytest.raises(VideoRequestError):
            h3.dialogue(invalid)


def test_comfyui_native_audio_uses_its_own_numbered_slots():
    config = OpenBoxConfig.model_validate({"video_generation": {"models": [{
        "id": h3.MODEL, "channel": "comfyui", "resolutions": ["480p"],
        "supports_reference_audio": True, "supports_reference_video": False,
    }]}})
    declared = video_providers.declared_model(h3.MODEL, config)
    route = SimpleNamespace(channel="comfyui", model=h3.MODEL)
    prompt = "<Audio 1> is the voice of (S1); <Audio 2> is the voice of (S2); <Picture 1> is the scene."
    options = {"resolution": "480p", "ratio": "9:16", "duration": 5, "generate_audio": True, "watermark": False}
    roles = ("reference_audio", "reference_image", "reference_audio")
    mimes = ["audio/wav", "image/png", "audio/mpeg"]
    video_providers.validate_request(route, declared=declared, roles=roles, input_mimes=mimes,
                                     prompt=prompt, **options)
    args = {**options, "prompt": prompt, "seed": 53}
    task = "b466c46b-4691-4c21-ae8e-f6ab73d314ce"
    graph = h3.reference_workflow(args, list(zip(roles, ("voice.wav", "room.png", "other.wav"), strict=True)), task)
    inputs = graph["13"]["inputs"]
    assert inputs["ref_audios.ref_audio_0"] == ["30", 0]
    assert inputs["ref_images.ref_image_0"] == ["32", 0]
    assert inputs["ref_audios.ref_audio_1"] == ["34", 0]
    assert graph["30"] == {"class_type": "LoadAudio", "inputs": {"audio": "voice.wav"}}
    assert graph["17"]["class_type"] == "SelfLiftH3Sampler"
    assert graph["17"]["inputs"]["seed"] == 53
    assert not any(key.startswith("ref_video_audios") for key in inputs)
    with pytest.raises(VideoRequestError, match="标签须和实际素材"):
        h3.validate(roles=roles, input_mimes=mimes, prompt="Follow <Audio 3>.", **options)
    with pytest.raises(VideoRequestError, match="须同时提供"):
        h3.validate(roles=("reference_audio",), input_mimes=["audio/wav"], prompt="Follow <Audio 1>.", **options)
    with pytest.raises(VideoRequestError, match="R2V"):
        h3.validate(roles=("first_frame",), input_mimes=["image/png"], prompt="Follow <Audio 1>.", **options)
    with pytest.raises(VideoRequestError, match="参考音频角色"):
        h3.validate(roles=("first_frame", "reference_audio"), input_mimes=["image/png", "audio/wav"],
                    prompt="Follow <Audio 1>.", **options)


def test_reference_audio_normalizes_to_stereo_wave():
    import io
    import shutil
    import wave

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required for media normalization")

    def mono_wave(seconds):
        source = io.BytesIO()
        with wave.open(source, "wb") as f:
            f.setparams((1, 2, 8_000, 0, "NONE", "not compressed"))
            f.writeframes(b"\x00\x00" * (8_000 * seconds))
        return source.getvalue()

    normalized, duration = h3._normalize_reference_audio(mono_wave(3), "audio/wav")
    assert duration == 3
    with wave.open(io.BytesIO(normalized), "rb") as result:
        assert (result.getframerate(), result.getnchannels(), result.getsampwidth()) == (32_000, 2, 2)
    with pytest.raises(VideoRequestError, match="每段须为 2–15 秒"):
        h3._normalize_reference_audio(mono_wave(1), "audio/wav")
    with pytest.raises(VideoRequestError, match="无法读取"):
        h3._normalize_reference_audio(b"not a WAV", "audio/wav")


def test_token_pinned_to_proxy_and_output_path(monkeypatch):
    endpoint = "https://example.test/proxy/18189"
    provider = SimpleNamespace(base_url=endpoint, api_key="test-only-secret-token")
    assert h3.configured_endpoint(provider) == (endpoint, provider.api_key)
    route = SimpleNamespace(base_url=endpoint, api_key=provider.api_key)
    query = "?filename=clip_00001_.mp4&type=output&subfolder=openbox-h3%2Fb466c46b-4691-4c21-ae8e-f6ab73d314ce"
    assert h3.owns_output_url(route, endpoint + "/view" + query)
    assert not h3.owns_output_url(route, "https://other.test/proxy/18189/view" + query)
    assert not h3.owns_output_url(route, endpoint + "/view" + query.replace("type=output", "type=input"))
    monkeypatch.setattr(h3.httpx, "AsyncClient", lambda **kwargs: kwargs)
    transport = h3.client(route, 12)
    assert transport["headers"]["Authorization"] == "token test-only-secret-token"
    assert transport["follow_redirects"] is False


def test_direct_route_uses_its_token_file_and_scoped_ca(monkeypatch, tmp_path):
    from tool.video_production import _model_capability_lines

    endpoint = "https://comfy.example:31876"
    token_file = tmp_path / "api.token"
    token_file.write_text("example-file-token\n")
    monkeypatch.setenv("COMFYUI_API_URL", endpoint + "/")
    monkeypatch.delenv("COMFYUI_API_TOKEN", raising=False)
    monkeypatch.setenv("COMFYUI_API_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("COMFYUI_CA_FILE", str(tmp_path / "server.pem"))
    config = OpenBoxConfig.model_validate(_apply_env_overrides({}))
    route = video_providers.resolve_route(h3.MODEL, config)
    assert route.base_url == endpoint and route.api_key == "example-file-token"
    assert "Ref2VA 4 steps" in "\n".join(_model_capability_lines(config))
    ca_files = []
    monkeypatch.setattr(h3.ssl, "create_default_context", lambda *, cafile: ca_files.append(cafile) or "tls-context")
    monkeypatch.setattr(h3.httpx, "AsyncClient", lambda **kwargs: kwargs)
    client = h3.client(route, 10)
    assert ca_files == [str(tmp_path / "server.pem")] and client["verify"] == "tls-context"
    assert client["headers"]["Authorization"] == "Bearer example-file-token"
    assert client["trust_env"] is False and client["follow_redirects"] is False
    provider = config.provider["comfyui-local"]
    for url in ("http://comfy.example", endpoint + "/prompt", endpoint + "?token=secret",
                endpoint + "/old/../proxy/8188", "https://name@comfy.example", "https://comfy.example:broken"):
        with pytest.raises(VideoRequestError):
            h3.configured_endpoint(provider.model_copy(update={"base_url": url}))


@pytest.mark.parametrize(("form", "requested", "expected"), [
    ({"template": "gallery_wishes"}, None, "hybrid"),
    ({"template_ref": {"edition_id": "vt_test"}, "template_recipe": {"reference_pack": "gallery-stage"}},
     "standard", "hybrid"),
    ({"template_ref": {"edition_id": "vt_test"}, "template_recipe": {"reference_pack": "gallery-stage"}},
     "poster", "poster"),
    ({"template_ref": {"edition_id": "vt_other"}, "template_recipe": {"reference_pack": ""}}, None, None),
    ({"template_recipe": {"reference_pack": "gallery-stage"}}, None, None),
    (None, "standard", None),
    (None, "poster", "poster"),
])
def test_template_resolves_h3_profile(form, requested, expected):
    assert video_template.effective_h3_profile(form, requested) == expected


@pytest.mark.parametrize("profile", [None, "hybrid"])
@pytest.mark.parametrize(("roles", "last_start"), [
    ((), None), (("first_frame",), False), (("last_frame",), True), (("last_frame", "first_frame"), True),
])
def test_direct_base_modes_bind_the_correct_frames(roles, last_start, profile):
    route = SimpleNamespace(base_url="https://comfy.example", channel="comfyui", model=h3.MODEL)
    refs = [{"role": role, "mime": "image/png", "object_key": role + ".png"} for role in roles]
    path, body = video_providers.build_payload(
        route, prompt="The tray slides into view.", refs=refs, resolution="720p", ratio="9:16", duration=5,
        generate_audio=True, watermark=False, seed=123, h3_profile=profile)
    assert path == "/prompt"
    graph = h3.turbo_workflow(body, [(r, r + ".png", 0) for r in roles], "794cde46-dd03-4a70-bd77-79092ea1b0d0")
    inputs = graph["13"]["inputs"]
    assert (inputs["width"], inputs["height"], inputs["length"]) == (640, 1120, 124)
    if profile == "hybrid":
        assert graph["4"]["inputs"]["unet_name"] == "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors"
        assert graph["15"]["inputs"]["steps"] == 10 and graph["17"]["inputs"]["seed"] == 123
        assert graph["17"]["inputs"]["positive"] == ["13", 0] and graph["17"]["inputs"]["transition_step"] == 6
        assert "80" not in graph and "81" not in graph
    else:
        assert graph["4"]["inputs"]["unet_name"].startswith("minimax_h3_fl2va_")
        assert graph["15"]["inputs"]["steps"] == 8 and graph["22"]["inputs"]["noise_seed"] == 123
    assert "integrated_multimodal_description: [Shot 1] The tray slides into view." in inputs["prompt"]
    assert ("5.17-second" in inputs["prompt"] or "5.17 seconds" in inputs["prompt"]) == bool(last_start)
    assert inputs["prompt"].startswith("integrated_multimodal_description:") == (not roles)
    for role, node in (("first_frame", "11"), ("last_frame", "12")):
        assert (node in graph) == (role in inputs) == (role in roles)
        if role in roles:
            assert graph[node]["inputs"]["image"] == role + ".png" and inputs[role] == [node, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["standard", "hybrid", "poster"])
async def test_direct_submit_binds_anchors_motion_and_independent_reference_slots(monkeypatch, profile):
    route = SimpleNamespace(base_url="https://comfy.example:31876", channel="comfyui",
                            model=h3.MODEL, submit_timeout_seconds=5)
    roles = ("last_frame", "reference_video", "reference_audio", "first_frame", "reference_image", "reference_video")
    mimes = ("image/png", "video/mp4", "audio/wav", "image/png", "image/png", "video/mp4")
    opts = dict(resolution="720p", ratio="16:9", duration=5, generate_audio=True, watermark=False)
    prompt = "<Picture 1> supplies the object. <Video 2> supplies its motion. <Audio 1> supplies its timbre."
    refs = [{"role": r, "mime": m, "object_key": f"input-{i}"} for i, (r, m) in enumerate(zip(roles, mimes))]
    video_providers.validate_request(route, roles=roles, input_mimes=mimes, prompt=prompt, **opts)
    with pytest.raises(VideoRequestError, match="对应"):
        video_providers.validate_request(route, roles=roles, input_mimes=mimes, prompt="<Picture 2>", **opts)
    with pytest.raises(VideoRequestError, match="一张"):
        video_providers.validate_request(route, roles=("last_frame", "last_frame"), input_mimes=["image/png"] * 2,
                                         prompt=prompt, **opts)
    _, body = video_providers.build_payload(route, refs=refs, prompt=prompt, seed=78, h3_profile=profile, **opts)

    async def upload(connection, ref):
        seconds = {"reference_video": 11.5, "reference_audio": 2.6}.get(ref["role"], 0)
        return ref["role"], ref["object_key"], seconds

    posted = {}
    def accept(request):
        assert request.url.path == "/prompt"
        posted.update(json.loads(request.content))
        return httpx.Response(200, json={"prompt_id": posted["prompt_id"]})
    monkeypatch.setattr(h3, "_upload_reference", upload)
    monkeypatch.setattr(h3, "client", lambda route, timeout: httpx.AsyncClient(
        base_url=route.base_url, transport=httpx.MockTransport(accept)))
    handle = await h3.submit(route, body)
    graph = posted["prompt"]
    plan = graph["30"]["inputs"]
    timeline = json.loads(plan["timeline_data"])
    assert (plan["width"], plan["height"], plan["generation_seconds"]) == (1120, 640, 124 / 24)
    assert [(v["file"], v["duration"], v["hasAudio"]) for v in timeline["videoClips"]] == [
        ("input-1", 11.5, False), ("input-5", 11.5, False)]
    assert timeline["videoAudioEnabled"] is False
    assert timeline["images"] == [{"file": "input-4"}]
    assert timeline["audios"] == [{"file": "input-2", "duration": 2.6, "audioMode": "reference"}]
    assert graph["13"]["inputs"]["prompt"] == prompt and graph["13"]["inputs"]["plan"] == ["30", 0]
    assert graph["11"]["inputs"]["image"] == "input-3" and graph["12"]["inputs"]["image"] == "input-0"
    for node, parent, loader, index in (("31", "13", "11", 0), ("32", "31", "12", -1)):
        assert graph[node] == {"class_type": "MiniMaxH3AddGuide", "inputs": {
            "positive": [parent, 0], "image": [loader, 0], "latent": ["13", 1], "vae": ["2", 0], "frame_idx": index}}
    assert graph["16"]["inputs"]["conditioning"] == ["32", 0]
    assert graph["20"]["inputs"]["images"] == ["18", 0] and graph["20"]["inputs"]["audio"] == ["19", 0]
    assert graph["21"]["inputs"]["filename_prefix"] == f"openbox-h3/{handle['id']}/clip"
    for node in graph.values():
        assert not node["class_type"].startswith("ShortDrama")
        assert all(v[0] in graph for v in node["inputs"].values() if isinstance(v, list))
    if profile in ("hybrid", "poster"):
        assert graph["4"]["inputs"]["unet_name"] == "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors"
        assert graph["15"]["inputs"]["steps"] == (12 if profile == "poster" else 10)
        if profile == "poster":
            assert graph["21"]["inputs"]["format.codec.encoding.crf"] == 16
        assert graph["17"]["class_type"] == "SelfLiftH3Sampler"
        assert graph["17"]["inputs"]["positive"] == ["32", 0] and graph["17"]["inputs"]["seed"] == 78
    else:
        assert graph["15"]["inputs"]["steps"] == 4
        assert graph["5"]["inputs"]["lora_name"].startswith("minimax_h3_ref2v_turbo_4step")
        assert graph["4"]["inputs"]["unet_name"].startswith("minimax_h3_ref2va_")


def test_reference_video_duration_measures_the_uploaded_silent_picture_track(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are needed for reference preparation")
    source = tmp_path / "longer-audio.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=s=32x32:r=24:d=2.5",
        "-f", "lavfi", "-i", "anullsrc=r=32000:cl=stereo:d=4", "-c:v", "libx264", "-c:a", "aac", str(source),
    ], check=True, timeout=15)
    content, seconds = h3._normalize_reference_video(source.read_bytes(), "video/mp4")
    assert seconds == 2.5
    uploaded = tmp_path / "reference.mp4"
    uploaded.write_bytes(content)
    streams = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(uploaded),
    ], timeout=10)
    assert streams.strip() == b"video"
