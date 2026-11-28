import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from light import reference_film as film
from light import wan_story
from light.reference_plan import ShotPlan


def plan_data(count=8):
    return {
        "format": "shot-list-v2",
        "title": "这局吃虾",
        "product": "小龙虾",
        "appearance": "红油黑锅",
        "setting_and_cast": "男右女左，暖餐厅与冷办公室",
        "identity_frame": "餐厅双人空桌",
        "headline": "有局有虾",
        "shots": [
            {
                "scene": "餐厅",
                "kind": "product" if i == count - 1 else "scene",
                "seconds": 4,
                "frame": "空桌前的两人" if i < count - 1 else "独立一锅虾，无人",
                "action": "抬眼看对方" if i < count - 1 else "薄热气升起",
                "speaker": "",
                "line": "",
            }
            for i in range(count)
        ],
    }


def test_short_shot_plan_is_variable_and_cannot_be_only_an_end_card():
    for count in (5, 8, 12):
        plan = ShotPlan.model_validate(plan_data(count))
        assert wan_story.shot_count({"remake_adaptation": plan.model_dump()}) == count
        assert isinstance(wan_story.parse_plan(plan.model_dump()), ShotPlan)
    with pytest.raises(ValidationError):
        ShotPlan.model_validate(plan_data(1))
    bad = plan_data()
    bad["shots"][-1]["line"] = "片尾文字"
    with pytest.raises(ValidationError):
        ShotPlan.model_validate(bad)


async def test_shots_overlap_keep_scene_references_and_resume_out_of_order(monkeypatch):
    from light import production, reference_direction, shot_editor

    data = plan_data(5)
    for shot, scene in zip(data["shots"], ["餐厅", "餐厅", "办公室", "办公室", "摄影棚"]):
        shot["scene"] = scene
    plan = ShotPlan.model_validate(data)
    other_scene_started, reviewed_three, saved_three = (asyncio.Event() for _ in range(3))
    inputs, jobs, saved = {}, [], []
    run = {"job_prefix": "test", "wan_cuts": [{"shot": 4, "source": "already-approved"}]}

    async def still(ctx, prefix, name, prompt, refs):
        inputs[name] = refs
        if name == "frame-v2-1":
            await other_scene_started.wait()
        if name == "frame-v2-3":
            other_scene_started.set()
        if name == "frame-v2-2":
            # Shot 3 must be filmed and reviewed even while this still is pending.
            await reviewed_three.wait()
        return name

    async def direct(ctx, plan, index, first, anchor, regenerate, feedback, max_repairs=3, *, reference_copy):
        assert reference_copy == "用户的文案"
        if index == 1:
            first = await regenerate(1, first, "纠正眼神")
        if index == 5:
            first = await regenerate(1, first, "纠正商品摆放")
            first = await regenerate(2, first, "重新构图", "reframe")
        return first, plan.shots[index - 1]

    async def film_job(ctx, request, **kwargs):
        jobs.append(request)
        if request.shot == 1:
            await saved_three.wait()
        return request.idempotency_key

    async def review(ctx, plan, index, asset, first, copy, anchor, direction):
        assert copy == "用户的文案"
        if index == 3 and ":director:" not in asset:
            reviewed_three.set()
            return None, "重拍动作"
        return {"shot": index, "source": asset, "review_version": film.REVIEW_VERSION}, ""

    async def checkpoint(ctx, prefix, cut):
        saved.append(cut["shot"])
        if cut["shot"] == 3:
            saved_three.set()

    monkeypatch.setattr(film, "paid_takes", AsyncMock(return_value={}))
    choose = AsyncMock(return_value={"report": {"action": "reshoot", "reason": "动作断开",
                                               "instruction": "抬眼后再停住"}, "evidence": ["frame"]})
    monkeypatch.setattr(reference_direction, "choose_edit", choose)
    monkeypatch.setattr(production, "save_director_decision", AsyncMock(side_effect=lambda *args: args[-1]))
    stage = AsyncMock()
    monkeypatch.setattr(film, "_still", still)
    monkeypatch.setattr(film, "_existing_job", AsyncMock(return_value=None))
    monkeypatch.setattr(film, "_job", film_job)
    monkeypatch.setattr(film, "review", review)
    monkeypatch.setattr(reference_direction, "resolve", direct)
    monkeypatch.setattr(production, "remake_stage", stage)
    monkeypatch.setattr(production, "save_reference_cut", checkpoint)
    prepared = AsyncMock()
    monkeypatch.setattr(shot_editor, "publish_request", prepared)
    _, cuts = await asyncio.wait_for(film._reviewed_takes(
        None, {"reference_copy": "用户的文案"}, run, plan, ["商品"], "演员"), timeout=3)

    assert inputs["frame-v2-2"] == ["演员", "frame-v2-1", "商品"]
    assert inputs["frame-v2-4"] == ["演员", "frame-v2-3", "商品"]
    assert inputs["frame-v2-3"] == ["演员", "商品"]
    assert any(call.args[2].input_assets[0].asset_id == "frame-v2-3" for call in prepared.await_args_list)
    assert inputs["frame-cast-v3-5-repair-first-1-edit"] == ["frame-v2-5", "商品"]
    assert inputs["frame-cast-v3-5-repair-first-2-reframe"] == ["商品"]
    assert next(r for r in jobs if r.shot == 1).input_assets[0].asset_id.endswith("repair-first-1-edit")
    assert sorted(r.shot for r in jobs) == [1, 2, 3, 3, 5]
    assert saved.index(3) < saved.index(1) and 4 not in saved
    assert [c["shot"] for c in cuts] == [1, 2, 3, 4, 5]
    assert cuts[2]["take"] == 2 and cuts[3] == run["wan_cuts"][0]
    assert [call.args[2] for call in stage.await_args_list] == ["wan_shots", "wan_edit"]


@pytest.mark.parametrize("action", ["recut", "reframe", "reshoot", "omit"])
async def test_first_defect_executes_directors_choice_and_replays_without_replanning(monkeypatch, action):
    from light import production, reference_direction

    plan = ShotPlan.model_validate(plan_data())
    index = 3
    plan.shots[index-1].optional = True
    coverage = {"frame": "单人侧面近景", "action": "看向对面朋友"}
    old = SimpleNamespace(output_asset_id="owned-take", request_data={"roles": ["first_frame"], "input_asset_ids": ["owned-still"]})
    def previous(ctx, kind, key):
        return old if key == film._take_key("test", index, 2) else None
    monkeypatch.setattr(film, "_existing_job", AsyncMock(side_effect=previous))
    monkeypatch.setattr(film, "paid_takes", AsyncMock(return_value={"owned-take": {"source": "owned-take", "first_reference": "owned-still"}}))
    report = {"action": action, "instruction": "保留人物视线", "reason": "边缘多出一只手"}
    if action == "reframe":
        report["coverage"] = coverage
    if action == "recut":
        report.update(take="owned-take", first=5, last=75)
    choose = AsyncMock(return_value={"report": report, "evidence": ["actual-board"]})
    monkeypatch.setattr(reference_direction, "choose_edit", choose)
    monkeypatch.setattr(production, "save_director_decision", AsyncMock(side_effect=lambda *args: args[-1]))
    job = AsyncMock(return_value="new-video")
    monkeypatch.setattr(film, "_job", job)
    monkeypatch.setattr(film, "_still", AsyncMock(return_value="new-setup"))
    direct = AsyncMock(side_effect=lambda ctx, plan, idx, first, *a, **k: (first, plan.shots[idx-1]))
    monkeypatch.setattr(reference_direction, "resolve", direct)
    review = AsyncMock(side_effect=lambda ctx, plan, idx, source, first, *a, **k: ({"shot": idx, "source": source}, ""))
    monkeypatch.setattr(film, "review", review)
    run = {"job_prefix": "test"}
    original = {"source": "original", "first_reference": "original-frame", "shot": index}
    for _ in range(2):
        result = await film._repair_take(None, {"reference_copy": "自选剧情"}, run, plan, ["photo"], "cast",
                                         index, dict(original), {"reason": "实际多出一只手", "review_asset": "actual-board"},
                                         [], "shoot", can_omit=True)
        assert result["director_action"] == action
    choose.assert_awaited_once()
    args = choose.await_args.args
    assert args[3] == "自选剧情" and "owned-take" in args[7] and args[6]["review_asset"] == "actual-board"
    if action in {"recut", "omit"}:
        job.assert_not_awaited()
    else:
        assert len({call.args[1].idempotency_key for call in job.await_args_list}) == 1
    if action == "recut":
        assert result["source"] == "owned-take" and review.await_args.kwargs["edit"].first == 5
    if action == "reframe":
        assert result["coverage"] == coverage and direct.await_args.args[3] == "new-setup"


async def test_shoot_failure_drains_submitted_jobs_and_stops_new_submissions(monkeypatch):
    from light import production, reference_direction, shot_editor

    plan = ShotPlan.model_validate(plan_data(8))
    four_started, failed = asyncio.Event(), asyncio.Event()
    started, drained = [], []

    async def film_job(ctx, request, **kwargs):
        started.append(request.shot)
        if len(started) == 4:
            four_started.set()
        if request.shot == 1:
            await four_started.wait()
            failed.set()
            raise film.RemakeStopped("拍摄中断")
        await failed.wait()
        drained.append(request.shot)
        return request.idempotency_key

    monkeypatch.setattr(film, "_still", AsyncMock(return_value="frame"))
    monkeypatch.setattr(film, "_existing_job", AsyncMock(return_value=None))
    monkeypatch.setattr(reference_direction, "resolve", AsyncMock(return_value=("frame", plan.shots[0])))
    monkeypatch.setattr(film, "_job", film_job)
    monkeypatch.setattr(film, "review", AsyncMock())
    monkeypatch.setattr(production, "remake_stage", AsyncMock())
    monkeypatch.setattr(shot_editor, "publish_request", AsyncMock())
    with pytest.raises(film.RemakeStopped, match="拍摄中断"):
        await asyncio.wait_for(film._reviewed_takes(
            None, {}, {"job_prefix": "test"}, plan, ["product"], "cast"), timeout=3)
    assert sorted(started) == [1, 2, 3, 4] and sorted(drained) == [2, 3, 4]
    film.review.assert_not_awaited()


def test_edit_window_excludes_extra_words_without_truncating_the_intended_line():
    transcript = {
        "text": "领导昨晚那顿请到",
        "words": [
            {"text": "领导", "from_sec": 1.8, "to_sec": 2.52},
            {"text": "昨晚那顿", "from_sec": 3.08, "to_sec": 4.24},
            {"text": "请到", "from_sec": 4.44, "to_sec": 4.88},
        ],
    }
    cues, left, right = film.speech_window("领导，昨晚那顿……", transcript, 5)
    assert cues[0]["from_sec"] == 1.8 and cues[0]["to_sec"] == 4.24
    assert left == 0 and 4.24 < right < 4.44
    assert film.speech_window("今晚，还想去？", transcript, 5) is None
    assert film.speech_window("", transcript, 5) is None
    assert (
        film.speech_window(
            "我请", {"words": [{"text": "我请客", "from_sec": 1, "to_sec": 2}]}, 4
        )
        is None
    )


def test_request_uses_each_shots_duration_and_keeps_title_out_of_dialogue():
    plan = ShotPlan.model_validate(plan_data())
    shot = plan.shots[0].model_copy(
        update={"seconds": 6, "speaker": "女主管", "line": "这局，吃虾。"}
    )
    req = film.shot_request("light:test", 1, shot, "first")
    assert (
        req.duration == 6
        and req.spoken_line == shot.line
        and req.model == "wan3.0-video"
    )
    assert plan.headline not in req.prompt
    assert film.shot_request("light:test", 8, plan.shots[-1], "hero").no_speech
    assert not film.shot_request("light:test", 8, plan.shots[-1], "hero").generate_audio
    assert (
        "自然开始对白"
        not in film.shot_request("light:test", 8, plan.shots[-1], "hero").prompt
    )


def test_grounded_take_names_visible_speaker_and_reserves_natural_speech_time():
    from light.reference_direction import Direction

    plan = ShotPlan.model_validate(plan_data())
    shot = plan.shots[0].model_copy(
        update={"speaker": "组长", "line": "菜还没上，先干三杯？", "seconds": 3}
    )
    direction = Direction(
        frame_ok=True,
        reason="说话人清晰可见",
        speaker="近景中穿灰西装的中年男子",
        action="灰西装男子抬杯看向右侧的男同事；男同事闭口，双手留在桌上。",
        seconds=5,
    )
    request = film.shot_request("light:test", 1, shot, "checked-frame", 2, direction)
    assert request.duration == 5 and request.generate_audio
    assert "只有近景中穿灰西装的中年男子说" in request.prompt
    assert "只有组长说" not in request.prompt
    assert request.idempotency_key.endswith(":retake:cast-v3")
    assert request.input_assets[0].asset_id == "checked-frame"


def test_render_button_cannot_advertise_or_accept_a_new_screenplay():
    from light import production_form
    from tool.light_remake import light_wan_story_tool

    session = SimpleNamespace(
        light_entry="video", light_production={"template": "wan_story"}
    )
    tools = {"light_wan_story": light_wan_story_tool}
    render = production_form.restrict_guided_tools(tools, session, "render")[
        "light_wan_story"
    ]
    assert render.parameters.model_validate({}).action == "render"
    assert "plan" not in render.parameters.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        render.parameters.model_validate({"action": "prepare", "plan": plan_data()})
    prepare = production_form.restrict_guided_tools(tools, session, "prepare")[
        "light_wan_story"
    ]
    assert prepare.parameters.model_validate({"plan": plan_data()}).action == "prepare"
    # Per-turn narrowing must not mutate the shared registry definition.
    assert "plan" in light_wan_story_tool.parameters.model_json_schema()["properties"]


def test_only_executed_owned_action_receipt_settles_the_guided_turn():
    from light.production_form import finished_guided_action
    from agent.processor import persisted_tool_metadata

    result = SimpleNamespace(canonical_tool_id="light_wan_story", status="error",
                             metadata={"guided_action_finished": "render", "error": True})
    result.metadata = persisted_tool_metadata(result.metadata)
    assert finished_guided_action("render", [result]) is result
    assert finished_guided_action("prepare", [result]) is None
    assert finished_guided_action(None, [result]) is None
    result.canonical_tool_id = "mcp:light_wan_story"
    assert finished_guided_action("render", [result]) is None
    result.canonical_tool_id = "light_wan_story"
    result.metadata = {"error_code": "invalid_arguments"}
    assert finished_guided_action("render", [result]) is None


def test_complete_cut_requires_every_approved_shot_and_exact_edit():
    cuts = [
        {
            "shot": i,
            "source": f"a{i}",
            "in_sec": 0,
            "duration_sec": 3,
            "cues": [],
            "heard": "",
        }
        for i in range(1, 9)
    ]
    timeline = film.timeline_for(cuts, "lettering")
    run = {"wan_cuts": cuts, "wan_final_key": "final", "remake_adaptation": plan_data(),
           "soda_visual_review": {"reviewed": True}, "voice_id": "voice:legacy"}
    job = SimpleNamespace(
        idempotency_key="final",
        kind="compose",
        status="completed",
        output_asset_id="movie",
        request_data={"timeline": timeline, "duration_sec": 24},
    )
    assert wan_story.is_complete_cut(run, job)
    for wrong in (
        [timeline["shots"][-1]],
        timeline["shots"][:-1],
        list(reversed(timeline["shots"])),
    ):
        broken = deepcopy(job)
        broken.request_data["timeline"]["shots"] = wrong
        assert not wan_story.is_complete_cut(run, broken)
    broken = deepcopy(job)
    broken.request_data["duration_sec"] = 4
    assert not wan_story.is_complete_cut(run, broken)
    with pytest.raises(film.RemakeStopped):
        film.timeline_for(cuts[-1:], "lettering")
    cuts[-1]["mute_audio"] = True
    assert film.timeline_for(cuts, "lettering")["shots"][-1]["volume"] == 0
    from video.timeline import Timeline
    scored = Timeline.model_validate(film.timeline_for(cuts, "lettering", music_start=12))
    assert scored.audio[0].from_sec == 12 and scored.audio[0].to_sec == 24
    assert scored.audio[0].duck_under_captions


def test_transcription_tool_returns_measured_word_times_to_the_agent():
    from tool.video_production import _transcription_lines

    measured = [{"text": "我请", "from_sec": 1.76, "to_sec": 2.28}]
    job = SimpleNamespace(
        id="asr",
        status="completed",
        request_data={},
        error=None,
        result_data={"transcript": {"text": "我请", "words": measured}},
    )
    import json

    output = dict(x.split("=", 1) for x in _transcription_lines(job))
    assert json.loads(output["words"]) == measured


async def test_review_cannot_accept_correct_words_from_the_wrong_actor(monkeypatch):
    import json
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    import litellm

    from trajectory import capture as trajectory
    from light import wan_edit

    @asynccontextmanager
    async def scope(*args, **kwargs):
        yield SimpleNamespace(chunk=AsyncMock())

    async def completion(**opts):
        schema = opts["tools"][0]["function"]["parameters"]
        assert {"speaker_matches", "cast_matches", "props_match"} <= set(
            schema["required"]
        )
        assert len(opts["messages"][0]["content"]) == 3
        answer = {
            "first": 0,
            "last": 89,
            "reason": "实际是白衬衫男子说话，应是灰西装男子",
            "continuity": "人物抢了台词",
            "retake": False,
            "speaker_matches": False,
            "cast_matches": True,
            "props_match": True,
        }
        call = SimpleNamespace(
            function=SimpleNamespace(name="choose_cut", arguments=json.dumps(answer))
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None, tool_calls=[call])
                )
            ]
        )

    monkeypatch.setattr(wan_edit, "_check", AsyncMock())
    monkeypatch.setattr(trajectory, "service_scope", scope)
    monkeypatch.setattr(trajectory, "capture_service_dispatch", scope)
    monkeypatch.setattr(litellm, "acompletion", completion)
    result, _ = await wan_edit.decide(
        None,
        {"review_contract": "cast-v3", "expected_speaker": "灰西装男子"},
        "contact",
        "contact-asset",
        [0],
        [89],
        references={"anchor": "anchor-asset"},
    )
    assert result.retake and result.speaker_matches is False

async def test_unfilmable_new_setup_returns_control_to_director_without_spending_on_wan(monkeypatch):
    from light import production, reference_direction as director
    plan = ShotPlan.model_validate(plan_data(6))
    plan.shots[2].optional = True
    current = {"shot": 3, "source": "actual-take", "first_reference": "actual-still"}
    monkeypatch.setattr(film, "paid_takes", AsyncMock(return_value={}))
    decisions = [
        {"report": {"action": "reframe", "reason": "两端无法剪除变形", "instruction": "换侧面",
                    "coverage": {"frame": "整虾近景", "action": "轻推镜头"}}, "evidence": ["actual-board"]},
        {"report": {"action": "omit", "reason": "该插镜可省，保留上菜和下一镜试吃", "instruction": "直接接试吃"},
         "evidence": ["failed-new-frame"]},
    ]
    choose = AsyncMock(side_effect=decisions)
    monkeypatch.setattr(director, "choose_edit", choose)
    monkeypatch.setattr(director, "resolve", AsyncMock(side_effect=director.UnusableFrame("拍法无法成立", "failed-new-frame")))
    monkeypatch.setattr(production, "save_director_decision", AsyncMock(side_effect=lambda *args: args[-1]))
    monkeypatch.setattr(film, "_existing_job", AsyncMock(return_value=None))
    monkeypatch.setattr(film, "_still", AsyncMock(return_value="setup"))
    job = AsyncMock()
    monkeypatch.setattr(film, "_job", job)
    cut = await film._repair_take(None, {}, {"job_prefix": "test"}, plan, ["product"], "anchor", 3, current,
                                  {"reason": "虾提前剥开"}, [], "final-1", can_omit=True)
    assert cut["director_action"] == "omit"
    assert choose.await_args.args[-1]["rejected_frame"] == "failed-new-frame"
    assert choose.await_args.kwargs["can_film"]
    job.assert_not_awaited()
