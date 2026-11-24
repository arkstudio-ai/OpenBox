from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from PIL import Image

from light import product_story as director
from light import product_story_continuity as continuity
from light import product_story_plan as recipe
from light import production, wan_story
from light.mode import LightError
from light.production_form import TEMPLATES, initial_form
from light.remake_case import input_fingerprint
from light.remake import RemakeStopped
from tool.light_remake import wan_tool_for_action
from tool.video_runninghub import MODEL, validate as validate_runninghub


def brand_plan(product, material, opening, details, line=""):
    lengths = (6, 6, 6, 6, 6, 6, 7, 7)
    return recipe.BrandPlan(
        language="vi", title=opening, product=product, product_assets=[material], appearance=details,
        setting_and_cast="Ánh sáng ban mai, diễn viên mặc áo xanh.",
        identity_frame="Một người đi làm mặc áo xanh.", headline="Một phút cho mình.",
        shots=[
            recipe.BrandShot(scene=opening, seconds=seconds, frame=opening, action=opening,
                             product_in_first_frame=index >= 4,
                             speaker="Người đi làm" if line and index == 0 else "",
                             line=line if index == 0 else "")
            for index, seconds in enumerate(lengths)
        ] + [recipe.BrandShot(scene="Sản phẩm", kind="product", seconds=4, frame=details,
                              action="Giữ khung hình", product_in_first_frame=True)],
    )


def locked_plan():
    old = brand_plan("Cà phê Mộc", "coffee", "Hương cà phê", "Túi giấy nâu", "Mời bạn.")
    return recipe.ContinuityPlan.model_validate({
        **old.model_dump(exclude={"format", "shots"}),
        "cast": {"Người đi làm": "Áo xanh", "Bạn": "Áo trắng"},
        "locations": {"counter": "Quầy gỗ", "studio": "Nền tối"},
        "states": {
            "waiting": {"location": "counter", "present": ["Người đi làm"], "facts": "Chưa có khách."},
            "arrived": {"location": "counter", "present": ["Người đi làm", "Bạn"], "facts": "Bạn đứng cạnh quầy."},
            "hero": {"location": "studio", "present": [], "facts": "Túi cà phê bên trái."},
        },
        "shots": [
            {**shot.model_dump(), "before": "waiting" if index < 2 else "arrived",
             "after": "waiting" if index == 0 else "arrived",
             "first_cast": ["Người đi làm"], "last_cast": ["Người đi làm"],
             "last_frame": "Người đi làm đặt tách xuống.", "product_in_last_frame": shot.product_in_first_frame,
             "transition": "match" if index == 1 else "cut", "edit_note": "Cắt sang chiếc tách."}
            for index, shot in enumerate(old.shots[:-1])
        ] + [{
            **old.shots[-1].model_dump(), "before": "hero", "after": "hero",
            "first_cast": [], "last_cast": [], "product_in_last_frame": True, "transition": "end_card",
        }],
    })


def test_locked_shot_rejects_early_cast_reset_and_internal_edit():
    plan = locked_plan()
    data = plan.model_dump()
    broken = {**data, "shots": [{**data["shots"][0], "first_cast": ["Bạn"]}, *data["shots"][1:]]}
    with pytest.raises(ValidationError, match="already present"):
        recipe.ContinuityPlan.model_validate(broken)
    broken = {**data, "shots": [*data["shots"][:2], {**data["shots"][2], "before": "waiting"}, *data["shots"][3:]]}
    with pytest.raises(ValidationError, match="previous end state"):
        recipe.ContinuityPlan.model_validate(broken)
    broken = {**data, "shots": [{**data["shots"][0], "action": "Cắt sang cận cảnh."}, *data["shots"][1:]]}
    with pytest.raises(ValidationError, match="Move edit instructions"):
        recipe.ContinuityPlan.model_validate(broken)

    assert wan_story.parse_plan(data, template=recipe.TEMPLATE) == plan
    shot = recipe.shots(plan)[0]
    request = director.shot_request("test", 1, shot, "first", 1, last="last")
    assert [item.role for item in request.input_assets] == ["first_frame", "last_frame"]
    assert "Cắt sang chiếc tách" not in request.prompt
    assert shot["last_frame"] in request.prompt and request.spoken_line == "Mời bạn."
    validate_runninghub(request.model, resolution=request.resolution, ratio=request.ratio, duration=request.duration,
                        input_mimes=["image/png"] * 2, roles=("first_frame", "last_frame"),
                        generate_audio=request.generate_audio, prompt=request.prompt, watermark=request.watermark)
    assert "Dựng nối" in recipe.display_script(plan)


def test_locked_movie_needs_visual_proof_for_same_cuts_and_card():
    plan = locked_plan()
    story = recipe.shots(plan)
    cuts = [{"shot": i, "source": f"take-{i}", "frames": shot["frames"], "start": 0, "speed": 1,
             "output_frames": shot["frames"], "review_board": f"board-{i}",
             "cues": [{"text": shot["spoken_line"]}] if shot["spoken_line"] else []}
            for i, shot in enumerate(story[:-1], 1)]
    run = {"remake_adaptation": plan.model_dump(), "wan_cuts": cuts, "wan_final_key": "current"}
    job = SimpleNamespace(idempotency_key="current", kind="compose", status="completed", output_asset_id="ad",
                          request_data=recipe.final_request(cuts, "vi", plan=plan, end_card="hero"),
                          result_data={"frames": 1296, "fps": 24, "duration_sec": 54,
                                       "has_audio": True, "language": "vi", "dialogue": 1})
    assert not wan_story.is_complete_cut(run, job)
    check = {"approved": True, "cut_assets": [c["review_board"] for c in cuts], "end_card": "hero"}
    run["director_decisions"] = {f"{recipe.VISUAL_REVIEW}:joins": check}
    assert wan_story.is_complete_cut(run, job)
    check["end_card"] = "old-hero"
    assert not wan_story.is_complete_cut(run, job)


async def test_locked_stills_share_only_deliberate_matches_and_gate_rejected_assets(monkeypatch):
    plan = locked_plan()
    prompts = {}
    checked = []

    async def still(_ctx, _prefix, name, text, refs, **_):
        prompts[name] = text, refs
        return name

    async def audit(_ctx, name, schema, guide, data, refs, **_):
        checked.append((name, data["shot"], refs))
        if "brand-v3-first-01-0" in refs:
            return {"report": {"ok": False, "reason": "Early character",
                               "first_correction": "Keep the doorway empty."}}
        if "brand-v3-last-03-0" in refs:
            return {"report": {"ok": False, "reason": "The object has not settled",
                               "last_correction": "The cup is already on the table."}}
        return {"report": {"ok": True, "reason": "Expected cast and props"}}

    monkeypatch.setattr(continuity, "_still", still)
    monkeypatch.setattr(continuity, "_material_refs", AsyncMock(return_value=["coffee-photo"]))
    monkeypatch.setattr(continuity.reference_quality, "ask", audit)
    monkeypatch.setattr(production, "save_director_decision", AsyncMock(side_effect=lambda *args: args[-1]))
    pairs, materials = await continuity.prepare(None, "test", {}, plan)
    assert materials == ["coffee-photo"]
    assert pairs[2][0] == pairs[1][1] and pairs[1][1].startswith("brand-v3-last-01-1-endpoint-")
    assert 2 == sum(index == 1 for _, index, _ in checked)
    assert prompts[pairs[2][1]][1][0] == pairs[2][0]
    assert pairs[3][0] == "brand-v3-first-03-0" and pairs[3][1].startswith("brand-v3-last-03-1-endpoint-")
    assert "镜头必须裁掉商品及其支架所在的那块场景" in prompts[pairs[3][0]][0]
    assert "brand-v3-first-03-1-endpoint" not in prompts
    assert "already on the table." in prompts[pairs[3][1]][0]
    assert continuity._endpoint_name("last", "03-1", "Keep the cup", "first") != (
        continuity._endpoint_name("last", "03-1", "Keep the stand", "first")
    )
    assert prompts[pairs[9][0]][1] == ["coffee-photo"] and pairs[9][1] is None
    assert not any(key.startswith("brand-v3-cast") and "Bạn" in text
                   for key, (text, _) in prompts.items())


def test_locked_motion_detects_abrupt_cut_without_rejecting_a_gradual_move():
    moves = [Image.new("RGB", (128, 72), (i * 13, i * 13, i * 13)) for i in range(10)]
    assert continuity.internal_jump(moves) is None
    assert continuity.internal_jump([*moves, Image.new("RGB", (128, 72), "white")]) == 10


async def test_join_repair_recalls_product_placement_after_a_cutaway_in_same_location(monkeypatch):
    plan = locked_plan()
    shots = list(plan.shots)
    shots[1] = shots[1].model_copy(update={"product_in_last_frame": True})
    shots[3] = shots[3].model_copy(update={"product_in_first_frame": True})
    plan = plan.model_copy(update={"shots": shots})
    tails = {2: "headphones-on-stand", 3: "cup-detail"}
    assert continuity._prior_product_tail(plan, tails, 4) == "headphones-on-stand"

    async def still(_ctx, _prefix, name, _text, _refs, **_):
        return name

    review = AsyncMock(return_value={"report": {"ok": True, "reason": "Expected anchors"}})
    monkeypatch.setattr(continuity, "_still", still)
    monkeypatch.setattr(continuity, "_material_refs", AsyncMock(return_value=["product-photo"]))
    monkeypatch.setattr(continuity.reference_quality, "ask", review)
    monkeypatch.setattr(production, "save_director_decision", AsyncMock(side_effect=lambda *args: args[-1]))
    run = {"wan_cuts": [{"shot": i, "review_tail": asset} for i, asset in tails.items()], "brand_repairs": [
        {"revision": 1, "kind": "join", "shots": [4], "report": {"correction": "Keep the stand"}},
        {"revision": 2, "kind": "endpoint", "shots": [4], "report": {"correction": "Clear the cup"}},
    ]}
    await continuity.prepare(None, "test", run, plan)
    data, refs = next(call.args[4:6] for call in review.await_args_list if call.args[4]["shot"] == 4)
    assert data["join_refs_after_actor_refs"] == ["previous actual tail", "last visible product tail"]
    assert "Keep the stand" in data["repair_instructions"]
    assert "Clear the cup" in data["repair_instructions"]
    assert refs[-4:-2] == ["cup-detail", "headphones-on-stand"]

    shots[2] = shots[2].model_copy(update={"transition": "ellipsis"})
    assert continuity._prior_product_tail(plan, tails, 4) is None


async def test_static_repair_only_reuses_a_reviewed_actor_free_product_frame(monkeypatch):
    shot = locked_plan().shots[0]
    ask = AsyncMock(return_value={"report": {"eligible": False, "reason": "An object needs to move"}})
    monkeypatch.setattr(continuity.reference_quality, "ask", ask)
    download = AsyncMock()
    assert await continuity.approved_hold(None, 1, shot, ("first", "last"), 120, download) is None
    ask.assert_not_awaited()
    still = shot.model_copy(update={"line": "", "first_cast": [], "last_cast": [],
                                    "product_in_first_frame": True, "product_in_last_frame": True})
    assert await continuity.approved_hold(None, 4, still, ("first", "last"), 120, download) is None
    download.assert_not_awaited()

    async def motion(folder, index, plate, frames, zoom):
        assert (index, plate.name, frames, zoom) == (4, "approved-frame.png", 120, "1")
        output = folder / "movie.mp4"
        output.write_bytes(b"fixed pixels")
        return output

    ask.return_value = {"report": {"eligible": True, "reason": "Fixed product insert"}}
    monkeypatch.setattr(continuity.post, "_motion", motion)
    store = AsyncMock(return_value="done")
    monkeypatch.setattr(continuity, "_reference_asset", store)
    assert await continuity.approved_hold(None, 4, still, ("first", "last"), 120, download) == "done"
    assert download.await_args.args[1] == "first"
    assert store.await_args.kwargs == {"mime": "video/mp4"}


async def test_failed_join_reopens_only_the_target_and_shared_endpoint(monkeypatch):
    plan = locked_plan()
    cuts = [{"shot": i, "source": f"take-{i}", "review_board": f"board-{i}",
             "review_version": recipe.VISUAL_REVIEW} for i in range(1, 9)]
    row = SimpleNamespace(light_entry="video", light_production={"run": {
        "job_prefix": "test", "wan_review_version": recipe.VISUAL_REVIEW, "remake_version": wan_story.VERSION,
        "remake_adaptation": plan.model_dump(), "wan_cuts": cuts,
    }})

    @asynccontextmanager
    async def session():
        yield None

    monkeypatch.setattr(production, "get_db_session", session)
    monkeypatch.setattr(production, "_lock", AsyncMock(return_value=row))
    ctx = SimpleNamespace(session_id="draft", user_id="owner", workspace_id="current", run_fence=())
    report = {"reason": "Tai nghe biến mất.", "correction": "Giữ nguyên tai nghe."}
    run = await production.reopen_brand_cut(ctx, "test", cuts=cuts, target=1, report=report)
    assert run["brand_repairs"][0]["shots"] == [1, 2]
    assert run["wan_cuts"] == cuts[2:]
    assert run["brand_repairs"][0]["previous_cuts"] == cuts[:2]
    assert run["brand_feedback"] == {"shot": 1, **report}

    with pytest.raises(LightError, match="superseded"):
        await production.save_reference_cut(ctx, "test", cuts[0])
    await production.save_reference_cut(ctx, "test", {**cuts[0], "repair_revision": 1, "source": "fresh"})
    assert [cut["source"] for cut in row.light_production["run"]["wan_cuts"]] == ["fresh", *(c["source"] for c in cuts[2:])]
    correction = "Giữ nguyên vị trí của tai nghe trên giá sát cửa sổ khi nhân vật ngồi xuống. " * 3
    correction += "Không để lóe sáng che viền máy ở cuối cảnh."
    request = director.shot_request("test", 1, recipe.shots(plan)[0], "first", 2,
                                    last="last", revision=1, correction=correction)
    assert request.idempotency_key == "test:shot1:repair:1:retake:2"
    assert "Không để lóe sáng che viền máy ở cuối cảnh" in request.prompt
    assert "固定照明中的反光保持静止" in request.prompt


async def test_join_review_reports_numeric_repair_target(monkeypatch):
    plan = locked_plan()
    result = {"report": {"ok": False, "shot": 6, "reason": "Món đồ biến mất.", "correction": "Giữ ở trên bàn."}}
    review = AsyncMock(return_value=result)
    monkeypatch.setattr(continuity.reference_quality, "ask", review)
    with pytest.raises(continuity.JoinRejected) as exc:
        await continuity.approve_joins(None, "test", {}, plan, [{"review_board": "board"}], "hero", [])
    assert exc.value.check.shot == 6
    assert exc.value.review_feedback == {"shot": 6, "reason": "Món đồ biến mất.", "correction": "Giữ ở trên bàn.", "kind": "join"}
    assert review.await_args.args[4]["shots"][0]["product_in_first_frame"] is False
    assert review.await_args.args[4]["end_card_stage"] == "unlettered_plate"


def test_explicit_retry_refilms_only_rejected_motion_and_keeps_approved_frames():
    plan = locked_plan()
    prior = {
        "remake_adaptation": plan.model_dump(), "remake_error": "Wrong cup position.",
        "brand_feedback": {"kind": "motion", "shot": 1, "reason": "Wrong cup position.", "correction": "Keep holding it."},
        "wan_cuts": [{"shot": 2, "source": "kept"}],
    }
    retry = production._brand_retry(prior)
    assert retry["wan_cuts"] == prior["wan_cuts"]
    assert recipe.repair_for(retry, 1)["revision"] == 1
    assert recipe.repair_for(retry, 1, frames=True) == {}
    assert recipe.repair_for(retry, 2) == {}

    prior["brand_feedback"]["kind"] = "endpoint"
    prior["brand_feedback"]["first_asset"] = "already-correct"
    retry = production._brand_retry(prior)
    assert retry["wan_cuts"] == []
    assert recipe.repair_for(retry, 2, frames=True)["shots"] == [1, 2]
    assert recipe.repair_for(retry, 1, frames=True)["report"]["first_asset"] == "already-correct"

    prior["brand_feedback"] = {"shot": 1, "reason": "Wrong cup position.", "correction": "Keep holding it."}
    prior["remake_stage"] = "wan_edit"
    retry = production._brand_retry(prior)
    join = recipe.repair_for(retry, 1, frames=True)
    assert join["kind"] == "join"
    assert retry["wan_cuts"] == []
    retry["brand_repairs"].append({"shots": [1, 2], "kind": "endpoint", "revision": 2})
    assert recipe.repair_for(retry, 1, frames=True)["kind"] == "endpoint"
    assert recipe.repair_for(retry, 1, kind="join") == join
    prior["remake_error"] = "The provider timed out."
    assert production._brand_retry(prior) == {}


async def test_brand_renderer_rechecks_new_seam_and_keeps_other_takes(monkeypatch):
    from light import product_story_post
    from tool.tool import ToolContext

    plan = locked_plan()
    row = SimpleNamespace(light_entry="video", light_production={"run": {
        "job_prefix": "test", "wan_review_version": recipe.VISUAL_REVIEW, "remake_version": wan_story.VERSION,
        "remake_adaptation": plan.model_dump(), "wan_cuts": [],
    }})
    revisions = {}

    @asynccontextmanager
    async def session():
        yield None

    async def filmed(_ctx, _prefix, run, index, shot, frame, **_):
        revision = recipe.repair_for(run, index).get("revision", 0)
        revisions.setdefault(index, []).append(revision)
        return {"shot": index, "source": f"take-{index}-{revision}", "review_board": f"board-{index}-{revision}",
                "review_version": recipe.VISUAL_REVIEW, "repair_revision": revision,
                "start": 0, "speed": 1, "frames": shot["frames"], "output_frames": shot["frames"],
                "cues": [{"text": shot["spoken_line"]}] if shot["spoken_line"] else []}

    checks = []

    async def join(_ctx, _prefix, run, _plan, cuts, _hero, _materials):
        checks.append([c["source"] for c in cuts])
        if not run.get("brand_repairs"):
            raise continuity.JoinRejected(continuity.JoinCheck(
                ok=False, shot=1, reason="Early actor", correction="Use the empty doorway.",
            ))

    monkeypatch.setattr(production, "get_db_session", session)
    monkeypatch.setattr(production, "_lock", AsyncMock(return_value=row))
    monkeypatch.setattr(production, "begin_reference_edit", AsyncMock(return_value=row.light_production["run"]))
    monkeypatch.setattr(production, "remake_stage", AsyncMock())
    monkeypatch.setattr(production, "save_wan_edit", AsyncMock(return_value={}))
    monkeypatch.setattr(continuity, "prepare", AsyncMock(return_value=(
        {i: (f"first-{i}", f"last-{i}") for i in range(1, len(plan.shots) + 1)}, ["coffee"],
    )))
    monkeypatch.setattr(continuity, "approve_joins", join)
    monkeypatch.setattr(product_story_post, "require_tools", lambda: None)
    monkeypatch.setattr(director, "_film", filmed)
    monkeypatch.setattr(director, "_existing_job", AsyncMock(return_value=SimpleNamespace(output_asset_id="movie")))
    monkeypatch.setattr(recipe, "is_complete_cut", lambda *_: True)
    monkeypatch.setattr(director.vp, "_attach_completed", AsyncMock(return_value=True))
    ctx = ToolContext(session_id="draft", user_id="owner", workspace_id="current")
    assert await director.render(ctx, {}, row.light_production["run"], plan) == ("movie", 54)
    assert revisions[1] == revisions[2] == [0, 1]
    assert all(revisions[i] == [0] for i in range(3, 9))
    assert checks[0][2:] == checks[1][2:] and checks[0][:2] != checks[1][:2]


@pytest.mark.parametrize(("product", "material", "opening", "details", "line"), [
    ("Tai nghe Sóng", "headset", "Con tàu đến ga", "Tai nghe xanh, chụp tai tròn", "Yên tĩnh quá."),
    ("Cà phê Mộc", "coffee", "Rót nước vào phin", "Túi giấy nâu với logo Mộc", ""),
])
async def test_brand_story_uses_each_products_story_frames_and_approved_dialogue(
    monkeypatch, product, material, opening, details, line,
):
    plan = brand_plan(product, material, opening, details, line)
    form = {"shop": "", "brief": "", "asset_ids": ["location", material], "template": recipe.TEMPLATE,
            "reference_copy": "", "product_name": product, "run": {"video_language": "vi"}}
    prompt = wan_story.planning_instruction(form, "prepare")
    assert product in prompt and "28镜" in prompt and "Tiếng Việt" in prompt and "省略 plan" in prompt
    assert all(example not in prompt for example in ("小米", "生日", "手机", "21镜"))
    assert input_fingerprint(form) != input_fingerprint({**form, "product_name": "另一个商品"})
    assert input_fingerprint(form) != input_fingerprint({**form, "asset_ids": ["other"]})
    with pytest.raises(ValidationError):
        recipe.PrepareRequest.model_validate({})
    with pytest.raises(ValidationError, match="54 seconds"):
        recipe.BrandPlan.model_validate({**plan.model_dump(), "shots": [
            {**shot.model_dump(), "seconds": 5} if i == 0 else shot.model_dump()
            for i, shot in enumerate(plan.shots)
        ]})
    assert wan_story.parse_plan(plan.model_dump(), template=recipe.TEMPLATE) == plan
    preview = recipe.display_script(plan)
    assert product in preview and opening in preview and "50–54" in preview

    frames = {}

    async def refs(_ctx, assets):
        assert assets == [material]
        return assets

    async def still(_ctx, _prefix, name, text, references, **_):
        frames[name] = text, references
        return name

    monkeypatch.setattr(director, "_material_refs", refs)
    monkeypatch.setattr(director, "_still", still)
    story = recipe.shots(plan)
    plates = await director._brand_frames(None, "current", plan, story, [1, 5])
    assert plates == {1: "brand-01", 5: "brand-05", 9: "brand-09"}
    assert frames["brand-01"][1] == ["brand-cast"] and opening in frames["brand-01"][0]
    assert frames["brand-05"][1] == ["brand-cast", material]
    assert frames["brand-09"][1] == [material] and product in frames["brand-09"][0]
    assert all("手机" not in text for text, _ in frames.values())

    take = director.shot_request("current", 1, story[0], plates[1], 3 if line else 1)
    assert opening in take.prompt and take.no_speech == (not line) and take.spoken_line == (line or None)
    validate_runninghub(take.model, resolution=take.resolution, ratio=take.ratio, duration=take.duration,
                        input_mimes=["image/png"], roles=("first_frame",), generate_audio=take.generate_audio,
                        prompt=take.prompt, watermark=take.watermark)
    assert "Người ba" not in take.prompt
    cuts = [{"shot": i, "source": f"take-{i}", "frames": shot["frames"], "start": 0, "speed": 1,
             "output_frames": shot["frames"], "cues": [{"text": line}] if i == 1 and line else []}
            for i, shot in enumerate(story[:-1], 1)]
    run = {"remake_adaptation": plan.model_dump(), "wan_cuts": cuts, "wan_final_key": "current"}
    request = recipe.final_request(cuts, "vi", plan=plan, end_card=plates[9])
    job = SimpleNamespace(idempotency_key="current", kind="compose", status="completed", output_asset_id="ad",
                          request_data=request, result_data={"frames": 1296, "fps": 24, "duration_sec": 54,
                                                              "has_audio": True, "language": "vi", "dialogue": int(bool(line))})
    assert request["post_shots"] == ["end-card"] and wan_story.shot_count(run) == 8
    assert wan_story.is_complete_cut(run, job)
    cuts[0]["cues"] = [{"text": "Không đúng lời thoại."}]
    assert not wan_story.is_complete_cut(run, job)


async def test_brand_story_approval_uses_current_material_name_and_language(monkeypatch):
    receipt = SimpleNamespace(state="claimed", run_id="run", generation=1)

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(get=AsyncMock(return_value=receipt))

    form = {**initial_form("video", "", ""), "template": recipe.TEMPLATE, "asset_ids": ["coffee"],
            "product_name": "Cà phê Mộc", "run": {"action": "prepare", "id": "receipt",
                                                  "video_language": "vi", "remake_version": wan_story.VERSION}}
    row = SimpleNamespace(light_entry="video", light_example_id=None, light_production=form)
    ctx = SimpleNamespace(session_id="session", user_id="owner", workspace_id="workspace",
                          run_id="run", run_generation=1, run_fence=("receipt", "run", 1))
    monkeypatch.setattr(production, "get_db_session", session)
    monkeypatch.setattr(production, "_lock", AsyncMock(return_value=row))
    candidate = locked_plan().model_copy(update={"language": "zh", "product": "Draft name"})
    foreign = candidate.model_copy(update={"product_assets": ["stale"]})
    with pytest.raises(LightError, match="current draft's materials"):
        await production.begin_remake(ctx, foreign, requested_action="prepare")
    prepared, _, approved = await production.begin_remake(ctx, candidate, requested_action="prepare")
    assert approved.product == "Cà phê Mộc" and approved.language == "vi"
    assert approved.product in prepared["script"] and "Draft name" not in prepared["script"]

    row.light_production = {**prepared, "run": {**prepared["run"], "action": "render"}}
    _, _, filming = await production.begin_remake(ctx, candidate, requested_action="render")
    assert filming == approved


def test_brand_story_preview_and_frame_bound_delivery():
    story = recipe.Plan()
    shots = recipe.shots(story)
    assert len(shots) == 21
    assert sum(s["frames"] for s in shots) == 1296
    assert [s["id"] for s in shots if s["spoken_line"]] == ["01", "02", "14"]
    assert all((recipe.ASSETS / f"{s['id']}.jpg").is_file() for s in shots if s["id"] != "17")
    assert recipe.TEMPLATE in TEMPLATES["video"]
    assert wan_tool_for_action("prepare", recipe.TEMPLATE).parameters is wan_story.ProductPreviewRequest
    assert wan_story.parse_plan(story.model_dump(), template=recipe.TEMPLATE) == story
    assert "在手机里回看同一张合照" in recipe.display_script(story)
    assert wan_story.shot_count({"remake_adaptation": story.model_dump()}) == recipe.FILMED_COUNT


def test_synced_take_moves_the_cut_and_checks_the_approved_words():
    shot = recipe.shots(recipe.Plan())[13]
    media = {"width": 1344, "height": 768, "has_audio": True, "duration_sec": 5.1667}
    transcript = {"sentences": [{"text": "爸 往我这边一点", "from_sec": 1.35, "to_sec": 4.95}]}
    take = director.shot_request("review", 14, shot, "asset_14", 1)
    assert take.model == MODEL and take.generate_audio and take.spoken_line == shot["spoken_line"]
    assert take.input_assets[0].role == "first_frame"
    cut = director.choose_cut(shot, 14, "motion", media, transcript)
    assert cut["start"] > 0 and cut["output_frames"] == 106
    assert (cut["start"] + cut["frames"]) / 24 >= 4.95
    transcript["sentences"][0]["text"] = "看这边"
    with pytest.raises(RemakeStopped, match="指定对白"):
        director.choose_cut(shot, 14, "motion", media, transcript)


def test_final_cannot_surface_with_missing_dialogue_or_an_old_cut():
    cuts = [{"shot": i, "source": str(i), "frames": s["frames"], "start": 0, "speed": 1}
            for i, s in enumerate(recipe.shots(recipe.Plan())[:16], 1)]
    run = {"wan_cuts": cuts, "wan_final_key": "final", "remake_adaptation": recipe.Plan().model_dump()}
    job = SimpleNamespace(idempotency_key="final", kind="compose", status="completed", output_asset_id="new",
                          request_data={**recipe.final_request(cuts), "_trajectory_context": {}},
                          result_data={"frames": 1296, "fps": 24, "duration_sec": 54,
                                       "has_audio": True, "dialogue": 3})
    assert wan_story.is_complete_cut(run, job)
    job.result_data["dialogue"] = 2
    assert not wan_story.is_complete_cut(run, job)
    job.result_data["dialogue"] = 3
    job.request_data["timeline"]["shots"][7]["asset"] = "stale"
    assert not wan_story.is_complete_cut(run, job)


def test_vietnamese_copy_overrides_a_chinese_interface_and_keeps_verbatim_words():
    import unicodedata

    from light import video_language

    form = {"reference_copy": "Quảng cáo Xiaomi 18 bằng tiếng Việt."}
    assert video_language.for_form(form, "zh-CN") == "vi"
    assert video_language.for_form({"brief": "请用越南语制作这条广告。"}, "zh-CN") == "vi"
    assert video_language.for_form({"brief": "Xiaomi 18"}, "vi-VN") == "vi"
    assert video_language.for_form({"brief": "Xiaomi 18"}, "zh-CN") == "zh"
    plan = recipe.Plan(answer="Ba đang chụp mà.").localized("vi")
    assert plan.language == "vi" and plan.answer == "Ba đang chụp mà."
    assert plan.title == "Có ba trong ảnh" and "54 giây · 21 cảnh" in recipe.display_script(plan)
    shot = recipe.shots(plan)[13]
    request = director.shot_request("review", 14, shot, "frame", 1)
    assert request.spoken_line == "Ba ơi, lại gần con chút."
    assert "Vietnamese" in request.prompt and "Mandarin" not in request.prompt
    transcript = {"sentences": [{"text": unicodedata.normalize("NFD", shot["spoken_line"]),
                                 "from_sec": 0.4, "to_sec": 2.1}]}
    cut = director.choose_cut(shot, 14, "asset", {
        "width": 1344, "height": 768, "has_audio": True, "duration_sec": 5.2,
    }, transcript)
    assert cut["cues"][0]["text"] == shot["spoken_line"]
    assert video_language.MEDIA_TAGS[plan.language] == "vie"


@pytest.mark.asyncio
async def test_vietnamese_voice_replacement_keeps_the_approved_line_and_edit_identity(monkeypatch):
    from light import production_voice

    async def render(**args):
        assert args["text"] == "Ba chụp mà." and args["locale"] == "vi"
        return "test-voice.wav"

    async def get_object(key):
        assert key == "test-voice.wav"
        return b"sample"

    monkeypatch.setattr(production_voice, "render_clip", render)
    monkeypatch.setattr(production_voice, "_canonical_wav", lambda data: (data, 1.0))
    monkeypatch.setattr(director, "get_oss", lambda: SimpleNamespace(get_object=get_object))
    ctx = SimpleNamespace(user_id="user", workspace_id="workspace", session_id="session")
    shot = recipe.shots(recipe.Plan().localized("vi"))[1]
    media = {"width": 1344, "height": 768, "has_audio": True, "duration_sec": 5.17}
    transcript = {"sentences": [{"text": "Ba chập mà!", "from_sec": 0.7, "to_sec": 1.9}]}
    with pytest.raises(RemakeStopped, match="指定对白"):
        director.choose_cut(shot, 2, "take", media, transcript)
    cut = await director._dub(ctx, 2, shot, "take", media, transcript)
    assert cut["cues"] == [{"text": "Ba chụp mà.", "from_sec": 0.7, "to_sec": 1.9}]
    assert recipe.final_request([cut], "vi")["timeline"]["shots"][0]["dub"]["key"] == "test-voice.wav"
