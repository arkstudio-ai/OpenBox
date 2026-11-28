import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from light import reference_film, reference_quality as quality, wan_story
from light.reference_plan import ShotPlan
from tool.tool import ToolContext


@pytest.mark.parametrize("tier", ["standard", "creative", "deep", "fast"])
async def test_visual_schema_retry_uses_the_selected_model_and_keeps_the_original_request(monkeypatch, tier):
    import litellm
    from agent import llm
    from trajectory import capture as trajectory
    from core import config

    @asynccontextmanager
    async def scope(*args, **kwargs):
        yield SimpleNamespace(chunk=AsyncMock())

    monkeypatch.setattr(trajectory, "service_scope", scope)
    monkeypatch.setattr(trajectory, "capture_service_dispatch", scope)
    config_value = config.OpenBoxConfig(model_tiers={"product_chat": True})
    chosen = next(row.model for row in config_value.model_tiers.chat if row.tier == tier)
    monkeypatch.setattr(config, "get_config", lambda: config_value)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda model: {})
    monkeypatch.setattr(quality, "_check", AsyncMock())
    monkeypatch.setattr(quality, "owned_images", AsyncMock(return_value={"https://example.invalid/product": "card",
                                                                       "https://example.invalid/actual": "take"}))
    messages = []

    async def respond(**options):
        assert options["model"] == chosen
        messages.append(deepcopy(options["messages"]))
        values = product_report(confidence=1.6 if len(messages) == 1 else .95)["report"]
        choice = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(values)))
        return SimpleNamespace(choices=[choice])

    monkeypatch.setattr(litellm, "acompletion", respond)
    answer = await quality.ask(ToolContext(model_id=chosen), "review_product", quality.ProductReport, "Inspect the product.",
                               {"language": "vi"}, ["card", "card", "take"])
    assert answer["model"] == chosen
    assert answer["report"]["confidence"] == .95
    assert messages[1][0] == messages[0][0] and 'confidence' in messages[1][1]["content"]
    assert [part["image_url"]["url"] for part in messages[0][0]["content"][1:]] == [
        "https://example.invalid/product", "https://example.invalid/product", "https://example.invalid/actual"]


def fixture():
    plan = ShotPlan(
        title="这局吃虾", product="小龙虾", appearance="红壳整只小龙虾，黑色圆锅",
        setting_and_cast="两名人物，餐厅解围后次日办公室", identity_frame="两人同框",
        headline="有局有虾", shots=[
            {"scene": "餐厅", "kind": "product" if i == 8 else "detail" if i == 5 else "scene",
             "seconds": 4, "frame": "空桌" if i < 4 else "一锅小龙虾", "action": "看向同事"}
            for i in range(1, 9)
        ],
    )
    cuts = [{"shot": i, "source": f"video-{i}", "in_sec": 0, "duration_sec": 3,
             "first": 0, "last": 74, "fps": 25, "cues": [], "heard": ""} for i in range(1, 9)]
    return plan, cuts


def product_report(**updates):
    return {"report": {"subject": "product", "matches": True, "stable": True, "confidence": 0.97,
                       "observed_product": "红壳整只小龙虾", "reason": "头胸部双螯与用户原图相符",
                       "action": "keep", "correction": "", **updates},
            "evidence": ["original-photo", "actual-video-frame"], "model": "vision"}


def story_report(**updates):
    return {"report": {"coherent": True, "cast_consistent": True, "props_continuous": True,
                       "natural_actions": True, "product_consistent": True,
                       "reason": "上菜前空桌，落桌后唯一虾锅，次日回应完整", "issues": [], **updates},
            "evidence": ["actual-story-frames"], "model": "vision"}


async def test_parallel_product_reviews_checkpoint_the_finished_shot_before_retry(monkeypatch):
    plan, cuts = fixture()
    hero_saved = asyncio.Event()
    snapshots = []

    async def save(state):
        snapshots.append(deepcopy(state))
        if "video-8" in state["products"]:
            hero_saved.set()

    async def inspect(ctx, plan, cut, materials):
        if cut["shot"] == 5:
            await hero_saved.wait()
            raise quality.RemakeStopped("视觉请求超时")
        return product_report()

    story = AsyncMock(return_value=story_report())
    monkeypatch.setattr(quality, "product", inspect)
    monkeypatch.setattr(quality, "story", story)
    with pytest.raises(quality.RemakeStopped, match="视觉请求超时"):
        await asyncio.wait_for(quality.finish(None, plan, cuts, ["photo"], "cast", None, save, AsyncMock()), 2)
    story.assert_not_awaited()
    assert list(snapshots[-1]["products"]) == ["video-8"]
    inspect = AsyncMock(return_value=product_report())
    monkeypatch.setattr(quality, "product", inspect)
    state = await quality.finish(None, plan, cuts, ["photo"], "cast", snapshots[-1], save, AsyncMock())
    assert state["status"] == "passed" and state["cuts"] == cuts
    assert [call.args[2]["shot"] for call in inspect.await_args_list] == [5]
    story.assert_awaited_once()


async def test_retry_checks_the_authorized_filming_action_before_buying_another_take(monkeypatch):
    plan, cuts = fixture()
    cuts[-1]["direction"] = {"action": "无蒸汽的固定商品特写"}
    state = {"version": quality.VERSION, "input_digest": quality.fingerprint(cuts),
             "status": "reviewing", "cuts": list(cuts), "omissions": [], "actions": [], "story_history": [],
             "uses_filming_intent": True, "products": {"video-5": product_report(), "video-8": product_report(matches=False, action="retake")}}
    monkeypatch.setattr(quality, "sample", AsyncMock(return_value=[(12, "observed-frame")]))
    ask = AsyncMock(return_value=product_report())
    monkeypatch.setattr(quality, "ask", ask)
    monkeypatch.setattr(quality, "story", AsyncMock(return_value=story_report()))
    repair = AsyncMock()
    result = await quality.finish(None, plan, cuts, ["photo"], "cast", state, AsyncMock(), repair)
    ask.assert_awaited_once()
    data, images = ask.await_args.args[-2:]
    assert data["approved_filming_action"] == cuts[-1]["direction"]["action"]
    assert images == ["photo", "observed-frame"]
    assert result["status"] == "passed"
    repair.assert_not_awaited()


async def test_story_keeps_frame_order_when_shots_finish_out_of_order(monkeypatch):
    import io
    from PIL import Image

    plan, cuts = fixture()
    second_ready = asyncio.Event()
    frames_read, boards = [], []

    async def frames(ctx, client, cut, positions, width):
        i = cut["shot"]
        if i == 1:
            await second_ready.wait()
        elif i == 2:
            second_ready.set()
        assert positions == (0.07, 0.5, 0.93) and width == 270
        frames_read.append(i)
        result = []
        for col in range(3):
            buf = io.BytesIO()
            Image.new("L", (2, 2), i * 20 + col * 3).save(buf, "PNG")
            result.append((col, buf.getvalue()))
        return result

    async def record(ctx, key, data):
        assert key.startswith("story-quality-")
        boards.append(Image.open(io.BytesIO(data)).convert("L"))
        return key

    ask = AsyncMock(return_value=story_report())
    monkeypatch.setattr(quality, "_sample_frames", frames)
    monkeypatch.setattr(quality, "_reference_asset", record)
    monkeypatch.setattr(quality, "ask", ask)
    await asyncio.wait_for(quality.story(None, plan, cuts, ["product"], "cast"), 3)
    assert frames_read.index(2) < frames_read.index(1) and len(boards) == 3
    for row, cut in enumerate(cuts):
        for col in range(3):
            pixel = boards[row // 3].getpixel((col * 270 + 135, row % 3 * 506 + 200))
            assert abs(pixel - (20 * cut["shot"] + 3 * col)) <= 1
    evidence = ask.await_args.args[-1]
    assert evidence[:2] == ["cast", "product"] and len(evidence) == 5


async def test_uncertain_optional_macro_is_omitted_without_touching_story_or_reusing_a_human_cut(monkeypatch):
    plan, cuts = fixture()

    async def product(ctx, plan, cut, materials):
        return product_report(confidence=0.66, action="omit", reason="剥壳肉形态像大虾，无法确认小龙虾") if cut["shot"] == 5 else product_report()

    monkeypatch.setattr(quality, "product", product)
    story = AsyncMock(return_value=story_report())
    monkeypatch.setattr(quality, "story", story)
    save, repair = AsyncMock(), AsyncMock()
    result = await quality.finish(None, plan, cuts, ["original"], "anchor", None, save, repair)
    assert result["status"] == "passed"
    assert [c["shot"] for c in result["cuts"]] == [1, 2, 3, 4, 6, 7, 8]
    assert result["omissions"][0]["source"] == "video-5"
    assert result["actions"][0]["action"] == "omit"
    assert result["products"]["video-5"]["report"]["confidence"] == 0.66
    repair.assert_not_awaited()
    assert story.await_args.args[2] == result["cuts"]
    # Recovery uses the signed-off review, not another paid shoot or a lost omission.
    assert await quality.finish(None, plan, cuts, ["original"], "anchor", result, save, repair) == result
    story.assert_awaited_once()


async def test_low_confidence_keep_is_not_a_pass_and_the_hero_cannot_be_omitted(monkeypatch):
    plan, cuts = fixture()

    async def product(ctx, plan, cut, materials):
        if cut["source"] == "video-8":
            return product_report(confidence=0.66, action="omit", reason="片尾商品无法确认")
        if cut["source"] == "video-5":
            return product_report(confidence=0.66, action="keep", reason="可能是小龙虾但不确定")
        return product_report()

    async def repair(index, feedback, attempt):
        return {**cuts[index - 1], "source": f"fixed-{index}"}

    monkeypatch.setattr(quality, "product", product)
    monkeypatch.setattr(quality, "story", AsyncMock(return_value=story_report()))
    repair_mock = AsyncMock(side_effect=repair)
    result = await quality.finish(None, plan, cuts, ["original"], "anchor", None, AsyncMock(), repair_mock)
    assert result["status"] == "passed" and not result["omissions"]
    assert [call.args[0] for call in repair_mock.await_args_list] == [5, 8]
    assert all(action["action"] == "retake" for action in result["actions"])


async def test_a_phone_story_insert_does_not_require_premature_product_placement(monkeypatch):
    plan, cuts = fixture()
    plan.shots[4].frame = "上菜前空桌上的手机和酒杯，群聊气泡"
    plan.shots[4].action = "手机收到一条新消息，不出现商品"

    async def product(ctx, plan, cut, materials):
        return product_report(subject="story_prop", observed_product="手机与啤酒", reason="按剧本此时还未上菜") if cut["shot"] == 5 else product_report()

    monkeypatch.setattr(quality, "product", product)
    monkeypatch.setattr(quality, "story", AsyncMock(return_value=story_report()))
    repair = AsyncMock()
    result = await quality.finish(None, plan, cuts, ["original"], "anchor", None, AsyncMock(), repair)
    assert result["status"] == "passed" and result["cuts"] == cuts and not result["actions"]
    repair.assert_not_awaited()
    report = quality.ProductReport.model_validate(product_report(subject="story_prop")["report"])
    assert not report.accepts_shot(plan.shots[-1])


async def test_cross_shot_continuity_failure_triggers_a_retake_then_a_new_whole_film_review(monkeypatch):
    plan, cuts = fixture()
    monkeypatch.setattr(quality, "product", AsyncMock(return_value=product_report()))
    review = AsyncMock(side_effect=[
        story_report(props_continuous=False, issues=[{"shot": 3, "action": "omit", "reason": "上菜前出现第二锅", "correction": "空桌，唯一锅仍在女主管手中"}]),
        story_report(),
    ])
    monkeypatch.setattr(quality, "story", review)
    repair = AsyncMock(return_value={**cuts[2], "source": "fixed-3"})
    result = await quality.finish(None, plan, cuts, ["original"], "anchor", None, AsyncMock(), repair)
    assert result["status"] == "passed" and not result["omissions"]
    repair.assert_awaited_once()
    assert result["cuts"][2]["source"] == "fixed-3" and review.await_count == 2


async def test_identical_selected_cuts_resume_the_pending_story_correction(monkeypatch):
    plan, cuts = fixture()
    issue = {"shot": 7, "action": "retake", "reason": "视线转开", "correction": "保持交流方向"}
    story = AsyncMock(side_effect=[story_report(natural_actions=False, issues=[issue]), story_report()])
    monkeypatch.setattr(quality, "story", story)
    monkeypatch.setattr(quality, "product", AsyncMock(return_value=product_report()))
    snapshots = []
    save = AsyncMock(side_effect=lambda state: snapshots.append(deepcopy(state)))
    with pytest.raises(quality.RemakeStopped):
        await quality.finish(None, plan, cuts, ["photo"], "cast", None, save,
                             AsyncMock(side_effect=quality.RemakeStopped("修正暂未交付")))
    repair = AsyncMock(return_value={**cuts[6], "source": "fixed-7"})
    result = await quality.finish(None, plan, cuts, ["photo"], "cast", snapshots[-1], save, repair)
    assert result["status"] == "passed" and len(result["story_history"]) == 2
    assert story.await_count == 2 and repair.await_count == 1


def test_matching_setup_only_uses_a_framed_take_of_the_same_speaker_in_the_same_scene():
    plan, cuts = fixture()
    for i, scene, speaker in [(1, "夜餐厅", "同事"), (5, "次日办公室", "同事"),
                              (6, "次日办公室", "主管"), (7, "次日办公室", "同事")]:
        plan.shots[i - 1] = plan.shots[i - 1].model_copy(update={"scene": scene, "kind": "scene", "speaker": speaker})
        cuts[i - 1]["first_reference"] = f"frame-{i}"
    assert quality.matching_setup(plan, cuts, 7) == cuts[4]
    assert quality.matching_setup(plan, cuts, 6) is None
    assert quality.matching_setup(plan, cuts, 8) is None


async def test_repeated_quality_failure_stops_at_two_repairs(monkeypatch):
    plan, cuts = fixture()
    monkeypatch.setattr(quality, "product", AsyncMock(return_value=product_report(matches=False, action="retake")))
    async def try_repair(index, feedback, attempt, *, existing_only=False):
        if existing_only:
            raise quality.RemakeStopped("已有素材也无法剪成合格镜头")
        return {**cuts[index-1], "source": f"fixed-{index}-{attempt}"}
    repair = AsyncMock(side_effect=try_repair)
    with pytest.raises(quality.RemakeStopped, match="已有素材"):
        await quality.finish(None, plan, cuts, ["original"], "anchor", None, AsyncMock(), repair)
    assert all(call.args[2] <= 2 or call.kwargs.get("existing_only") for call in repair.await_args_list)


async def test_complete_cut_requires_post_composition_review_and_cannot_hide_missing_story(monkeypatch):
    plan, cuts = fixture()
    monkeypatch.setattr(quality, "product", AsyncMock(return_value=product_report()))
    monkeypatch.setattr(quality, "story", AsyncMock(return_value=story_report()))
    state = await quality.finish(None, plan, cuts, ["original"], "anchor", None, AsyncMock(), AsyncMock())
    timeline = reference_film.timeline_for(state["cuts"], "art")
    run = {"wan_cuts": cuts, "wan_quality": state, "wan_final_key": "final", "remake_adaptation": plan.model_dump()}
    job = SimpleNamespace(kind="compose", status="completed", idempotency_key="final", output_asset_id="movie",
                          request_data={"timeline": timeline, "duration_sec": 24})
    assert wan_story.is_complete_cut(run, job, require_delivery_review=False)
    assert not wan_story.is_complete_cut(run, job)
    state["final"] = {"output": "movie", "dialogue_matches": True, "report": {
        "story_visible": True, "product_visible": True, "title_readable": True,
        "no_visual_defect": True, "reason": "完整剧情、商品和题字均清晰",
    }}
    assert wan_story.is_complete_cut(run, job)
    bad = deepcopy(run)
    bad["wan_quality"]["cuts"] = bad["wan_quality"]["cuts"][1:]
    bad["wan_quality"]["omissions"] = [{"shot": 1, "reason": "删去不佳画面", "evidence": ["frame"]}]
    assert quality.delivery_cuts(bad) is None
    bad = deepcopy(run)
    bad["wan_quality"]["products"]["video-8"]["report"]["confidence"] = 0.66
    assert quality.delivery_cuts(bad) is None
    bad = deepcopy(job)
    bad.request_data["timeline"]["shots"] = timeline["shots"][-1:]
    bad.request_data["duration_sec"] = 3
    assert not wan_story.is_complete_cut(run, bad)


def test_no_omission_may_truncate_dialogue_or_leave_a_short_film():
    plan, cuts = fixture()
    bad = deepcopy(cuts)
    bad[0]["cues"] = [{"text": "我请", "from_sec": 2, "to_sec": 3.5}]
    with pytest.raises(ValueError, match="truncates"):
        quality.validate_selection(plan, cuts, bad, [])
    with pytest.raises(ValueError):
        quality.validate_selection(plan, cuts, cuts[-1:], [])


def test_promotional_lettering_stays_readable_on_light_and_dark_hero_backgrounds():
    from PIL import Image
    import io

    light = quality.lettering_palette([Image.new("RGB", (540, 960), (212, 204, 199))])
    dark = quality.lettering_palette([Image.new("RGB", (540, 960), (25, 32, 30))])
    assert light["ink"] == "#3B2618" and light["minimum_contrast"] > 8
    assert dark["ink"] == "#FFF8E9" and dark["minimum_contrast"] > 8
    # Both the four-character layout and a shorter promotional line accept the palette.
    for title in ("有局有虾", "趁热吃"):
        png = reference_film.lettering(title, "巷口小龙虾", ink=light["ink"], outline=light["outline"])
        assert Image.open(io.BytesIO(png)).mode == "RGBA"


async def test_first_frame_repair_can_fix_a_second_defect_without_approving_the_failed_image(monkeypatch):
    from light import reference_direction as direction

    plan, _ = fixture()
    failed = direction.Direction(frame_ok=False, reason="重复人物", correction="删除多出的前景人物",
                                 speaker="", action="抬眼", seconds=4)
    passed = failed.model_copy(update={"frame_ok": True, "reason": "仅有指定人物", "correction": ""})
    inspect = AsyncMock(side_effect=[failed, failed, passed])
    monkeypatch.setattr(direction, "bind", inspect)
    regenerate = AsyncMock(side_effect=["repair-one", "repair-two"])
    image, _ = await direction.resolve(None, plan, 1, "original", "anchor", regenerate)
    assert image == "repair-two"
    assert [c.args[3] for c in inspect.await_args_list] == ["original", "repair-one", "repair-two"]
    assert [c.args[:2] for c in regenerate.await_args_list] == [(1, "original"), (2, "repair-one")]
    monkeypatch.setattr(direction, "bind", AsyncMock(return_value=failed))
    regenerate = AsyncMock(return_value="still-wrong")
    with pytest.raises(quality.RemakeStopped, match="自动修正3次"):
        await direction.resolve(None, plan, 1, "original", "anchor", regenerate)
    assert regenerate.await_count == 3


async def test_failed_filming_stops_queued_submissions_but_settles_inflight_work():
    import asyncio
    from light.remake import _parallel

    submitted, settled = [], []
    inflight = asyncio.Event()

    async def operation(index):
        submitted.append(index)
        if index == 1:
            await inflight.wait()
            raise quality.RemakeStopped("bad first frame")
        inflight.set()
        await asyncio.sleep(0.01)
        settled.append(index)
        return index

    with pytest.raises(quality.RemakeStopped, match="bad first frame"):
        await _parallel(range(1, 7), operation, concurrency=2, stop_on_error=True)
    assert submitted == [1, 2] and settled == [2]

async def test_first_pass_omission_and_free_reordering_are_reviewed_in_actual_order(monkeypatch):
    plan, cuts = fixture()
    cuts[4].update(omit_reason="这个动作与上一个镜头重复", omit_evidence=["actual"])
    order = [2, 1, 3, 4, 6, 7, 8]
    reviewer = AsyncMock(side_effect=[story_report(order=order), story_report()])
    monkeypatch.setattr(quality, "story", reviewer)
    monkeypatch.setattr(quality, "product", AsyncMock(return_value=product_report()))
    repair = AsyncMock()
    state = await quality.finish(None, plan, cuts, ["photo"], "anchor", None, AsyncMock(), repair)
    repair.assert_not_awaited()
    assert [c["shot"] for c in state["cuts"]] == order
    assert [a["action"] for a in state["actions"]] == ["omit", "reorder"]
    assert reviewer.await_count == 2
    timeline = reference_film.timeline_for(state["cuts"], "title")
    assert [s["asset"] for s in timeline["shots"]] == [f"video-{i}" for i in order]

async def test_crop_and_revised_staging_are_rechecked_against_the_directors_new_plan(monkeypatch):
    from light import reference_direction as director
    plan, _ = fixture()
    setup = director.Coverage(frame="抬高取景，人物肩部以上", action="两人抬眼相视")
    first = director.Direction(frame_ok=False, reason="屏幕在下方入镜", correction="上移取景",
                               repair_mode="crop", crop={"left": .15, "top": 0, "size": .7}, coverage=setup,
                               speaker="", action="相视", seconds=4)
    good = first.model_copy(update={"frame_ok": True, "coverage": None})
    inspect = AsyncMock(side_effect=[first, good])
    monkeypatch.setattr(director, "bind", inspect)
    crop = AsyncMock(return_value="cropped-owned-frame")
    monkeypatch.setattr(director, "crop_frame", crop)
    regenerate = AsyncMock()
    image, result = await director.resolve(None, plan, 1, "frame", "anchor", regenerate)
    assert image == "cropped-owned-frame" and result.coverage == setup
    assert inspect.await_args.args[1].shots[0].action == setup.action
    crop.assert_awaited_once()
    regenerate.assert_not_awaited()


async def test_terminal_editor_can_drop_optional_insert_after_inspecting_new_camera(monkeypatch):
    plan, cuts = fixture()
    async def inspect(ctx, plan, cut, materials):
        return product_report(matches=False, action="retake") if cut["shot"] == 5 else product_report()
    monkeypatch.setattr(quality, "product", inspect)
    monkeypatch.setattr(quality, "story", AsyncMock(return_value=story_report()))
    repair = AsyncMock(return_value={**cuts[4], "omit_reason": "换机位仍然穿帮，省略不影响剧情",
                                     "omit_evidence": ["examined-new-frame"]})
    result = await quality.finish(None, plan, cuts, ["original"], "anchor", None, AsyncMock(), repair)
    assert result["status"] == "passed" and result["omissions"][0]["evidence"] == ["examined-new-frame"]

async def test_director_can_extend_real_silent_product_interval_without_another_generation(monkeypatch):
    from light.reference_direction import Edit
    plan, cuts = fixture()
    decision = Edit(action="retime", reason="店名需要多留一秒", instruction="延长已有真实商品镜", output_seconds=4)
    cut = await reference_film.time_cut(None, plan.shots[-1], cuts[-1], decision)
    assert cut["source"] == cuts[-1]["source"] and cut["speed"] == .75
    changed = [*cuts[:-1], cut]
    quality.validate_selection(plan, cuts, changed, [])
    timeline = reference_film.timeline_for(changed, "title")
    assert timeline["shots"][-1]["duration_sec"] == 3 and timeline["shots"][-1]["speed"] == .75
    assert timeline["overlays"][-1]["to_sec"] == 25
    with pytest.raises(ValueError):
        await reference_film.time_cut(None, plan.shots[-1], {**cuts[-1], "cues": [{"text": "不能慢放对白"}]}, decision)


def test_product_hold_sits_under_actual_brand_title_and_uses_original_footage():
    from video.timeline import Timeline
    plan, cuts = fixture()
    held = {**cuts[-1], "hold_asset": "printed-frame", "hold_frame": 32, "source_duration_sec": 3,
            "duration_sec": 4, "speed": .75, "mute_audio": True}
    quality.validate_selection(plan, cuts, [*cuts[:-1], held], [])
    timeline = Timeline.model_validate(reference_film.timeline_for([*cuts[:-1], held], "headline"))
    assert timeline.shots[-1].timeline_sec == 4
    assert timeline.shots[-1].asset == "video-8"
    assert [layer.asset for layer in timeline.overlays] == ["printed-frame", "headline"]
    assert timeline.overlays[0].from_sec == 21 and timeline.overlays[0].to_sec == 25
