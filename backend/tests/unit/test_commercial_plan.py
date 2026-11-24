from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from light import commercial_film, commercial_plan as recipe, production, reference_quality, remake, remake_case, wan_story
from light.mode import LightError
from light.production_form import initial_form
from tool.light_remake import wan_tool_for_action
from tool.tool import ToolContext


def treatment(**overrides):
    return dict({
        "language": "vi", "product": "Bình nước", "title": "Một nhịp mới",
        "appearance": "Bình màu cam, nắp đen có quai.", "mode": "carry", "transfer_mode":"pass",
        "product_assets": ["bottle"], "protagonist": "Một vận động viên mặc áo trắng.",
        "authority": "Người quản lý mặc áo xanh.",
        "settings": dict(home="Bếp", track="Đường chạy", street="Góc phố", office="Văn phòng"),
        "routine": "Chuẩn bị cho buổi tập.", "inserts": ["Lấy cốc", "Xoay nắp", "Rót nước", "Đặt cốc", "Cất cốc"],
        "insert_product_visible": [False, True, True, False, False],
        "insert_openings": ["Cốc còn trên bàn.", "Nắp ở sát cổ bình.", "Tay giữ bình ngay ngắn.",
                            "Cốc còn trong tay.", "Tủ vẫn mở."],
        "contact": "Chạm tay phải vào quai nắp.", "carry": "Nắm quai.", "first_use": "Nhấp một ngụm.",
        "handoff": "Đưa bình cho người quản lý.", "second_use": "Người quản lý uống một ngụm.",
        "pickup_state": "Bình dựng trên bàn.", "first_use_state": "Nắp nằm trên bàn.", "handoff_state": "Nắp vẫn nằm trên quầy.",
        "handoff_preparation": "Lấy bình ra khỏi túi.",
        "street_anchor": "Con rối hơi.", "call_line": "Tôi tới ngay.", "rebuke_line": "Sao giờ này mới tới?",
    }, **overrides)


def test_edit_keeps_the_reference_while_product_language_and_handoff_change():
    for mode, product in (("carry", "Bình nước"), ("wear", "Mũ chạy bộ"), ("stationary", "Máy pha cà phê")):
        plan = recipe.Plan(**treatment(mode=mode, product=product))
        shots = recipe.shots(plan)
        assert [s["id"] for s in shots] == list(range(1, 29))
        cuts = [sum(s["frames"] for s in shots[:n]) for n in [11, 17, 23, 27, 28]]
        assert cuts == [365, 528, 836, 1152, 1277]
        assert [s["spoken_line"] for s in shots if s["spoken_line"]] == [plan.call_line, plan.rebuke_line]
        assert shots[25]["action"] == plan.handoff and shots[-1]["scene"] == "return"
        assert plan.pickup_state in shots[1]["frame"] and plan.first_use_state not in shots[1]["end"]
        assert plan.first_use_state in shots[2]["frame"] and plan.handoff_preparation in shots[24]["action"]
        if mode != "stationary":
            assert "刚刚离开原支撑" in shots[1]["end"] and "省略" in shots[2]["frame"]
            assert shots[27]["product_visible"]
        if mode == "wear":
            assert shots[21]["product_visible"] and "仍照常穿戴同一件商品" in shots[21]["frame"]
        if mode == "carry":
            assert all("用空手起波浪" in shots[i]["action"] for i in (21, 27))
        assert plan.handoff_state in shots[25]["end"] and plan.handoff_state in shots[26]["frame"]
        assert [s["product_visible"] for s in shots[3:8]] == plan.insert_product_visible
        assert [s["frame"] for s in shots[3:8]] == plan.insert_openings
        assert shots[11]["product_visible"] is (mode != "stationary")
        script = recipe.display_script(plan)
        assert product in script and "Trở lại góc phố" in script
        assert all(not "\u4e00" <= c <= "\u9fff" for c in script)
        assert wan_story.parse_plan(plan.model_dump(), template="product_story") == plan
        assert wan_story.shot_count({"remake_adaptation": plan.model_dump()}) == 28


def test_wearable_is_fitted_on_the_manager_across_the_actual_office_cut():
    data = treatment(mode="wear", product="Mũ lưỡi trai", transfer_mode="fit")
    plan = recipe.Plan(**data)
    shots = recipe.shots(plan)
    assert "仍佩戴在自己身上" in shots[24]["end"] and "上司身体承重" in shots[25]["end"]
    assert "手退出近景" in shots[26]["action"]
    assert shots[27]["product_visible"] and "已经戴好" in shots[27]["frame"]
    old = {k: v for k, v in data.items() if k != "transfer_mode"}
    assert recipe.Plan(**old).transfer_mode == "pass"
    assert recipe.Plan(**{**old, "mode": "stationary"}).transfer_mode == "access"


def test_running_outfit_changes_at_the_two_source_cuts_and_keeps_the_product():
    data = treatment(mode="wear", product="Ba lô đỏ")
    old = recipe.Plan(**data)
    plan = old.model_copy(update={"track_wardrobe": "Áo chạy xanh than. Vẫn đeo đúng ba lô đỏ."})
    shots = recipe.shots(plan)
    assert plan.track_wardrobe in shots[11]["frame"] and "恢复之前的通勤" in shots[17]["frame"]
    assert "沿用同一套通勤" in shots[23]["frame"] and plan.track_wardrobe in recipe.display_script(plan)
    assert "恢复之前的通勤" not in recipe.shots(old)[17]["frame"]


def test_physical_directions_have_the_same_capacity_from_reading_through_storage():
    captured = recipe.Reading.model_json_schema()["properties"]
    delivery = recipe.Plan.model_json_schema()["properties"]
    for field in ("pickup_state", "first_use_state", "handoff_preparation", "handoff_state"):
        assert captured[field]["maxLength"] == delivery[field]["maxLength"] == 350
    directions = {key: "Động tác diễn ra trước quầy. " * 9 for key in (
        "pickup_state", "first_use_state", "handoff_preparation", "handoff_state")}
    payload = treatment(**directions)
    scene = recipe.Plan.model_validate(payload)
    assert all(len(getattr(scene, key)) > 180 for key in directions)


async def test_internal_schema_failure_returns_a_localized_failed_button_receipt(monkeypatch):
    from pydantic import ValidationError

    form = {"template": "product_story"}
    work = {"action": "render", "video_language": "vi", "job_prefix": "job"}
    ctx = ToolContext(user_id="owner", session_id="session", workspace_id="workspace")
    monkeypatch.setattr(production, "begin_remake", AsyncMock(return_value=(form, work, None)))
    record = AsyncMock()
    monkeypatch.setattr(production, "remake_stage", record)
    with pytest.raises(ValidationError) as bad:
        recipe.Plan.model_validate({})
    monkeypatch.setattr(recipe, "adapt", AsyncMock(side_effect=bad.value))
    guided = wan_tool_for_action("render", "product_story")
    result = await guided.execute(guided.parameters(), ctx)
    assert result.metadata["error"] and result.metadata["do_not_retry"]
    assert result.metadata["guided_action_finished"] == "render"
    assert "Tiến trình" in result.output and record.await_args.kwargs["error"] == result.output


async def test_provider_failure_uses_the_films_language_in_the_form_and_chat(monkeypatch):
    from tool.tool import ToolResult

    plan = recipe.Plan(**treatment())
    run = {"id": "item", "action": "render", "video_language": "vi", "job_prefix": "job"}
    ctx = ToolContext(user_id="owner", session_id="session", workspace_id="workspace")
    monkeypatch.setattr(production, "begin_remake", AsyncMock(return_value=({"template": "product_story"}, run, plan)))
    store = AsyncMock()
    monkeypatch.setattr(production, "remake_stage", store)
    monkeypatch.setattr(remake, "_existing_job", AsyncMock(return_value=None))
    monkeypatch.setattr(remake, "_check", AsyncMock())
    refuse = AsyncMock(return_value=ToolResult(title="validation", output="尺寸不匹配", metadata={"frame_validation": True}))
    monkeypatch.setattr(remake.video_production, "execute_generate", refuse)

    async def render(ctx, _form, run, plan):
        shot = recipe.shots(plan)[10]
        request = commercial_film.shot_request("job", plan, shot, "still", None, "", 1)
        return await remake._job(ctx, request, kind="segment", label=shot["title"], run=run)

    monkeypatch.setattr(commercial_film, "render", render)
    guided = wan_tool_for_action("render", "product_story")
    result = await guided.execute(guided.parameters(), ctx)
    assert result.metadata["error"] and result.metadata["guided_action_finished"] == "render"
    assert "Cảnh 11" in result.output and "Khung hình tham chiếu" in result.output
    assert not commercial_film._has_cjk(result.output)
    assert result.output == store.await_args.kwargs["error"]
    assert [call.args[0].action for call in refuse.await_args_list] == ["validate"]


async def test_picker_reads_all_materials_and_resolves_images_to_owned_inputs(monkeypatch):
    async def material_refs(ctx, ids, *, video_sample="opening", detail_width):
        assert detail_width == 1440
        if ids == ["bottle-video"]:
            return [f"bottle-{video_sample}"]
        return ids

    data = treatment()
    data.pop("product_assets")
    data.pop("language")
    report = {**data, "track_wardrobe": "Áo chạy xanh navy.", "product_views": [3], "obscured_product_views": [2, 3, 4],
              "protagonist_view": 1, "setting_view": 5}
    bad = {**report, "first_use": "Nhấp nước trên đường chạy."}
    change = {"slot": "first_use", "reason": "Cảnh đầu ở nhà.", "replacement": report["first_use"]}
    preparation = ("Trong lúc quản lý đứng dậy hỏi vì sao cô đến trễ, cô cầm vững thân bình màu cam ở tay trái. "
                   "Cô dùng tay phải mở nắp và đặt nó lên mép quầy. Giữ thân bình mở ở tầm tay người nhận khi máy chuyển sang cảnh tiếp theo.")
    assert 180 < len(preparation) < 350
    explain_transfer = {"slot": "handoff_preparation", "reason": "Giữ đúng trạng thái của bình.", "replacement": preparation}
    ask = AsyncMock(side_effect=[{"report": bad}, {"report": {"problems": [change, explain_transfer]}}, {"report": {"problems": []}}])
    monkeypatch.setattr(remake, "_material_refs", material_refs)
    monkeypatch.setattr(reference_quality, "ask", ask)
    form = {"asset_ids": ["athlete", "bottle-video", "corner"], "product_name": "Bình Cam", "reference_copy": "Dùng khi chạy."}
    plan = await recipe.adapt(None, form, "vi")
    assert all(call.args[5] == ["athlete", "bottle-opening", "bottle-middle", "bottle-late", "corner"]
               for call in ask.await_args_list)
    assert ask.await_args_list[2].args[4]["storyboard"]["first_use"] == report["first_use"]
    assert plan.first_use == report["first_use"] and ask.await_count == 3
    assert plan.product_assets == ["bottle-video"] and plan.product == "Bình Cam"
    assert plan.obscured_product_videos == ["bottle-video"]
    assert plan.score_revision == 2 and plan.handoff_preparation == preparation
    assert await recipe.filming_references(None, plan) == (["bottle-middle"], ["athlete"], ["corner"])
    assert plan.protagonist_asset == "athlete" and plan.setting_asset == "corner"


async def test_filming_reads_native_detail_at_distinct_actual_frames_and_preserves_old_previews(monkeypatch):
    from tool import footage_analyze
    from video import media_probe
    row = SimpleNamespace(id="clip", mime="video/mp4", meta={}, oss_key="my-input.mp4")
    ctx = ToolContext(user_id="user", session_id="session", workspace_id="workspace")
    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [row])),
                              scalar=AsyncMock(return_value=None))
    captures = []
    def snapshot(_store, _key, at, *, width, exact):
        captures.append((at, width, exact))
        return "https://test.invalid/frame"
    original_client = httpx.AsyncClient
    def client(**opts):
        return original_client(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"example", headers={"content-type":"image/jpeg"})), **opts)
    async def store(_ctx, key, _bytes):
        return key
    monkeypatch.setattr(remake, "get_db_session", connection)
    monkeypatch.setattr(remake, "_reference_asset", store)
    monkeypatch.setattr(remake, "get_oss", lambda: None)
    monkeypatch.setattr(footage_analyze, "snapshot_url", snapshot)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    probe = AsyncMock(return_value={"duration_sec":1.5, "width":1344})
    monkeypatch.setattr(media_probe, "ensure_meta", probe)
    sampled = await recipe._views(ctx, "clip")
    assert sorted(captures) == [(0, 1344, True), (.75, 1344, True), (1.275, 1344, True)]
    assert len({ref for _, _, ref in sampled}) == 3
    untouched = recipe.Plan(**treatment(product_assets=["clip"], video_samples={"product_1":"middle"}))
    real, _, _ = await recipe.filming_references(ctx, untouched)
    assert captures[-1] == (.5, 720, False) and real[0] != sampled[1][2]


@pytest.mark.parametrize("accepted,kind,expected_wait", [
    (False, "not_sent", [2, 10]), (False, "rate_limit", [30, 30]), (None, "unknown", []),
])
async def test_reference_camera_recovers_only_confirmed_unsent_requests(monkeypatch, accepted, kind, expected_wait):
    from light import wan_remake
    from tool.tool import ToolResult
    ctx = ToolContext(session_id="session", user_id="owner", workspace_id="ws")
    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(scalar=AsyncMock(return_value=None))
    refusal = ToolResult(title="provider", metadata={"rejection_kind":kind, "provider_accepted":accepted, "error":True})
    capture = AsyncMock(side_effect=[refusal, refusal, ToolResult(metadata={"asset_id":"frame"})])
    wait = AsyncMock()
    monkeypatch.setattr(wan_remake, "get_db_session", connection)
    monkeypatch.setattr(wan_remake, "_check", AsyncMock())
    monkeypatch.setattr(wan_remake.image_gen, "execute", capture)
    monkeypatch.setattr(wan_remake.asyncio, "sleep", wait)
    if expected_wait:
        assert await wan_remake._still(ctx, "job", "office", "set", [], language="vi") == "frame"
    else:
        with pytest.raises(remake.RemakeStopped, match="Chưa nhận được"):
            await wan_remake._still(ctx, "job", "office", "set", [], language="vi")
    assert [call.args[0] for call in wait.await_args_list] == expected_wait
    identities = [call.args[1].part_id for call in capture.await_args_list]
    assert len(set(identities)) == len(expected_wait) + 1
@pytest.mark.parametrize("copy,quote,keep", [
    ("Làm quảng cáo cho sản phẩm.", "", False),
    ("Làm quảng cáo cho sản phẩm.", "Giữ nguyên căn bếp", False),
    ("Giữ nguyên căn bếp ở cảnh đầu.", "Giữ nguyên căn bếp", True),
])
async def test_product_footage_does_not_supply_an_incidental_set(monkeypatch, copy, quote, keep):
    report = treatment()
    report.pop("product_assets")
    report.pop("language")
    report.update(product_views=[1], obscured_product_views=[1], setting_view=1, setting_scene="home", setting_quote=quote,
                  track_wardrobe="Áo chạy xanh navy.")
    monkeypatch.setattr(recipe, "_views", AsyncMock(return_value=[("clip", "late", "original-frame")]))
    monkeypatch.setattr(reference_quality, "ask", AsyncMock(side_effect=[
        {"report": report}, {"report": {"problems": []}},
    ]))
    plan = await recipe.adapt(None, {"asset_ids": ["clip"], "reference_copy": copy}, "vi")
    assert plan.product_assets == ["clip"] and plan.setting_asset == ("clip" if keep else "")
    assert plan.obscured_product_videos == []
    assert ("setting" in plan.video_samples) is keep


@pytest.mark.parametrize("action", ["prepare", "render"])
async def test_product_button_owns_planning_and_requires_no_second_confirmation(monkeypatch, action):
    run = {"id": "item", "action": action, "remake_version": wan_story.VERSION,
           "video_language": "vi", "job_prefix": "job"}
    form = {**initial_form("video", "", ""), "template": "product_story", "asset_ids": ["bottle"],
            "product_name": "Bình Cam", "run": run}
    row = SimpleNamespace(light_entry="video", light_example_id=None, light_production=form)
    receipt = SimpleNamespace(state="claimed", run_id="run", generation=2)
    ctx = ToolContext(session_id="session", user_id="owner", workspace_id="workspace", run_id="run", run_generation=2)
    monkeypatch.setattr(ToolContext, "assert_dispatch_allowed", AsyncMock())

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(get=AsyncMock(return_value=receipt))

    monkeypatch.setattr(production, "get_db_session", session)
    monkeypatch.setattr(production, "_lock", AsyncMock(return_value=row))
    candidate = recipe.Plan(**treatment(product="From photo"))
    plan_action = AsyncMock(return_value=candidate)
    film = AsyncMock(return_value=("movie", recipe.DURATION))
    monkeypatch.setattr(recipe, "adapt", plan_action)
    monkeypatch.setattr(commercial_film, "render", film)
    with pytest.raises(LightError, match="current draft's materials"):
        await production.begin_remake(ctx, candidate.model_copy(update={"setting_asset": "unselected"}), requested_action=action)

    tool = wan_tool_for_action(action, "product_story")
    assert set(tool.parameters.model_fields) == {"action"}
    result = await tool.execute(tool.parameters(), ctx)
    assert plan_action.await_count == 1 and result.metadata["guided_action_finished"] == action
    assert "Bình Cam" in row.light_production["script"] and "From photo" not in row.light_production["script"]
    assert row.light_production["remake_prepared_for"] == remake_case.input_fingerprint(row.light_production)
    assert bool(row.light_production["run"].get("commercial_auto_staging")) is (action == "render")
    if action == "prepare":
        assert not result.metadata["rendered"] and film.await_count == 0
        _, _, preview = await production.begin_remake(ctx, None, requested_action=action)
        assert preview.product == "Bình Cam"
    else:
        assert result.metadata["shots"] == 28 and result.metadata["asset_id"] == "movie"
        assert film.await_count == 1
        _, _, cached = await production.begin_remake(ctx, None, requested_action=action)
        assert cached.product_assets == ["bottle"] and cached.language == "vi"


def test_only_shortcut_requires_separate_preview_on_generate():
    form = {**initial_form("video", "", ""), "template": "product_story", "asset_ids": ["reference"]}
    row = SimpleNamespace(light_entry="video", light_production=form)
    view = {**form, "status": "draft", "materials": [{"id": "reference"}]}
    production._validate_action(row, view, "render")
    with pytest.raises(LightError, match="photo or video"):
        production._validate_action(row, {**view, "materials": []}, "render")
    row.light_production = {**form, "template": "shortcut_story"}
    with pytest.raises(LightError, match="分镜"):
        production._validate_action(row, {**view, "template": "shortcut_story"}, "render")


def test_delivery_recovers_an_existing_receipt_and_rejects_a_mismatched_subtitle_or_duplicate_take():
    from light import commercial_frame_audit as pixels
    from light.commercial_post import SCORE, score_for

    plan = recipe.Plan(**treatment())
    cuts = [dict(shot=i, source=f"film-{i}", review_board=f"review-{i}", review_version=recipe.REVIEW,
                 output_frames=beat.frames, frames=beat.frames, start=0, speed=1)
            for i, beat in enumerate(recipe.BEATS, 1)]
    result = {"shot_frames": [b.frames for b in recipe.BEATS], "score": SCORE, "frames": 1277, "fps": 24,
              "duration_sec": 1277 / 24, "has_audio": True, "language": "vi", "dialogue": 2,
              "dialogue_sources": [{"shot": 23}, {"shot": 25}],
              "subtitles_srt": f"1\n00:00:33,500 --> 00:00:34,000\n{plan.call_line}\n\n2\n00:00:36,000 --> 00:00:38,000\n{plan.rebuke_line}\n"}
    proof = {"approved": True, "cut_assets": [c["review_board"] for c in cuts]}
    run = {"remake_adaptation": plan.model_dump(), "wan_cuts": cuts, "wan_final_key": "final",
           "director_decisions": {f"{recipe.REVIEW}:joins": proof}}
    job = SimpleNamespace(kind="compose", status="completed", output_asset_id="movie", idempotency_key="final",
                          request_data=recipe.final_request(cuts, plan), result_data=result)
    for optional in ("video_samples", "call_voice_gender", "rebuke_voice_gender", "score_revision"):
        job.request_data["storyboard"].pop(optional)
    assert wan_story.is_complete_cut(run, job)
    revised = plan.model_copy(update={"score_revision": 2})
    new_run = {**run, "remake_adaptation": revised.model_dump()}
    revised_job = SimpleNamespace(**{**vars(job), "request_data": recipe.final_request(cuts, revised)})
    assert not wan_story.is_complete_cut(new_run, revised_job)
    revised_job.result_data = {**result, "score": score_for(revised)}
    assert not wan_story.is_complete_cut(new_run, revised_job)
    job.result_data = {**result, "subtitles_srt": result["subtitles_srt"].replace(plan.call_line, "A different line.")}
    assert not wan_story.is_complete_cut(run, job)
    job.result_data = result
    decisions = run["director_decisions"]
    proof.update(signature_check="gesture-and-callback", scene_checks={scene: scene for scene in commercial_film.SCENE_JOINS})
    decisions["gesture-and-callback"] = {"ok": True}
    for scene, pairs in commercial_film.SCENE_JOINS.items():
        decisions[scene] = {"ok": True, "sheets": ["seams"], "pairs": [[i, i + 1] for i in pairs],
                            "cut_assets": proof["cut_assets"][pairs[0] - 1:pairs[-1] + 1]}
    assert not wan_story.is_complete_cut(run, job)
    result["dialogue_sources"] = [{"shot": 23, "method": "native_asr", "asr_text": plan.call_line},
                                 {"shot": 25, "method": "tts", "asr_text": plan.rebuke_line}]
    assert wan_story.is_complete_cut(run, job)
    revised_job.result_data = {**result, "score": score_for(revised)}
    assert not wan_story.is_complete_cut(new_run, revised_job)
    proof.update(frame_plan=pixels.plan_id(revised), frame_checks=[])
    for cut in cuts:
        index = cut["shot"]
        identity = f"source-window-{index}"
        key = f"{recipe.REVIEW}:{pixels.VERSION}:{index}:{identity}"
        batches = []
        for first, last in pixels._batches(index):
            batch = f"{key}:read:{first}-{last}"
            batches.append(batch)
            decisions[batch] = {"cut": identity, "range": [first, last], "reviewed": True,
                                "sheets": [f"sheet-{n}" for n in range(first, last + 1, pixels.PAGE)],
                                "report": {"suspicions": [], "rows": [
                                    {"first": n, "last": min(n + 3, last), "observation": "Visible, stable product."}
                                    for n in range(first, last + 1, pixels.COLUMNS)]}}
        decisions[key] = {"version": pixels.VERSION, "identity": identity, "ok": True, "shot": index,
                          "board": cut["review_board"], "window": pixels.window(cut), "batches": batches, "native_checks": []}
        proof["frame_checks"].append(key)
    assert wan_story.is_complete_cut(new_run, revised_job)
    sport = revised.model_copy(update={"track_wardrobe": "Áo chạy xanh navy."})
    sport_run = {**new_run, "remake_adaptation": sport.model_dump()}
    revised_job.request_data = recipe.final_request(cuts, sport)
    assert not wan_story.is_complete_cut(sport_run, revised_job)
    decisions["track"]["ok"] = False
    assert not wan_story.is_complete_cut(run, job)
    decisions["track"]["ok"] = True
    cuts[17]["source"] = cuts[16]["source"]
    job.request_data = recipe.final_request(cuts, plan)
    assert not wan_story.is_complete_cut(run, job)


def test_explicit_retry_changes_only_the_rejected_commercial_take():
    report = {"shot": 11, "kind": "commercial", "reason": "Chưa có điểm chạm.", "correction": "Cho thấy điểm chạm ở cuối cảnh."}
    prior = {"remake_adaptation": {"format": recipe.FORMAT}, "brand_feedback": report}
    assert production._brand_retry(prior) == {}
    failed = {**prior, "remake_error": report["reason"]}
    first = production._brand_retry(failed)
    assert first["commercial_repairs"] == [{"target": 11, "revision": 1, "report": report}]
    second = production._brand_retry({**failed, **first})
    assert [(r["target"], r["revision"]) for r in second["commercial_repairs"]] == [(11, 1), (11, 2)]
    assert production._brand_retry({**failed, "wan_final_key": "already-edited"}) == {}
