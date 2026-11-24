import asyncio
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
import pytest

from light import commercial_film as film


GROUNDED = {"same_item": True, "consistent_hands": True, "plausible_support": True,
            "reference_item": "sample item", "filmed_item": "sample item", "support": "workbench"}


def plan(mode="carry"):
    return SimpleNamespace(mode=mode, language="vi", setting_scene="home", product="Bút máy", appearance="thân xanh đậm, ngòi bạc",
                           settings=SimpleNamespace(home="căn bếp", street="góc phố", track="đường chạy", office="văn phòng"),
                           handoff="Trao chiếc bút sang tay người quản lý.", contact="Chạm đầu ngón tay vào thân bút.",
                           carry="Bút được cài vào túi ngực.", street_anchor="Hình nhân bơm hơi màu đỏ.")


def shot(i, frames=24):
    b = film.recipe.BEATS[i - 1]
    return {"id": i, "scene": film.recipe.SCENE_IDS[i - 1], "frames": frames, "title": "Thử",
            "frame": b.opening, "action": b.action, "end": b.ending, "camera": b.camera,
            "product_visible": b.product, "spoken_line": "Được rồi." if i == 23 else ""}


def test_original_framing_guides_cover_the_28_edits():
    guides = sorted(film.GUIDES.glob("*.jpg"))
    assert [item.stem for item in guides] == [f"{i:02}" for i in range(1, 29)]
    assert all(Image.open(item).mode == "L" and Image.open(item).size == (320, 180) for item in guides)
    assert set(film.MATCH_IN) == {12, 18, 24}
    assert [sum(b.frames for b in film.recipe.BEATS[:i - 1]) for i in film.MATCH_IN] == [365, 528, 836]


def test_actual_frames_bind_the_contacts_handoff_and_return_to_the_corner():
    lib = {s: s for s in ("home", "office", "track", "street", "authority", "protagonist")}
    cuts = {i: {"review_head": f"head{i}", "review_tail": f"tail{i}"} for i in range(1, 29)}

    def refs(i, mode="carry"):
        return [asset for _, asset in film._setup_refs(plan(mode), shot(i), lib, ["actual-product"], cuts, "gray-layout")]

    assert refs(12)[:2] == ["tail11", "track"]
    assert refs(18)[0] == "tail17"
    assert refs(24)[0] == "tail23"
    assert refs(26)[0] == "tail25" and "actual-product" in refs(26)
    assert refs(27)[0] == "tail26" and "protagonist" not in refs(27)
    assert refs(28)[:3] == ["head22", "tail27", "street"] and "protagonist" not in refs(28)
    assert refs(15, "stationary")[0] == "home"
    assert "actual-product" in refs(22) and "actual-product" not in refs(22, "stationary")
    assert not shot(22)["product_visible"] and "túi ngực" in film._carry_state(plan(), shot(22))
    assert film._dependents(25) == {25, 26, 27, 28}
    assert film._dependents(17) == {17, 18}
    assert film._dependents(22) == {22, 28}


def test_short_insert_is_edited_from_real_motion_and_dance_has_a_beat():
    target = shot(5, 12)
    options = film._windows(target, 120, [1] * 75 + [10] * 14 + [1] * 30)
    assert all(count == 12 for _, count in options)
    assert any(75 <= start <= 79 for start, _ in options)
    assert film._windows(shot(12, 17), 120, [5] * 119) == [(0, 17)]
    assert film._windows(shot(11, 14), 120, [5] * 119)[0] == (106, 14)
    assert (0, 120) in film._windows(shot(26, 63), 120, [5] * 119)
    assert (0, 148) in film._windows(shot(25, 124), 148, [5] * 147)
    assert all(0 <= s and s + n <= 120 for s, n in film._windows(shot(28, 125), 120, [5] * 119))
    assert not film._windows(shot(28, 125), 30, [5] * 29)
    assert "101 BPM" in film.shot_request("session", plan(), shot(28), "head", None, "", 1).prompt
    request = film.shot_request("session", plan(), shot(23, 32), "head", "tail", "", 1)
    assert request.spoken_line == "Được rồi." and not request.no_speech
    assert [ref.role for ref in request.input_assets] == ["first_frame", "last_frame"]
    assert request.model == "MiniMax-H3-Max-Turbo"


def test_reference_score_drives_both_filming_and_motion_selection_with_a_separate_reprise_phase():
    p = plan()
    p.score_revision = 2
    assert film._first_beat(10, p) == pytest.approx(.11125)
    assert film._first_beat(28, p) == pytest.approx(.0805)
    assert film._first_beat(28, plan()) == film._first_beat(28) == 0.0
    prompt = film.shot_request("session", p, shot(28), "head", None, "", 1).prompt
    assert "128 BPM" in prompt and "0.08秒" in prompt and "0.469秒一拍" in prompt
    dance = shot(22, 77)
    envelope = [1.] * 143
    phase = (.08 + 12 * 60 / 128 - 727 / 24) % (60 / 128) * 24
    for beat in range(12):
        peak = 13 + round(phase + beat * 24 * 60 / 128)
        if peak < len(envelope):
            envelope[peak] = 20.
    new = film._windows(dance, 144, envelope, plan=p)
    old = film._windows(dance, 144, envelope, plan=plan())
    assert abs(new[0][0] - 13) <= 1 and abs(old[0][0] - 13) > 3


def test_dialogue_uses_complete_asr_only_inside_selected_cut():
    transcript = {"words": [{"from_sec": 2., "to_sec": 2.5, "text": "Được"},
                            {"from_sec": 2.53, "to_sec": 3., "text": "rồi"}]}
    speech = film.speech_window("Được rồi.", transcript, 5.)
    options = film._windows(shot(23, 32), 120, [5.] * 119, speech)
    s, n = options[0]
    assert film._dialogue({"start": s, "frames": n}, transcript, speech)["cues"][0]["text"] == "Được rồi."
    assert film._dialogue({"start": 65, "frames": 16}, transcript, speech) == {
        "cues": [], "speech_range": [65 / 24, 3.]}
    assert film._dialogue({"start": 0, "frames": 10}, transcript, None) == {"cues": []}


def test_static_frames_and_internal_cuts_fail_physical_check():
    frames = [Image.new("RGB", (64, 36), (12, 30, 44)) for _ in range(36)]
    assert not film._physical_options(frames, shot(12, 36), None)
    frames[18:] = [Image.new("RGB", (64, 36), (220, 220, 220)) for _ in range(18)]
    assert not film._physical_options(frames, shot(12, 36), None)


@pytest.mark.asyncio
async def test_fully_static_real_source_keeps_visual_evidence_for_the_directed_restage(monkeypatch, tmp_path):
    from video import media_probe

    async def download(*_args):
        pass
    async def meta(*_args, **_kwargs):
        return {"has_audio": False}
    async def flat(*_args):
        return Image.new("RGB", film.REVIEW_SIZE, "gray").tobytes() * 48
    async def store(_ctx, name, data):
        assert Image.open(BytesIO(data)).width == film.REVIEW_SIZE[0] * 5
        return name
    monkeypatch.setattr(film.product_story, "_download", download)
    monkeypatch.setattr(media_probe, "ensure_meta", meta)
    monkeypatch.setattr(film, "ff", flat)
    monkeypatch.setattr(film, "_reference_asset", store)
    result = await film._inspect(SimpleNamespace(user_id="test"), tmp_path, plan(), shot(5, 12), "real-static",
                                 ("first", None), {}, [], {})
    assert not result["ok"] and result["evidence"] == ["commercial-rejected-motion-real-static"]
    assert result["observed_frames"] == 48


@pytest.mark.asyncio
async def test_schedule_follows_real_independent_completions():
    events, cuts = [], {}
    async def work(s):
        i = s["id"]
        assert all(dep in cuts for dep in film.DEPENDENCIES.get(i, ()))
        await asyncio.sleep(0)
        events.append(i)
        return {"shot": i}
    await film._shoot_remaining(work, [shot(i) for i in range(1, 29)], cuts, concurrency=4)
    assert sorted(events) == list(range(1, 29))
    assert events.index(25) < events.index(26) < events.index(27) < events.index(28)


@pytest.mark.asyncio
async def test_real_video_decode_keeps_exact_full_width_head_and_tail(tmp_path, monkeypatch):
    from light.product_story_post import ff

    filename = tmp_path / "take.mp4"
    await ff("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24", "-frames:v", "24",
             "-threads:v", "1", "-c:v", "libx264", filename)
    saved = {}
    async def capture(_ctx, name, data):
        saved[name] = data
        return name
    monkeypatch.setattr(film, "_reference_asset", capture)
    head = await film._actual_frame(None, "asset-take", filename, 0)
    tail = await film._actual_frame(None, "asset-take", filename, 23)
    assert Image.open(BytesIO(saved[head])).size == (1024, 576)
    assert saved[head] != saved[tail]
    with pytest.raises(film.RemakeStopped):
        await film._actual_frame(None, "asset-take", filename, 26)


@pytest.mark.asyncio
async def test_restart_between_seam_approval_and_last_cut_write_restores_exact_edits(monkeypatch):
    from dataclasses import dataclass
    from light import production

    @dataclass
    class Context:
        message_id: str = "event"
        sandbox: object = None
        _on_output: object = None

    cuts = [{"shot": i, "source": f"video{i}", "review_board": f"review{i}",
             "output_frames": b.frames, "review_version": film.recipe.REVIEW, "frames": b.frames,
             "start": 0, "speed": 1, "cues": []} for i, b in enumerate(film.recipe.BEATS, 1)]
    decisions = {f"provisional{i}": {"ok": True, "cut": cut} for i, cut in enumerate(cuts)}
    decisions["rejected-alternative-11"] = {"ok": True, "cut": {**cuts[10], "source": "wrong", "review_board": "wrong"}}
    decisions[f"{film.recipe.REVIEW}:joins"] = {"approved": True, "cut_assets": [c["review_board"] for c in cuts]}
    run = {"job_prefix": "prefix", "director_decisions": decisions, "wan_cuts": list(cuts[:4])}
    assert film._approved_edit(run)[11]["source"] == "video11"
    assert film._approved_edit({**run, "director_decisions": {}}) is None
    assert film._approved_edit({**run, "director_decisions": {
        **decisions, f"{film.recipe.REVIEW}:joins": {"approved": True, "cut_assets": ["undelivered"] * 28}}}) is None

    async def begin(*_args):
        return run

    async def noop(*_args):
        return True

    async def save_cut(_ctx, _prefix, item):
        assert len(run["wan_cuts"]) + 1 == item["shot"]
        run["wan_cuts"].append(item)

    async def save_edit(_ctx, _prefix, *, cuts, final_key):
        assert cuts == run["wan_cuts"]
        run["wan_final_key"] = final_key
        return run

    async def job(*_args):
        return SimpleNamespace(output_asset_id="published")

    monkeypatch.setattr(production, "begin_reference_edit", begin)
    monkeypatch.setattr(production, "remake_stage", noop)
    monkeypatch.setattr(production, "save_reference_cut", save_cut)
    monkeypatch.setattr(production, "save_wan_edit", save_edit)
    monkeypatch.setattr(film.recipe, "shots", lambda _plan: [shot(i) for i in range(1, 29)])
    monkeypatch.setattr(film.recipe, "final_request", lambda _cuts, _plan: {"cut": "final"})
    monkeypatch.setattr(film.recipe, "is_complete_cut", lambda _run, _job: _run["wan_cuts"] == cuts)
    monkeypatch.setattr(film, "_existing_job", job)
    monkeypatch.setattr(film.vp, "_attach_completed", noop)
    assert await film.render(Context(), {}, run, plan()) == ("published", 1277 / 24)


@pytest.mark.asyncio
async def test_interrupted_motion_review_reuses_paid_take_and_replays_its_correction(monkeypatch, tmp_path):
    from light import production, shot_editor

    run = {"director_decisions": {}}
    requests, inspected = [], []
    async def guide(*_args):
        return "blocking"

    async def frames(_ctx, _prefix, _plan, _shot, _library, _material, _cuts, _guide, correction, _run):
        return ("corrected-head" if correction else "head"), None

    async def submit(_ctx, request, **_kwargs):
        requests.append(request.idempotency_key)
        return f"source-{request.idempotency_key}"

    async def inspect(_ctx, _folder, _plan, _shot, source, *_args):
        inspected.append(source)
        if len(inspected) == 1:
            return {"ok": False, "reason": "Broken pose", "correction": "Show one continuous wrist movement."}
        if len(inspected) == 2:
            raise OSError("worker disconnected after the second delivery")
        return {"ok": True, "cut": {"source": source}}

    async def save(_ctx, _prefix, _num, key, decision):
        return run["director_decisions"].setdefault(key, decision)

    async def noop(*_args, **_kwargs):
        pass

    monkeypatch.setattr(film, "_reference_asset", guide)
    monkeypatch.setattr(film, "_frames", frames)
    monkeypatch.setattr(film, "_inspect", inspect)
    monkeypatch.setattr(film, "_job", submit)
    monkeypatch.setattr(shot_editor, "publish_request", noop)
    monkeypatch.setattr(production, "save_director_decision", save)
    args = (None, tmp_path, "prefix", run, plan(), shot(4), {}, [], {}, "")
    with pytest.raises(OSError):
        await film._film(*args)
    cut = await film._film(*args)
    assert cut["take"] == 2 and requests[1] == requests[2] and requests[0] != requests[1]
    assert await film._film(*args) == cut
    assert len(requests) == 3


def test_contact_can_be_cut_from_earlier_motion_before_a_long_held_tail():
    from PIL import ImageDraw

    frames = []
    for i in range(120):
        x = 18 + 3 * max(0, min(i - 20, 23))
        frame = Image.new("RGB", (160, 90), "#302322")
        ImageDraw.Draw(frame).rectangle((x, 25, x + 38, 76), fill="#e39445")
        frames.append(frame)
    target = shot(11, 14)
    old = film._windows(target, len(frames), film._motion_deltas(frames))
    assert all(len(set(fr.tobytes() for fr in frames[s:s + n])) == 1 for s, n in old)
    options = film._physical_options(frames, target, None, frames[-1])
    assert options and any(start < 43 <= start + n - 1 <= 60 for start, n in options)
    assert not film._physical_options([frames[-1]] * 120, target, None, frames[-1])


@pytest.mark.asyncio
async def test_preflight_corrects_only_an_already_touched_start_before_buying_motion(monkeypatch):
    from light import production

    run = {}
    created, inspected = [], []
    async def create(_ctx, _prefix, name, prompt, references, **_kwargs):
        created.append((prompt, references))
        return f"frame{len(created)}"

    async def see(_ctx, _name, _schema, _brief, _data, pictures, **_kwargs):
        inspected.append(pictures)
        assert _schema is film.FirstMatchStaging and film.MATCH_ENTRY_REVIEW in _brief
        return {"report": {**GROUNDED, "ok": len(inspected) == 2, "reason": "Compare finger position.",
                           "entry_path": "From the far edge to the product.", "has_clear_approach": len(inspected) == 2,
                           "reached_feature": True,
                           "first_correction": "Move the hand above the product." if len(inspected) == 1 else "",
                           "last_correction": ""}}

    async def save(_ctx, _prefix, _index, _key, decision):
        return decision

    monkeypatch.setattr(film, "_still", create)
    monkeypatch.setattr(film.reference_quality, "ask", see)
    monkeypatch.setattr(production, "save_director_decision", save)
    roles = {"home": "kitchen", "protagonist": "person", "product_cards": ["clean-product"]}
    pair = await film._frames(None, "job", plan(), shot(11, 14), roles, ["product-photo"], {}, "guide", "", run)
    assert pair == ("frame3", "frame2")
    assert inspected == [["frame1", "frame2", "product-photo"], ["frame3", "frame2", "product-photo"]]
    assert created[2][1][0] == "frame2" and "倒推" in created[2][0]
    assert all("clean-product" in refs and "product-photo" not in refs for _, refs in created)
    assert film.MATCH_ENTRY_OPENING in created[0][0] and plan().contact not in created[0][0]
    assert plan().contact in created[1][0] and film.MATCH_ENTRY_OPENING not in created[1][0]
    assert "同时承重" in created[0][0] and "画缘以外" in created[2][0]
    assert "约半秒" in film.shot_request("run", plan(), shot(11, 14), "start", "finish", "", 1).prompt


def test_long_product_and_dance_descriptions_fit_the_paid_provider_prompt():
    p = plan("wear")
    p.street_anchor = "đỏ " * 116
    p.carry = "dây " * 87
    p.appearance = "vải " * 87
    s = shot(22, 77)
    s.update(frame="Khuôn hình " + p.street_anchor, action="Diễn viên nhảy " + "vẫy " * 85)
    correction = "giữ tay tại đây " * 30
    req = film.shot_request("job", p, s, "still", None, correction, 2)
    prompt, _ = film.vp.resolve_spoken_line(req.prompt, spoken_line=req.spoken_line,
                                            no_speech=req.no_speech, generate_audio=req.generate_audio)
    assert len(prompt) <= 2048
    assert correction[:200] in prompt and "101 BPM" in prompt
    assert "tiếng Việt" in film.REVIEW_LANGUAGE["vi"]
    assert not film._has_cjk(film._reason("vi", "镜头没有通过"))


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "scene"), [("stationary", "home"), ("stationary", "office"), ("carry", "home"), ("wear", "home")])
async def test_a_stationary_product_keeps_its_actual_installation_at_work(monkeypatch, mode, scene):
    p = plan(mode)
    p.setting_scene = scene
    p.protagonist, p.authority = "Mai", "Hùng"
    captured = {}

    async def generate(_ctx, _prefix, name, prompt, refs, **kwargs):
        assert kwargs["language"] == p.language
        kind = name.split("-")[1]
        captured[kind] = (prompt, refs)
        return "generated-" + kind

    from light import commercial_product_reference

    async def inputs(_ctx, supplied, *, prefix, run):
        assert supplied is p and prefix == "prefix"
        return ["actual-product"], ["clean-object"], ["actual-performer"], ["actual-room"]
    async def carried(_ctx, _prefix, _run, _plan, cast, clean, original):
        assert cast == "generated-protagonist" and clean == ["clean-object"] and original == ["actual-product"]
        return "verified-worn", False, _plan
    async def clean_cast(_ctx, _prefix, _run, _plan, identities, original):
        assert identities["authority"] == "generated-authority" and original == ["actual-product"]
    async def contact(_ctx, _prefix, _run, _plan, carried, original):
        assert carried == "verified-worn" and original == ["actual-product"]
        return "verified-contact"
    monkeypatch.setattr(film, "_verify_unworn_cast", clean_cast)
    monkeypatch.setattr(film, "_freeze_contact", contact)
    monkeypatch.setattr(film, "_carried_actor", carried)
    monkeypatch.setattr(commercial_product_reference, "filming_inputs", inputs)
    monkeypatch.setattr(film, "_still", generate)
    libraries, product, shooting_plan = await film._cast_and_sets(None, "prefix", p)
    assert shooting_plan is p
    assert product == ["actual-product"] and libraries["office"] == "generated-office"
    assert captured["protagonist"][1] == ["actual-performer"]
    if mode == "stationary":
        other = "office" if scene == "home" else "home"
        assert captured[scene][1] == ["actual-room", "clean-object"]
        assert captured[other][1] == ["generated-" + scene, "clean-object"]
        anchor, _ = film._setup_refs(p, shot(12), libraries, product, {11: {"review_tail": "edge"}}, "board")[0]
        assert "只继承脸和空手" in anchor
    else:
        assert not captured["office"][1] and captured["home"][1] == ["actual-room"]
    assert not captured["street"][1] and not captured["track"][1]


@pytest.mark.asyncio
async def test_scene_evidence_covers_all_cuts_and_repairs_only_the_changed_group(monkeypatch, tmp_path):
    from light import production

    highlighted = {i - 1 for i in film.MATCH_IN} | {27}
    ordinary = {n for pairs in film.SCENE_JOINS.values() for n in pairs}
    assert sorted(ordinary | highlighted) == list(range(1, 28))
    assert not ordinary.intersection(highlighted)
    cuts = {}
    for i in range(1, 29):
        cuts[i] = {"review_board": f"board{i}", "review_head": f"head{i}", "review_tail": f"tail{i}"}
        Image.new("RGB", (1920, 236), (i * 5, 50, 80)).save(tmp_path / f"board{i}.jpg")
    run, reviewed = {}, []
    async def save(_ctx, _prefix, _i, _key, decision):
        return decision
    async def asset(_ctx, key, data):
        assert Image.open(BytesIO(data)).width == 1280
        return key
    async def approve(_ctx, name, schema, _guide, data, images, **_options):
        if name == "commercial_reference_joins":
            reviewed.append("signature")
            return {"report": {"ok": True, "reason": "Good.", "shot": 28}}
        scene = data["scene"]
        reviewed.append(scene)
        assert len(images) == 1 + data["sheets"]
        if scene == "home" and reviewed.count(scene) == 1:
            return {"report": {"ok": False, "reason": "The cap jumps.", "correction": "Show the closed container.",
                               "after": 6, "shot": 7, "outgoing": "The lid was fully shut.",
                               "incoming": "The same container is open before it was moved."}}
        return {"report": {"ok": True, "reason": "Same visible state."}}

    monkeypatch.setattr(production, "save_director_decision", save)
    monkeypatch.setattr(film, "_reference_asset", asset)
    monkeypatch.setattr(film.reference_quality, "ask", approve)
    monkeypatch.setattr(film.recipe, "shots", lambda _p: [shot(i) for i in range(1, 29)])
    issues = await film._check_joins(None, tmp_path, "job", run, plan(), cuts, ["product"])
    assert [v["shot"] for v in issues] == [7]
    corrections, adjacent = {}, {}
    old = cuts[7].copy()
    film._repair_joins(issues, cuts, corrections, adjacent)
    assert sorted(set(range(1, 29)) - cuts.keys()) == [7]
    assert adjacent[7][0][1] == "tail6"
    assert film.recipe.REVIEW + ":joins" not in run["director_decisions"]
    cuts[7] = {**old, "review_board": "corrected7"}
    Image.new("RGB", (1920, 236), "#555").save(tmp_path / "corrected7.jpg")
    assert not await film._check_joins(None, tmp_path, "job", run, plan(), cuts, ["product"])
    assert reviewed.count("home") == 2 and all(reviewed.count(k) == 1 for k in ("signature", "track", "street", "office"))
    proof = run["director_decisions"][film.recipe.REVIEW + ":joins"]
    assert set(proof["scene_checks"]) == set(film.SCENE_JOINS)
    assert film._scene_proofs(run["director_decisions"], proof)
    broken = {k: v for k, v in run["director_decisions"].items() if k != proof["scene_checks"]["track"]}
    assert not film._scene_proofs(broken, proof)
    for scene, key in proof["scene_checks"].items():
        saved = run["director_decisions"][key]
        assert saved["sheets"] and saved["pairs"] == [[i, i + 1] for i in film.SCENE_JOINS[scene]]


@pytest.mark.asyncio
async def test_normal_cuts_recheck_old_hand_release_once_and_keep_the_signature_cut_review(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from light import production

    p = plan("wear")
    p.score_revision, p.transfer_mode = 2, "fit"
    cuts = {i: {"review_board": f"board{i}", "review_head": f"head{i}", "review_tail": f"tail{i}"}
            for i in range(1, 29)}
    signature_ids = sorted({i for pair in film.MATCH_IN.items() for i in pair} | {14, 22, 25, 26, 27, 28})
    old_signature = f"{film.recipe.REVIEW}:signature:{film._fingerprint([cuts[i]['review_board'] for i in signature_ids])}"
    office_ids = range(24, 28)
    old_office = f"{film.recipe.REVIEW}:scene:office:{film._fingerprint([cuts[i]['review_board'] for i in office_ids])}"
    previous = {"ok": False, "reason": "The dressing hand exits across the cut."}
    run = {"director_decisions": {old_signature: previous, old_office: previous}}
    for i in office_ids:
        Image.new("RGB", (400, 110), (i, 90, 30)).save(tmp_path / f"board{i}.jpg")

    async def review(_ctx, name, schema, directions, data, images, **opts):
        if name == "commercial_reference_joins":
            assert "board26" not in images and "board22" in images and "board27" in images and "board28" in images
            assert all(frame in images for frame in ("tail11", "head12", "tail17", "head18", "tail23", "head24"))
            return {"report": {"ok": True, "shot": 28, "reason": "The same receiver returns to the original street."}}
        assert name == "commercial_scene_seams" and "自然动作省略" in directions and "next_opening" not in data
        return {"report": {"ok": True, "reason": "The receiver keeps the cap across the reaction cut."}}

    ask = AsyncMock(side_effect=review)
    monkeypatch.setattr(film.reference_quality, "ask", ask)
    monkeypatch.setattr(film, "_reference_asset", AsyncMock(return_value="office-sheet"))
    monkeypatch.setattr(film.recipe, "shots", lambda _: [shot(i) for i in range(1, 29)])
    monkeypatch.setattr(production, "save_director_decision", AsyncMock(side_effect=lambda *args: args[-1]))
    for _ in range(2):
        key, signature = await film._signature_joins(None, "render", run, p, cuts, ["product"])
        scene, office = await film._scene_joins(None, tmp_path, "render", run, p, cuts, ["product"], "office")
        assert key != old_signature and scene != old_office and signature["ok"] and office["ok"]
    assert ask.await_count == 2 and run["director_decisions"][old_signature] is previous
    run["director_decisions"][old_office] = approved = {"ok": True, "sheets": ["historic"]}
    assert await film._scene_joins(None, tmp_path, "render", run, p, cuts, [], "office") == (old_office, approved)
    assert ask.await_count == 2


@pytest.mark.asyncio
async def test_modern_cut_audits_legacy_aggregate_without_overwriting_it_on_resume(monkeypatch, tmp_path):
    from light import production

    p = plan("wear")
    p.score_revision, p.track_wardrobe = 2, "Navy athletic kit."
    cuts = {i: {"shot": i, "source": f"source{i}", "review_board": f"board{i}", "review_version": film.recipe.REVIEW,
                "output_frames": beat.frames} for i, beat in enumerate(film.recipe.BEATS, 1)}
    boards = [cut["review_board"] for cut in cuts.values()]
    base = f"{film.recipe.REVIEW}:joins"
    legacy = {"approved": True, "cut_assets": boards}
    decisions = {base: legacy}
    run = {"director_decisions": decisions, "wan_cuts": list(cuts.values())}
    assert film._edit_proof(decisions, plan(), boards) == legacy
    assert film._approved_edit(run, p) is None and not film._scene_proofs(decisions, legacy, p)
    async def signature(*args):
        decisions["wardrobe"] = {"ok": True, "wardrobe_continuous": True, "sheet": "real-six-frames"}
        decisions["signature"] = {"ok": True, "wardrobe_check": "wardrobe", "report": {}}
        return "signature", decisions["signature"]
    async def scene(*args):
        name = args[-1]
        joins = film.SCENE_JOINS[name]
        decisions[name] = {"ok": True, "sheets": [f"sheet-{name}"], "pairs": [[i, i + 1] for i in joins],
                           "cut_assets": boards[joins[0] - 1:joins[-1] + 1]}
        return name, decisions[name]
    async def save(_ctx, _prefix, _index, key, data):
        return decisions.setdefault(key, data)
    monkeypatch.setattr(film, "_signature_joins", signature)
    monkeypatch.setattr(film, "_scene_joins", scene)
    monkeypatch.setattr(production, "save_director_decision", save)
    assert not await film._check_joins(None, tmp_path, "job", run, p, cuts, [])
    assert decisions[base] is legacy and any(key.startswith(base + ":audited:") for key in decisions)
    audited = film._edit_proof(decisions, p, boards)
    assert audited["signature_check"] == "signature" and film._approved_edit(run, p) == cuts
    decisions["wardrobe"]["ok"] = False
    assert film._edit_proof(decisions, p, boards) is None
    decisions.pop("wardrobe")
    assert not film._scene_proofs(decisions, audited, p) and film._approved_edit(run, p) is None
    assert film._edit_proof(decisions, plan(), boards) == legacy


@pytest.mark.asyncio
async def test_only_an_explicit_retry_reopens_a_rejected_shot(monkeypatch, tmp_path):
    from light import production, shot_editor

    run = {}
    paid = []
    async def guide(*_args):
        return "guide"

    async def frames(*args, **kwargs):
        return f"opening-{kwargs.get('user_revision', 0)}", None

    async def submit(_ctx, request, **kwargs):
        paid.append(request.idempotency_key)
        return request.idempotency_key

    async def check(_ctx, _folder, _plan, _shot, key, *_args):
        if key.endswith(":revision2"):
            return {"ok": True, "cut": {"source": key}}
        return {"ok": False, "reason": "Động tác chưa rõ.", "correction": "Chạm bằng đầu ngón tay."}

    async def save(_ctx, _prefix, _index, _key, result):
        return result

    async def noop(*args, **kwargs):
        pass

    monkeypatch.setattr(film, "_reference_asset", guide)
    monkeypatch.setattr(film, "_frames", frames)
    monkeypatch.setattr(film, "_job", submit)
    monkeypatch.setattr(film, "_inspect", check)
    monkeypatch.setattr(shot_editor, "publish_request", noop)
    monkeypatch.setattr(production, "save_director_decision", save)
    args = (None, tmp_path, "prefix", run, plan(), shot(4), {}, [], {})
    with pytest.raises(film.ReviewRejected) as stopped:
        await film._film(*args, "")
    assert len(paid) == 3
    feedback = stopped.value.review_feedback
    assert feedback["shot"] == 4 and feedback["kind"] == "commercial" and feedback["reason"] == str(stopped.value)
    # A worker restart within the same receipt replays the recorded rejection.
    with pytest.raises(film.ReviewRejected):
        await film._film(*args, "")
    assert len(paid) == 3
    run["commercial_repairs"] = [{"target": 4, "revision": 1, "report": feedback},
                                 {"target": 4, "revision": 2, "report": feedback}]
    revision, note = film._requested_repairs(run)[4]
    result = await film._film(*args, note, user_revision=revision)
    assert result["source"] == paid[-1] and len(set(paid)) == 4
    assert await film._film(*args, note, user_revision=revision) == result and len(paid) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("accept_staging", [True, False])
async def test_exhausted_endpoints_get_one_new_staging_before_any_video_submission(monkeypatch, accept_staging):
    from light import production

    p = plan()
    p.handoff_state = "The other actor controls the open item; the detached part rests on the workbench."
    cuts = {25: {"review_tail": "office-shot"}}
    rooms = {"office": "set", "authority": "boss", "protagonist": "performer", "product_cards": ["clean-product"]}
    pictures, checks, run = [], [], {}

    async def image(_ctx, _prefix, name, prompt, refs, **options):
        pictures.append((name, prompt, refs))
        return f"photo{len(pictures)}"

    async def audit(_ctx, _name, _schema, guide, _data, evidence, **options):
        checks.append(evidence)
        assert "朝下" in guide and "不能" in guide
        return {"report": {**GROUNDED, "ok": accept_staging and len(checks) == 4, "reason": "Cần kiểm tra hai đầu.",
                           "first_correction": "Giữ điểm tựa ở nguyên vị trí.", "last_correction": ""}}

    async def save(_ctx, _prefix, _idx, _key, result):
        return result

    monkeypatch.setattr(film, "_still", image)
    monkeypatch.setattr(film.reference_quality, "ask", audit)
    monkeypatch.setattr(production, "save_director_decision", save)
    async def locked(_ctx, _prefix, _run, _plan, first, last):
        assert first == "photo5" and last == "photo6"
        return last
    monkeypatch.setattr(film, "_lock_handoff_background", locked)
    call = film._frames(None, "run", p, shot(26, 63), rooms, ["product"], cuts, "guide", "", run)
    if accept_staging:
        assert await call == ("photo5", "photo6")
    else:
        with pytest.raises(film.ReviewRejected) as failure:
            await call
        assert failure.value.review_feedback["shot"] == 26
    assert len(checks) == 4 and checks[-1][:2] == ["photo5", "photo6"]
    assert "换成更容易实拍的摆位" in pictures[-2][1] and "photo5" in pictures[-1][2]


def test_restage_changes_only_the_handoff_region_and_cannot_approve_wrong_product_or_support():
    from pydantic import ValidationError

    first = Image.new("RGB", (300, 180), "#042142")
    last = Image.new("RGB", first.size, "#df8c4b")
    composite = Image.open(BytesIO(film._same_plate(first, last, (200, 200, 650, 650))))
    assert composite.getpixel((30, 15)) == first.getpixel((30, 15))
    assert composite.getpixel((100, 80)) == last.getpixel((100, 80))
    assert composite.getpixel((267, 153)) == first.getpixel((267, 153))
    for problem in ["same_item", "plausible_support", "consistent_hands"]:
        with pytest.raises(ValidationError):
            film.ContactStaging.model_validate({**GROUNDED, problem: False, "ok": True, "reason": "Unsubstantiated."})


@pytest.mark.asyncio
async def test_office_entry_still_stages_the_pre_action_pose_only(monkeypatch):
    p = plan()
    s = shot(25, 124)
    s["frame"] = "The lead has just entered; the manager is still seated in the distance."
    s["action"] = "The manager stands, while the lead takes the product out."
    s["end"] = "The manager is standing and the product is in the lead's hand."
    prompts = []
    async def still(_ctx, _prefix, _name, prompt, refs, **kwargs):
        prompts.append(prompt)
        return "opening"
    monkeypatch.setattr(film, "_still", still)
    roles = {"office": "set", "authority": "boss", "protagonist": "lead", "product_cards": ["clean-product"]}
    assert await film._frames(None, "run", p, s, roles, ["product"], {}, "guide", "", {}) == ("opening", None)
    assert s["frame"] in prompts[0] and s["action"] not in prompts[0] and s["end"] not in prompts[0]
    assert p.carry in prompts[0]
    assert s["action"] in film.shot_request("run", p, s, "opening", None, "", 1).prompt


@pytest.mark.asyncio
async def test_only_an_independently_approved_opening_can_be_used_without_a_last_frame(monkeypatch):
    from light import production

    run, calls = {}, []
    async def still(_ctx, _prefix, name, prompt, refs, **kwargs):
        return name

    async def audit(_ctx, name, schema, guide, data, images, **kwargs):
        calls.append((name, images))
        if name == "commercial_single_opening":
            return {"report": {"ok": True, "reason": "Tay chưa chạm; vật được đỡ đúng."}}
        return {"report": {**GROUNDED, "ok": False, "reason": "Đích đến chưa rõ.",
                           "entry_path": "Hand starts offscreen.", "has_clear_approach": True, "reached_feature": False,
                           "first_correction": "", "last_correction": "Sửa điểm đến ở khung cuối."}}

    async def save(_ctx, _prefix, _index, _key, result):
        return result

    monkeypatch.setattr(film, "_still", still)
    monkeypatch.setattr(film.reference_quality, "ask", audit)
    monkeypatch.setattr(production, "save_director_decision", save)
    opening, ending = await film._frames(None, "render", plan(), shot(11), {"home": "room", "protagonist": "actor", "product_cards": ["clean-product"]},
                                         ["product"], {}, "layout", "", run)
    assert ending is None and calls[-1] == ("commercial_single_opening", [opening, "product"])
    assert len(calls) <= 5


@pytest.mark.asyncio
async def test_one_bad_first_frame_only_video_does_not_buy_another(monkeypatch, tmp_path):
    from light import production, shot_editor

    run, paid = {}, []
    async def guide(*args):
        return "guide"
    async def one(*args, **kwargs):
        return "approved-opening", None
    async def submit(_ctx, req, **kwargs):
        assert [r.role for r in req.input_assets] == ["first_frame"]
        paid.append(req.idempotency_key)
        return "take"
    async def inspect(*args):
        return {"ok": False, "reason": "Không thấy mẫu tiếp xúc cuối.", "correction": "Giữ đúng điểm chạm."}
    async def save(_ctx, _prefix, _i, key, data):
        return run.setdefault("director_decisions", {}).setdefault(key, data)
    async def noop(*args, **kwargs):
        pass
    monkeypatch.setattr(film, "_reference_asset", guide)
    monkeypatch.setattr(film, "_frames", one)
    monkeypatch.setattr(film, "_job", submit)
    monkeypatch.setattr(film, "_inspect", inspect)
    monkeypatch.setattr(shot_editor, "publish_request", noop)
    monkeypatch.setattr(production, "save_director_decision", save)
    args = (None, tmp_path, "prefix", run, plan(), shot(11), {}, [], {}, "")
    for _ in range(2):
        with pytest.raises(film.ReviewRejected):
            await film._film(*args)
    assert len(paid) == 1
    assert any(k.endswith("single-camera:11:r0") for k in run["director_decisions"])


@pytest.mark.asyncio
@pytest.mark.parametrize("use_real_opening", [True, False])
async def test_confrontation_retake_checks_the_real_opening_and_blocks_in_the_ready_end(monkeypatch, use_real_opening):
    from light import production

    p = plan()
    p.handoff_state = "The object is prepared on the workbench; the detached part is outside the frame."
    frames, decisions = [], []
    async def make(_ctx, _prefix, name, prompt, refs, **kwargs):
        frames.append((prompt, refs))
        return f"new{len(frames)}"
    async def check(_ctx, name, schema, guide, data, refs, **kwargs):
        decisions.append((name, refs))
        if name == "commercial_office_opening":
            return {"report": {"ok": use_real_opening, "reason": "Check prop timing.",
                               "correction": "" if use_real_opening else "Start in the doorway."}}
        return {"report": {"ok": True, "reason": "The approach ends before the transfer."}}
    async def save(_ctx, _prefix, _index, _key, result):
        return result
    monkeypatch.setattr(film, "_still", make)
    monkeypatch.setattr(film.reference_quality, "ask", check)
    monkeypatch.setattr(production, "save_director_decision", save)
    rooms = {"office": "set", "protagonist": "actor", "authority": "manager", "product_cards": ["isolated"]}
    first, last = await film._frames(None, "job", p, shot(25), rooms, ["source-photo"], {}, "guide", "修正道具时序。", {},
                                     office_retake=True, observed_start="actual-video-frame-0")
    assert first == ("actual-video-frame-0" if use_real_opening else "new1")
    assert frames[-1][1][0] == first and p.handoff_state in frames[-1][0]
    assert decisions[-1] == ("commercial_office_path", [first, last, "source-photo"])


def test_actual_carry_support_starts_at_departure_and_ends_at_office_preparation():
    p = plan()
    for n in (10, 11, 12, 17, 18, 23, 24, 25):
        constraint = film._carry_state(p, shot(n))
        assert p.carry in constraint and "不能用新桌面" in constraint
    assert p.carry in film._carry_state(p, shot(9))
    for n in (2, 3, 6, 26, 27, 28):
        assert film._carry_state(p, shot(n)) == ""
    assert film._carry_state(p, shot(24), ending=True)
    assert not film._carry_state(p, shot(25), ending=True)
    assert not film._carry_state(plan("stationary"), shot(17))
    req = film.shot_request("render", p, shot(11), "first", "last", "补拍", 2)
    assert p.carry in req.prompt


@pytest.mark.asyncio
async def test_combined_carry_card_is_corrected_once_and_reused_only_where_it_can_be_seen(monkeypatch):
    from light import production

    p = plan()
    p.protagonist = "Mai"
    made, reviews = [], []
    run = {"director_decisions": {
        "commercial-product:neutral-photograph:another": {"approved": True, "photograph": "other-object",
                        "identification": {"visible_identifiers": "Unrelated geometry"}},
        "commercial-product:neutral-photograph:verified": {"approved": True, "photograph": "clean-object",
                        "identification": {"visible_identifiers": "One continuous cap with one actual opening"}},
    }}
    async def image(_ctx, _prefix, name, prompt, refs, **kwargs):
        assert kwargs["language"] == p.language
        assert "One continuous cap with one actual opening" in prompt and "Unrelated geometry" not in prompt
        made.append((name, list(refs)))
        return name
    async def assess(_ctx, name, schema, guidance, data, refs, **options):
        reviews.append(refs)
        ok = len(reviews) == 2
        return {"report": {"ok": ok, "reason": "Check the actual clip.", "correction": "" if ok else "Clip it to the shirt pocket.",
                           "actor_matches": True, "product_matches": True, "support_matches": ok,
                           "position_matches": True, "contact_point_reachable": ok, "single_product": True,
                           "product_at_feet": False}}
    async def save(_ctx, _prefix, _index, key, data):
        return run.setdefault("director_decisions", {}).setdefault(key, data)
    monkeypatch.setattr(film, "_still", image)
    monkeypatch.setattr(film.reference_quality, "ask", assess)
    monkeypatch.setattr(production, "save_director_decision", save)
    params = (None, "job", run, p, "actor", ["clean-object"], ["original-room-photo"])
    picture, feet, shooting_plan = await film._carried_actor(*params)
    assert shooting_plan is p
    assert not feet and len(made) == 2
    assert all("original-room-photo" not in sources and "clean-object" in sources for _, sources in made)
    assert all(images[1:] == ["actor", "original-room-photo"] for images in reviews)
    assert await film._carried_actor(*params) == (picture, False, p)
    assert len(reviews) == 2

    library = {scene: scene for scene in ("home", "track", "street", "office", "protagonist", "authority")}
    library.update(carried_actor=picture, carry_at_feet=False)
    for i in (9, 10, 11, 12, 14, 15, 16, 17, 18, 20, 21, 22, 23, 24, 25):
        assert film._wear_reference(shot(i), library, False) == picture
    for i in (2, 13, 19, 26, 27, 28):
        assert film._wear_reference(shot(i), library, False) is None
    assert film._wear_reference(shot(25), library, True) is None
    assert film._wear_reference(shot(13), {**library, "carry_at_feet": True}, False) == picture
    refs = [img for _, img in film._setup_refs(p, shot(22), library, ["card1", "card2"], {}, "guide")]
    assert picture in refs and "protagonist" not in refs and "card1" not in refs
    refs = [img for _, img in film._setup_refs(p, shot(11), library, ["card1", "card2"], {}, "guide")]
    assert picture in refs and "card1" in refs and "card2" not in refs


@pytest.mark.asyncio
@pytest.mark.parametrize("affected", ["protagonist", "authority"])
async def test_before_receipt_removes_only_a_competing_wearable_from_the_actual_cast(monkeypatch, affected):
    from light import production

    p = plan("wear")
    actors = {"protagonist": "original-lead", "authority": "original-manager"}
    original = dict(actors)
    run, before, after = {}, [], []
    async def see(_ctx, _name, schema, _guide, body, refs, **kwargs):
        assert schema is film.UnwornCast
        before.append((list(refs), body["original_portraits"]))
        ok = len(before) == 2
        return {"report": {"ok": ok, "reason": "Both roles checked separately.", "correction": "",
                           f"{affected}_correction": "" if ok else "Remove the spare cap, keep facial and costume identity."}}
    async def remove(_ctx, _prefix, _name, prompt, refs, **kwargs):
        assert kwargs["language"] == p.language
        after.append(list(refs))
        return "fresh-clean-portrait"
    async def save(_ctx, _prefix, _index, _key, value):
        return value
    monkeypatch.setattr(film.reference_quality, "ask", see)
    monkeypatch.setattr(film, "_still", remove)
    monkeypatch.setattr(production, "save_director_decision", save)
    await film._verify_unworn_cast(None, "job", run, p, actors, ["actual-product"])
    assert before[0][0] == ["original-lead", "original-manager", "actual-product"]
    assert before[1] == ([actors["protagonist"], actors["authority"], "actual-product", original[affected]], [affected])
    assert after == [[original[affected]]] and actors[affected] == "fresh-clean-portrait"


def test_carry_support_and_reachable_product_feature_are_independent_checks():
    from pydantic import ValidationError

    checks = {"ok": True, "reason": "Weighed from its base.", "correction": "", "actor_matches": True,
              "product_matches": True, "support_matches": True, "position_matches": True, "single_product": True,
              "product_at_feet": False, "contact_point_reachable": False}
    with pytest.raises(ValidationError):
        film.CarriedLook.model_validate(checks)
    assert film.CarriedLook.model_validate({**checks, "contact_point_reachable": True}).ok


def test_visible_second_wearable_cannot_pass_merely_because_pickup_is_correct():
    from pydantic import ValidationError

    data = {"ok": True, "row": 1, "reason": "Product is raised.", "correction": "",
            "wear_site": "A second item is visible on the actor.", "competing_item": True}
    with pytest.raises(ValidationError):
        film.FirstWearSelection.model_validate(data)
    data.update(wear_site="Only hands and the support surface are in this closeup.", competing_item=False)
    assert film.FirstWearSelection.model_validate(data).ok


@pytest.mark.asyncio
async def test_frozen_contact_is_checked_against_actual_goods_and_applies_to_each_outgoing_end(monkeypatch):
    from light import production

    p = plan()
    crop_key = f"{film.recipe.REVIEW}:carry-look:{film._fingerprint('body', ['raw'])}"
    run = {"director_decisions": {crop_key: {"report": {"feature_xy": [550, 444]}}}}
    photos, judged = [], []
    async def marked(_ctx, body, xy):
        assert body == "body" and xy == [550, 444]
        return "reference-only-mark"
    async def still(_ctx, _prefix, _name, prompt, refs, **kwargs):
        photos.append(list(refs))
        return f"pose-{len(photos)}"
    async def assess(_ctx, _name, schema, _guide, body, evidence, **kwargs):
        assert schema is film.FingerPose and body["contact"] == p.contact
        judged.append(evidence)
        ok = len(judged) == 2
        return {"report": {"ok": ok, "actual_product": True, "actual_contact": ok, "anatomical_hand": True,
                           "support_and_height": True, "reason": "Finger location checked.",
                           "correction": "" if ok else "Bring the fingertip to the edge."}}
    async def save(_ctx, _prefix, _index, _key, value):
        return value
    monkeypatch.setattr(film, "_target_guide", marked)
    monkeypatch.setattr(film, "_still", still)
    monkeypatch.setattr(film.reference_quality, "ask", assess)
    monkeypatch.setattr(production, "save_director_decision", save)
    result = await film._freeze_contact(None, "job", run, p, "body", ["raw"])
    assert result == "pose-2" and photos == [["body", "reference-only-mark"], ["body", "reference-only-mark", "pose-1"]]
    assert judged == [["pose-1", "body", "raw"], ["pose-2", "body", "raw"]]
    lib = {"contact_actor": result, "carried_actor": "body"}
    for number in (11, 17, 23):
        assert film._wear_reference(shot(number), lib, True) == result
        assert film._wear_reference(shot(number), lib, False) == "body"
    assert film._wear_reference(shot(22), lib, True) == "body"


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed", [False, True])
async def test_running_outfit_changes_only_the_non_product_clothes_and_preserves_both_gesture_cuts(monkeypatch, fixed):
    from light import production

    p = plan("stationary" if fixed else "wear")
    lib = {key: key for key in ["protagonist", "authority", "home", "track", "street", "office"]}
    if not fixed:
        lib.update(carried_actor="commute-in-use", contact_actor="commute-touch")
    run, requests, examined = {}, [], []
    async def still(_ctx, _prefix, _key, prompt, refs, **opts):
        assert "真实" in prompt or "真商品" in prompt
        requests.append(list(refs))
        return f"sport-{len(requests)}"
    async def check(_ctx, _name, schema, _guide, data, images, **opts):
        assert schema is film.TrackCast
        examined.append(list(images))
        return {"report": {"ok": True, "reason": "The actor, wardrobe and product are continuous.", "shoes_box": [210, 785, 734, 986]}}
    async def crop(_ctx, source, box):
        assert source == "sport-1" and box == [210, 785, 734, 986]
        return "only-running-shoes"
    async def save(_ctx, _prefix, _shot, _key, value):
        return value
    monkeypatch.setattr(film, "_still", still)
    monkeypatch.setattr(film, "_shoe_detail", crop)
    monkeypatch.setattr(film.reference_quality, "ask", check)
    monkeypatch.setattr(production, "save_director_decision", save)
    await film._running_cast(None, "p", run, p, lib, ["original-product"])
    assert not requests and not examined
    p.track_wardrobe = "Áo chạy xanh, vẫn mang chính sản phẩm."
    await film._running_cast(None, "p", run, p, lib, ["original-product"])
    if fixed:
        assert requests == [["protagonist"]] and examined == [["sport-1", "protagonist", "original-product"]]
    else:
        assert requests == [["commute-in-use"], ["commute-touch", "sport-1"]]
        assert examined == [["sport-1", "sport-2", "commute-in-use", "commute-touch", "original-product"]]
        assert film._wear_reference(shot(11), lib, True) == "commute-touch"
        assert film._wear_reference(shot(12), lib, False) == film._wear_reference(shot(17), lib, True) == "sport-2"
        assert film._wear_reference(shot(18), lib, False) == film._wear_reference(shot(24), lib, False) == "commute-touch"
        assert film._wear_reference(shot(23), lib, True) == "commute-touch"
    assert film._wear_reference(shot(13), lib, False) == "only-running-shoes"
    assert film._wear_reference(shot(14), lib, False) == "sport-1"
    refs = film._setup_refs(p, shot(12), lib, ["card"], {11: {"review_tail": "commute-cut"}}, "guide")
    assert refs[0][1] == "commute-cut" and "已经穿上该场定妆" in refs[0][0]
    if fixed:
        no_runner = film._setup_refs(p, shot(15), lib, ["card"], {}, "guide")
        assert not any(ref.startswith("sport-") for _, ref in no_runner)


@pytest.mark.asyncio
async def test_actual_six_frame_wardrobe_sheet_preserves_duplicate_frames_in_their_real_slots(monkeypatch):
    from light import production

    p = plan("wear")
    p.protagonist, p.track_wardrobe = "Pink commute shirt", "Navy athletic shirt"
    cuts = {i: {"review_head": f"h{i}", "review_tail": f"t{i}"} for i in range(1, 29)}
    cuts[12]["review_head"] = cuts[18]["review_head"] = "wrong-pink"
    colors = {"t11": (206, 137, 167), "wrong-pink": (206, 137, 167), "t17": (21, 57, 92),
              "t23": (206, 137, 167), "h24": (206, 137, 167)}
    run, invoked = {}, []
    async def download(_ctx, aid, path):
        Image.new("RGB", (544, 306), colors[aid]).save(path, "PNG")
    async def picture(_ctx, name, data):
        with Image.open(BytesIO(data)) as frame:
            assert frame.size == (1088, 984)
            assert frame.getpixel((700, 90))[0] > 190 and frame.getpixel((200, 400))[0] < 33
        return name
    async def audit(_ctx, _name, schema, _guide, _body, refs, **opts):
        invoked.append(refs)
        assert schema is film.WardrobeJoin and len(refs) == 1
        return {"report": {"ok": False, "shot": 12, "reason": "No switch to the established uniform.",
                           "correction": "Use the running outfit for the track entry.", "wardrobe_continuous": False,
                           "wardrobe_sequence": "Pink to pink, navy to pink, and pink to pink."}}
    async def save(_ctx, _prefix, _shot, _key, value):
        return value
    monkeypatch.setattr(film.product_story, "_download", download)
    monkeypatch.setattr(film, "_reference_asset", picture)
    monkeypatch.setattr(film.reference_quality, "ask", audit)
    monkeypatch.setattr(production, "save_director_decision", save)
    key, failure = await film._wardrobe_joins(None, "job", run, p, cuts)
    assert not failure["ok"] and failure["shot"] == 12
    assert await film._wardrobe_joins(None, "job", run, p, cuts) == (key, failure) and len(invoked) == 1


@pytest.mark.parametrize(("mode", "transfer", "opening", "ending", "reaction"), [
    ("carry", "pass", "可见空隙", "承重", "独自持有"),
    ("wear", "fit", "自己实际穿戴", "可轻扶", "特写开头撤手"),
    ("stationary", "access", "使用位", "同一安装处", "支撑位置不动"),
])
def test_product_transfer_and_the_next_closeup_preserve_the_reference_phases(mode, transfer, opening, ending, reaction):
    p = plan(mode)
    p.transfer_mode = transfer
    handoff = film.shot_request("job", p, shot(26), "first", "last", "", 1)
    following = film.shot_request("job", p, shot(27), "first", None, "", 1)
    assert opening in handoff.prompt and ending in handoff.prompt and reaction in following.prompt


@pytest.mark.parametrize("language", ["vi", "en"])
def test_stored_retry_feedback_localizes_older_provider_reasons_and_corrections(language):
    p = plan()
    p.language = language
    feedback = film._rejected(p, shot(17), "第三轮审片失败。", "保留商品，重新拍动作。").review_feedback
    assert not film._has_cjk(feedback["reason"] + feedback["correction"])
    assert ("tư thế mở đầu" if language == "vi" else "opening pose") in feedback["correction"]
    feedback = film._rejected(p, shot(17), "Wrong wrist.", "Keep the left wrist at the belt.").review_feedback
    assert feedback["correction"] == "Keep the left wrist at the belt."


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier_shows_approach", [True, False])
async def test_two_endpoint_handoff_verifies_nine_real_frames_and_reuses_more_of_paid_take(monkeypatch, tmp_path, earlier_shows_approach):
    from video import media_probe

    photo = Image.new("RGB", film.REVIEW_SIZE)
    observed = []
    async def download(_ctx, asset, path):
        if asset == "intended":
            photo.save(path, "PNG")
    async def metadata(*_args, **_kwargs):
        return {"has_audio": False}
    async def decode(*_args):
        return photo.tobytes() * 120
    async def record_picture(_ctx, name, _picture):
        return name
    async def select(_ctx, _name, _schema, _prompt, _body, _refs, **_opts):
        return {"report": {"ok": True, "reason": "Selected a moving row.", "correction": "", "row": 2}}
    async def actual(_ctx, _source, _file, position):
        return f"real-frame-{position}"
    async def examine(_ctx, _plan, _shot, _first, _source, _frames, start, count, _refs):
        observed.append((start, count))
        before = start == 0 and earlier_shows_approach
        return {"board": f"nine-{start}", "report": {"opening": "Hand already holds it." if not before else "There is a gap.",
                "middle": "Both support it.", "ending": "Receiver carries it.", "ok": before, "same_item": True,
                "before_contact": before, "continuous_action": True, "supported": True, "reached": True,
                "reason": "Real first frame inspected.", "correction": "" if before else "Start with a visible gap."}}
    monkeypatch.setattr(film.product_story, "_download", download)
    monkeypatch.setattr(media_probe, "ensure_meta", metadata)
    monkeypatch.setattr(film, "ff", decode)
    monkeypatch.setattr(film, "_physical_options", lambda *_args: [(0, 120), (24, 63)])
    monkeypatch.setattr(film, "_reference_asset", record_picture)
    monkeypatch.setattr(film, "_actual_frame", actual)
    monkeypatch.setattr(film.reference_quality, "ask", select)
    monkeypatch.setattr(film, "_single_performance", examine)
    ctx = SimpleNamespace(user_id="tester")
    result = await film._inspect(ctx, tmp_path, plan(), shot(26, 63), "source", ["opening", "intended"],
                                 {"office": "room"}, ["original"], {})
    assert observed == [(24, 63), (0, 120)] and len(result["performance_checks"]) == 2
    assert result["ok"] is earlier_shows_approach
    if earlier_shows_approach:
        assert result["cut"]["start"] == 0 and result["cut"]["motion_performance"] == "nine-0"
    else:
        assert "cut" not in result and result["correction"] == "Start with a visible gap."


@pytest.mark.asyncio
async def test_resume_rechecks_previously_accepted_handoff_without_refilming(monkeypatch, tmp_path):
    from light import production

    old_key = f"{film.recipe.REVIEW}:take:26:{film._fingerprint(['previous-tail'], '', 1)}:contact-v2"
    run = {"director_decisions": {old_key: {"ok": True, "evidence": ["first", "set", "last", "item", "old-board"],
                                         "cut": {"source": "paid-clip", "start": 29, "frames": 63}}}}
    checks = []
    async def guide(*_args):
        return "guide"
    async def examine(_ctx, _folder, _plan, _shot, source, inputs, *_args):
        checks.append((source, inputs))
        return {"ok": True, "cut": {"source": source, "start": 0, "frames": 124, "motion_performance": "nine-frames"}}
    async def save(_ctx, _prefix, _shot, _key, value):
        return value
    monkeypatch.setattr(film, "_reference_asset", guide)
    monkeypatch.setattr(film, "_inspect", examine)
    monkeypatch.setattr(production, "save_director_decision", save)
    args = (None, tmp_path, "prefix", run, plan(), shot(26, 63), {}, [], {25: {"review_tail": "previous-tail"}}, "")
    new = await film._film(*args)
    assert new["start"] == 0 and new["motion_performance"] == "nine-frames"
    assert await film._film(*args) == new and checks == [("paid-clip", ("first", "last"))]


@pytest.mark.asyncio
@pytest.mark.parametrize(("revision", "case", "recovered"), [
    (2, "visual", True), (1, "visual", False), (2, "no_evidence", False),
    (2, "provider", False), (2, "unknown_submission", False),
])
async def test_only_evidenced_visual_failures_call_one_new_directed_take(monkeypatch, tmp_path, revision, case, recovered):
    import light

    p, s = plan(), shot(22, 77)
    p.score_revision = revision
    cameras, reviews = [], []
    async def photograph(*args, **kwargs):
        cameras.append((args[-1], kwargs))
        if len(cameras) == 1:
            if case == "unknown_submission":
                raise film.RemakeStopped("The outcome is unknown.")
            failed = film._rejected(p, s, "An actual take failed.", "Change the blocking.")
            if case == "provider":
                failed.review_feedback["provider_failure"] = True
            raise failed
        return {"source": "one-directed-take"}
    async def restage(*args, user_revision):
        reviews.append((args[-1].review_feedback["shot"], user_revision))
        return None if case == "no_evidence" else ("Keep the camera low.", [("Real evidence", "source-frame")])
    monkeypatch.setattr(film, "_film", photograph)
    monkeypatch.setattr(light, "commercial_recovery", SimpleNamespace(restage=restage), raising=False)
    args = (None, tmp_path, "job", {}, p, s, {}, [], {}, "Original correction.")
    if recovered:
        result = await film._photograph(*args, adjacent=[("Neighbor", "neighbor")], user_revision=2)
        assert result["source"] == "one-directed-take"
        assert cameras[1] == ("Original correction. Keep the camera low.",
                              {"adjacent": [("Neighbor", "neighbor"), ("Real evidence", "source-frame")],
                               "user_revision": 2, "director_restage": True})
    else:
        with pytest.raises(film.RemakeStopped):
            await film._photograph(*args, adjacent=[], user_revision=2)
        assert len(cameras) == 1
    assert reviews == ([(22, 2)] if case in ("no_evidence", "visual") and revision == 2 else [])


@pytest.mark.asyncio
async def test_restage_cannot_purchase_another_take_for_a_later_seam_on_the_same_shot(monkeypatch, tmp_path):
    from light import production, shot_editor

    run, paid = {}, []
    async def guide(*args):
        return "guide"
    async def frames(*args, **kwargs):
        return "fresh-first", None
    async def publish(*args):
        pass
    async def film_one(_ctx, request, **kwargs):
        paid.append(request.idempotency_key)
        return "exposed-film"
    async def reject(*args, **kwargs):
        return {"ok": False, "reason": "Incorrect prop.", "correction": "Keep the original item in view."}
    async def save(_ctx, _prefix, _index, key, value):
        return run.setdefault("director_decisions", {}).setdefault(key, value)
    monkeypatch.setattr(film, "_frames", frames)
    monkeypatch.setattr(film, "_reference_asset", guide)
    monkeypatch.setattr(film, "_job", film_one)
    monkeypatch.setattr(film, "_inspect", reject)
    monkeypatch.setattr(production, "save_director_decision", save)
    monkeypatch.setattr(shot_editor, "publish_request", publish)
    args = (None, tmp_path, "job", run, plan(), shot(22), {}, [], {})
    for instruction in ("First direction", "First direction", "A different seam reopened this shot"):
        with pytest.raises(film.ReviewRejected):
            await film._film(*args, instruction, director_restage=True)
    assert len(paid) == 1
    assert run["director_decisions"][f"{film.recipe.REVIEW}:recovery-camera:22:r0"]["request_key"] == paid[0]


def _carry_measurement(**observations):
    data = dict(ok=False, reason="The actual contact surface sits one body landmark higher.",
                correction="Lower the supported product a little.", actor_matches=True, product_matches=True,
                support_matches=True, position_matches=False, contact_point_reachable=False, single_product=True,
                product_at_feet=False, feature_xy=[340, 470], landmark_xy=[340, 550],
                anatomical_sides_match=True, named_feature_visible=True, naturally_reachable=True,
                only_height_differs=True, height_offset="above", feature_height_quote="touch the pen tip at waist height",
                observed_feature_height="Upper abdomen, supported in the prescribed hand.")
    return film.MeasuredCarryLook.model_validate({**data, **observations})


@pytest.mark.parametrize("mismatch", [
    {"support_matches": False}, {"product_matches": False}, {"single_product": False},
    {"anatomical_sides_match": False}, {"named_feature_visible": False}, {"naturally_reachable": False},
    {"feature_xy": None}, {"only_height_differs": False}, {"height_offset": "uncertain"},
    {"height_offset": "below"}, {"feature_xy": [340, 545]}])
def test_height_alignment_requires_one_specific_observable_physical_mismatch(mismatch):
    assert _carry_measurement().alignable_direction() == "above"
    assert not _carry_measurement(**mismatch).alignable_direction()


def test_naturally_reachable_feature_can_project_above_a_supported_product():
    with pytest.raises(ValueError, match="explicit direction"):
        _carry_measurement(feature_height_quote="")
    observation = _carry_measurement(ok=True, contact_point_reachable=True, position_matches=True,
                                     only_height_differs=False, height_offset="level", landmark_xy=None,
                                     feature_height_quote="")
    assert observation.ok and observation.feature_xy == [340, 470]
    assert observation.landmark_xy is None and observation.alignable_direction() is None
    quoted = _carry_measurement().model_dump()
    with pytest.raises(ValueError, match="exact wording"):
        film.MeasuredCarryLook.model_validate(quoted, context={"carry": "the whole product rests beside the hip"})
    assert film.MeasuredCarryLook.model_validate(quoted, context={"merchant": {"notes": quoted["feature_height_quote"]}}).alignable_direction() == "above"


@pytest.mark.asyncio
async def test_previously_misread_carry_plate_is_regraded_before_purchasing_another(monkeypatch):
    from unittest.mock import AsyncMock

    p, cast, raw = plan(), "actor", ["user-photo"]
    old = film._fingerprint("plate", raw, p.carry, p.contact, "measured-height-v1")
    key = f"{film.recipe.REVIEW}:carry-look:{old}"
    accepted = _carry_measurement(ok=True, contact_point_reachable=True, position_matches=True,
                                  only_height_differs=False, height_offset="level", landmark_xy=None,
                                  feature_height_quote="")
    run = {"commercial_auto_staging": True, "director_decisions": {
           key + "-unrelated": {"evidence": ["unrelated", cast, *raw]},
           key: {"report": {"ok": False}, "evidence": ["plate", cast, *raw]}}}
    grade, camera = AsyncMock(return_value=accepted), AsyncMock()
    monkeypatch.setattr(film, "_grade_carried_photo", grade)
    monkeypatch.setattr(film, "_still", camera)
    assert await film._carried_actor(None, "job", run, p, cast, [], raw) == ("plate", False, p)
    grade.assert_awaited_once_with(None, "job", run, p, "plate", cast, raw)
    camera.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["first pose", "second pose", "hand never matches", "merchant declined", "regrade failed"])
async def test_carry_alignment_uses_the_first_physically_valid_pose_and_grades_it_again(monkeypatch, case):
    import copy
    from light import commercial_calibration

    p = plan()
    p.protagonist = "Mai"
    adjusted = copy.copy(p)
    adjusted.contact = "Chạm đúng vào ngòi bút ở bụng trên."
    raw, cast, run = ["owned-product-image"], "bare-actor", {"commercial_auto_staging": True}
    photographed, graded, revisions = [], [], []
    async def still(_ctx, _prefix, name, prompt, refs, **opts):
        photographed.append(name)
        return "photo-" + str(len(photographed))
    async def measure(_ctx, _prefix, _run, directions, candidate, source_actor, source_product):
        assert source_actor == cast and source_product == raw
        graded.append((directions.contact, candidate))
        if directions is adjusted:
            return _carry_measurement(ok=case != "regrade failed", contact_point_reachable=case != "regrade failed",
                                      position_matches=case != "regrade failed", only_height_differs=False, height_offset="level")
        if case == "hand never matches" or (candidate == "photo-1" and case == "second pose"):
            return _carry_measurement(anatomical_sides_match=False)
        return _carry_measurement()
    async def align(_ctx, _prefix, live, directions, candidate, source_actor, source_product, evidence):
        assert directions is p and source_actor == cast and source_product == raw
        revisions.append([item["photograph"] for item in evidence["checks"]])
        if case == "merchant declined":
            return None
        live["commercial_carry_calibration"] = {"photograph": candidate, "cast": source_actor, "source_ids": raw, "after_plan_digest": "digest"}
        return adjusted
    async def marker(*_args):
        return "qa-position-guide"
    monkeypatch.setattr(film.recipe, "shots", lambda _: [shot(i) for i in range(1, 29)])
    monkeypatch.setattr(film, "_still", still)
    monkeypatch.setattr(film, "_grade_carried_photo", measure)
    monkeypatch.setattr(film, "_target_guide", marker)
    monkeypatch.setattr(commercial_calibration, "align_carry", align)
    monkeypatch.setattr(commercial_calibration, "_digest", lambda _: "digest")
    if case in {"first pose", "second pose"}:
        usable = "photo-2" if case == "second pose" else "photo-1"
        assert await film._carried_actor(None, "run", run, p, cast, ["card"], raw) == (usable, False, adjusted)
        assert film._carry_grade_key(run, p, usable, raw) != film._carry_grade_key(run, adjusted, usable, raw)
        assert await film._carried_actor(None, "run", run, adjusted, cast, ["card"], raw) == (usable, False, adjusted)
        assert graded[-2:] == [(adjusted.contact, usable)] * 2
        with pytest.raises(film.RemakeStopped):
            await film._carried_actor(None, "run", run, adjusted, cast, ["card"], ["other-product"])
    else:
        with pytest.raises(film.ReviewRejected):
            await film._carried_actor(None, "run", run, p, cast, ["card"], raw)
    assert len(photographed) == (1 if case in {"first pose", "regrade failed"} else 2)
    expected = {"first pose": [["photo-1"]], "second pose": [["photo-2"]], "hand never matches": [],
                "merchant declined": [["photo-1"], ["photo-2"]], "regrade failed": [["photo-1"]]}
    assert revisions == expected[case] and graded[0] == (p.contact, "photo-1")


def test_workplace_contact_sheet_identifies_four_shipped_empty_set_photos():
    import hashlib
    import json

    library = film.GUIDES / "workplaces"
    catalogue = json.loads((library / "sources.json").read_text())
    assert len(catalogue) == 4
    assert Image.open(library / "contact-sheet.jpg").size == (1360, 804)
    for plate in catalogue:
        data = (library / plate["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == plate["sha256"]
        assert plate["source"] and len(plate["source_sha256"]) == 64
        w, h = Image.open(BytesIO(data)).size
        assert w >= 1440 and abs(w / h - 16 / 9) < .01


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [2, None])
async def test_scout_records_the_actual_plate_or_keeps_the_custom_set(monkeypatch, choice):
    import hashlib
    import json

    p = plan()
    p.score_revision = 2
    run = {"commercial_auto_staging": True}
    reviewed, imported = [], []
    async def upload(_ctx, key, data):
        imported.append((key, hashlib.sha256(data).hexdigest()))
        return key
    async def scout(_ctx, name, schema, guide, data, pictures, **options):
        assert data["office"] == p.settings.office and data["merchant"] == {"notes": "a simple workspace"}
        reviewed.append(pictures)
        return {"report": {"location": choice, "merchant_requires_specific_site": False,
                           "empty_of_people_and_featured_goods": True, "setting_matches": bool(choice),
                           "entry_and_transfer_possible": True, "reason": "Desk and door are visible."}}
    async def record(_ctx, _prefix, values, _shot, key, decision):
        values.setdefault("director_decisions", {})[key] = decision
        return decision
    monkeypatch.setattr(film, "_reference_asset", upload)
    monkeypatch.setattr(film.reference_quality, "ask", scout)
    monkeypatch.setattr(film, "_record", record)
    params = (None, "job", run, p, {"notes": "a simple workspace"}, False)
    first = await film._scout_workplace(*params)
    run["wan_cuts"] = [{"shot": 25}]
    assert await film._scout_workplace(*params) == first
    assert len(reviewed) == 1 and reviewed[0] == [imported[0][0]]
    if choice:
        catalogue = json.loads((film.GUIDES / "workplaces/sources.json").read_text())
        assert imported[-1][1] == catalogue[choice - 1]["sha256"]
        assert first == imported[-1][0]
    else:
        assert first is None and len(imported) == 1


@pytest.mark.asyncio
async def test_scout_preserves_the_user_site_installed_goods_and_an_established_custom_room():
    p = plan()
    p.score_revision = 2
    assert await film._scout_workplace(None, "job", {"commercial_auto_staging": True}, p, {}, True) is None
    assert await film._scout_workplace(None, "job", {}, p, {}, False) is None
    assert await film._scout_workplace(None, "job", {"commercial_auto_staging": True, "wan_cuts": [{"shot": 1}]}, p, {}, False) is None
    p.mode = "stationary"
    assert await film._scout_workplace(None, "job", {"commercial_auto_staging": True}, p, {}, False) is None
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        film.WorkplaceChoice(location=1, merchant_requires_specific_site=True, empty_of_people_and_featured_goods=True,
                             setting_matches=True, entry_and_transfer_possible=True, reason="Named merchant location.")


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True])
async def test_render_uses_the_calibrated_script_for_filming_review_and_publication(monkeypatch, resumed):
    from unittest.mock import AsyncMock
    from light import commercial_post, production
    from tests.unit.test_commercial_plan import treatment
    from tool.tool import ToolContext

    before = film.recipe.Plan(**treatment(contact="Touch at the waist."))
    after = before.model_copy(update={"contact": "Touch at the upper abdomen."})
    run = {"job_prefix": "trial", "remake_adaptation": (after if resumed else before).model_dump()}
    if resumed:
        run["commercial_carry_calibration"] = {"photograph": "held", "cast": "actor"}
    seen = set()

    async def cast(_ctx, _prefix, supplied, live, *, form):
        assert supplied == (after if resumed else before)
        live["remake_adaptation"] = after.model_dump()
        return {}, ["original"], after
    async def film_one(_ctx, _folder, _prefix, _run, supplied, item, *_args, **_kwargs):
        seen.add(item["id"])
        assert supplied == after
        if item["id"] in (11, 17, 23):
            assert after.contact in item["action"] and before.contact not in item["action"]
        return {"shot": item["id"], "source": f"source{item['id']}"}
    async def judges(_ctx, _folder, _prefix, _run, supplied, cuts, material, *, cast):
        assert supplied == after and len(cuts) == 28 and material == ["original"] and cast == {}
        assert stages.await_args.args[-1] == "wan_edit"
        return []
    async def deliver(folder, supplied, cuts, *, ctx):
        assert supplied == after and len(cuts) == 28
        assert len(list(folder.glob("source-*.mp4"))) == 28
        return folder / "deliver.mp4", {"frames": 1277}
    async def fetch(_ctx, asset, path):
        path.write_text(asset)
    async def publish(_ctx, _key, _output, supplied, request, measured):
        assert supplied == after and request == {"contact": after.contact} and measured["frames"] == 1277
        return SimpleNamespace(output_asset_id="result")
    monkeypatch.setattr(production, "begin_reference_edit", AsyncMock(return_value=run))
    monkeypatch.setattr(production, "save_wan_edit", AsyncMock(return_value=run))
    stages = AsyncMock()
    monkeypatch.setattr(production, "remake_stage", stages)
    monkeypatch.setattr(production, "save_reference_cut", AsyncMock())
    monkeypatch.setattr(film, "_approved_edit", lambda *_: None)
    monkeypatch.setattr(film, "_cast_and_sets", cast)
    monkeypatch.setattr(film, "_photograph", film_one)
    monkeypatch.setattr(film, "_check_joins", judges)
    monkeypatch.setattr(film, "_existing_job", AsyncMock(return_value=None))
    monkeypatch.setattr(film.recipe, "final_request", lambda _cuts, supplied: {"contact": supplied.contact})
    monkeypatch.setattr(film.product_story, "_download", fetch)
    monkeypatch.setattr(commercial_post, "assemble", deliver)
    monkeypatch.setattr(film.product_story, "_publish", publish)
    monkeypatch.setattr(film.recipe, "is_complete_cut", lambda live, _job: live["remake_adaptation"] == after.model_dump())
    monkeypatch.setattr(film.vp, "_attach_completed", AsyncMock(return_value=True))
    assert await film.render(ToolContext(), {}, run, before) == ("result", 1277 / 24)
    assert seen == set(range(1, 29))
    assert [call.args[-1] for call in stages.await_args_list] == ["remake_frames", "wan_shots", "wan_edit", "compose"]


@pytest.mark.asyncio
async def test_requested_retry_of_failed_calibrated_photo_photographs_the_same_corrected_plan(monkeypatch):
    from unittest.mock import AsyncMock
    from light import commercial_calibration

    p = plan()
    p.protagonist = "Mai"
    run = {"commercial_auto_staging": True, "commercial_carry_calibration": {"photograph": "existing", "cast": "actor",
           "source_ids": ["raw"], "after_plan_digest": "digest"},
           "commercial_repairs": [{"target": 10, "revision": 2, "report": {"correction": "Expose the actual tip."}}]}
    async def check(_ctx, _prefix, _run, supplied, candidate, *_):
        assert supplied is p
        if candidate == "existing":
            return _carry_measurement(named_feature_visible=False)
        return _carry_measurement(ok=True, position_matches=True, contact_point_reachable=True,
                                  only_height_differs=False, height_offset="level")
    generate = AsyncMock(return_value="new-plate")
    align = AsyncMock()
    monkeypatch.setattr(film, "_grade_carried_photo", check)
    monkeypatch.setattr(film, "_still", generate)
    monkeypatch.setattr(commercial_calibration, "_digest", lambda *_: "digest")
    monkeypatch.setattr(commercial_calibration, "align_carry", align)
    result = await film._carried_actor(None, "job", run, p, "actor", ["card"], ["raw"])
    assert result == ("new-plate", False, p) and "Expose the actual tip." in generate.await_args.args[3]
    align.assert_not_awaited()


@pytest.mark.parametrize("mode,transfer", [("carry", "pass"), ("wear", "fit"), ("stationary", "access")])
def test_confrontation_transfer_and_reaction_get_only_their_own_film_phase(mode, transfer):
    from pydantic import ValidationError
    p = plan(mode)
    p.transfer_mode = transfer
    p.handoff_state = "State only reached after transfer."
    p.handoff_preparation = "Keep original control while the authority stands."
    stages = film._transfer(p)
    early = film._office_shot_phases(p, 25)
    assert early == {"preparation_during_rebuke": p.handoff_preparation, "required_cut_before_transfer": stages["opening_phase"]}
    assert stages["ending_phase"] not in str(early) and p.handoff_state not in str(early)
    exchange = film._office_shot_phases(p, 26)
    assert exchange["opening_phase"] == stages["opening_phase"] and exchange["ending_phase"] == stages["ending_phase"]
    assert film._office_shot_phases(p, 27) == {"reaction_after_transfer": stages["next_opening"]}
    assert film._office_shot_phases(p, 24) == {}
    evidence = dict(ok=True, row=1, reason="The authority confronts the protagonist.",
                    control_at_cut="Already controlled by the other actor.", advanced_to_transfer=True)
    with pytest.raises(ValidationError):
        film.ConfrontationSelection(**evidence)
    assert film.ConfrontationSelection(**{**evidence, "control_at_cut": "Still with the protagonist.", "advanced_to_transfer": False}).ok


@pytest.mark.asyncio
@pytest.mark.parametrize("number", [2, 25])
@pytest.mark.parametrize("same_receipt", [True, False])
async def test_recut_of_obsolete_phase_review_uses_only_the_same_paid_camera_and_assets(monkeypatch, tmp_path, number, same_receipt):
    from unittest.mock import AsyncMock

    p = plan("wear")
    staged = shot(number)
    run = {"director_decisions": {}}
    before = dict(ok=False, evidence=["opening", "room", "wardrobe", "contact-sheet"],
                  report=dict(ok=False, row=1, reason="Legacy cut point."), reason="Legacy cut point.")
    record_key = "commercial-reference-v4:take:25:old"
    paid = SimpleNamespace(status="completed", output_asset_id="paid-motion",
                           request_data={"input_asset_ids": ["opening" if same_receipt else "different-opening"]})
    lookup = AsyncMock(return_value=paid)
    inspect = AsyncMock(return_value={"ok": True, "cut": {"source": "paid-motion"}})
    async def keep(_ctx, _prefix, data, i, key, value):
        assert i == number
        data["director_decisions"][key] = value
        return value
    monkeypatch.setattr(film, "_existing_job", lookup)
    monkeypatch.setattr(film, "_inspect", inspect)
    monkeypatch.setattr(film, "_record", keep)
    args = (None, tmp_path, "job", run, p, staged, {}, [], {}, record_key, before, "", 1, 0)
    first = await film._recut_checkpoint(*args)
    assert lookup.await_args_list[0].args[-1] == film.shot_request("job", p, staged, "opening", None, "", 1).idempotency_key
    if same_receipt:
        assert first["cut"] == {"source": "paid-motion", "take": 1} and first["replaces_review"] == record_key
        assert await film._recut_checkpoint(*args) == first
        inspect.assert_awaited_once()
        assert lookup.await_count == 1
    else:
        assert first is before and not run["director_decisions"]
        inspect.assert_not_awaited()
    present = {**before, "report": {**before["report"], "control_at_cut": "Actor left.", "competing_item": True}}
    assert not film._obsolete_selection(p, number, present)


@pytest.mark.asyncio
async def test_ui_retry_reassesses_original_paid_confrontation_before_drawing_anything(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    p = plan("wear")
    p.score_revision = 2
    note = "Giữ nguyên góc máy và bối cảnh."  # The standard Retry carries the saved reviewer note.
    key = f"{film.recipe.REVIEW}:take:25:first"
    old = {"ok": False, "report": {"ok": False, "row": 1}, "evidence": ["opening", "room", "original"]}
    run = {"director_decisions": {key: old}, "commercial_repairs": [
        {"target": 25, "revision": 1, "report": {"kind": "commercial", "shot": 25, "correction": note}}]}
    paid = SimpleNamespace(status="completed", output_asset_id="original-paid", request_data={"input_asset_ids": ["opening"]})
    receipt = AsyncMock(return_value=paid)
    grading = AsyncMock(return_value={"ok": True, "report": {"row": 1, "control_at_cut": "Protagonist still wears it."},
                                      "cut": {"source": "original-paid"}})
    async def record(_ctx, _prefix, data, _idx, side, value):
        return data["director_decisions"].setdefault(side, value)
    monkeypatch.setattr(film, "_existing_job", receipt)
    monkeypatch.setattr(film, "_inspect", grading)
    monkeypatch.setattr(film, "_record", record)
    camera = AsyncMock(side_effect=AssertionError("No paid camera needed"))
    monkeypatch.setattr(film, "_frames", camera)
    args = (None, tmp_path, "job", run, p, shot(25), {}, [], {})
    assert await film._film(*args, note, user_revision=1) == {"source": "original-paid", "take": 1}
    assert await film._film(*args, note, user_revision=1) == {"source": "original-paid", "take": 1}
    assert not receipt.call_args.args[-1].endswith(":revision1")
    assert await film._historic_phase_cut(*args, "Change the shot for the merchant", 1) is None
    run["wan_final_key"] = "published"
    assert await film._historic_phase_cut(*args, note, 1) is None
    camera.assert_not_awaited()
    grading.assert_awaited_once()
    receipt.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("usable", [True, False])
async def test_migrate_previously_accepted_first_match_through_exact_directed_take(monkeypatch, tmp_path, usable):
    from unittest.mock import AsyncMock

    p = plan("wear")
    p.score_revision = 2
    key = f"{film.recipe.REVIEW}:take:11:directed:contact-v2"
    old = {"ok": True, "cut": {"source": "directed-take", "take": 1, "review_board": "old"},
           "evidence": ["start", "set", "end", "reference"], "report": {"ok": True, "row": 2}}
    ticket = {"setup": ["start", "end"], "request_key": "job:shot11:owned-directed-camera"}
    run = {"director_decisions": {key: old, "ticket": ticket}}
    async def paid(_ctx, kind, camera_key):
        if camera_key == ticket["request_key"]:
            return SimpleNamespace(status="completed", output_asset_id="directed-take",
                                   request_data={"input_asset_ids": ["start", "end"]})
    result = {"ok": usable, "report": {"row": 1, "has_clear_approach": usable},
              "cut": {"source": "directed-take", "review_board": "new"}}
    grading = AsyncMock(return_value=result)
    async def record(_ctx, _prefix, data, _idx, side, value):
        return data["director_decisions"].setdefault(side, value)
    monkeypatch.setattr(film, "_existing_job", paid)
    monkeypatch.setattr(film, "_inspect", grading)
    monkeypatch.setattr(film, "_record", record)
    args = (None, tmp_path, "job", run, p, shot(11, 14), {}, [], {}, "", 0)
    expected = {"source": "directed-take", "take": 1, "review_board": "new"} if usable else None
    assert await film._historic_phase_cut(*args) == expected
    assert await film._historic_phase_cut(*args) == expected
    grading.assert_awaited_once()
    assert run["director_decisions"][key] == old
    assert run["director_decisions"][f"{key}:recut-first-match-entry-v1"]["ok"] is usable


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["unchanged", "new entry", "new tail", "rejected"])
async def test_rechecking_saved_first_match_reopens_only_the_affected_unpublished_cuts(monkeypatch, tmp_path, phase):
    from unittest.mock import AsyncMock

    p = plan()
    p.score_revision = 2
    key = f"{film.recipe.REVIEW}:take:11:historic"
    old = {"shot": 11, "start": 10, "frames": 14, "output_frames": 14, "speed": 1.,
           "source": "owned", "review_board": "old", "review_head": "f10", "review_tail": "f23"}
    old_decision = {"ok": True, "evidence": ["start", "set", "end", "board"], "cut": old, "report": {"row": 1}}
    run = {"director_decisions": {key: old_decision}, "wan_cuts": [old]}
    cuts = {11: old, 12: {"shot": 12, "source": "neighbor"}, 18: {"shot": 18, "source": "still-valid"}}
    assert film._unpublished_first_match({**run, "wan_final_key": "published"}, p, cuts) is None
    legacy = film._unpublished_first_match(run, p, cuts)
    assert legacy == (key, old_decision)
    new = old if phase == "unchanged" else {**old, "start": 3, "frames": 21, "speed": 1.5, "review_head": "f03",
            "review_board": "new", "review_tail": "f24" if phase == "new tail" else "f23"}
    result = {"ok": phase != "rejected", "cut": new, "report": {"reason": "Observed the entry and arrival."},
              "replaces_review": key}
    async def recut(*args):
        run["director_decisions"][f"{key}:recut-first-match-entry-v1"] = result
        return result
    reopen = AsyncMock()
    monkeypatch.setattr(film, "_recut_checkpoint", recut)
    monkeypatch.setattr(film.commercial_frame_audit, "reopen_saved", reopen)
    await film._recheck_saved_match(None, tmp_path, "job", run, p, shot(11, 14), {}, [], cuts, legacy)
    if phase == "unchanged":
        reopen.assert_not_awaited()
        assert film._unpublished_first_match(run, p, cuts) is None
    else:
        assert reopen.await_args.args[-2] == ({11} if phase == "new entry" else {11, 12})
        assert reopen.await_args.kwargs["verified_first_match"]["key"] == f"{key}:recut-first-match-entry-v1"
    assert (11 in cuts) == (phase != "rejected") and (12 in cuts) == (phase in {"unchanged", "new entry"})
    assert cuts[18]["source"] == "still-valid" and run["director_decisions"][key]["cut"] == old


@pytest.mark.asyncio
async def test_neutral_cast_keeps_the_supporting_hand_and_uses_distinct_pose_anchors(monkeypatch):
    p = plan("wear")
    p.score_revision = 2
    p.protagonist = "Blue jacket"
    p.track_wardrobe = "Yellow running shirt"
    library = {"protagonist": "actor", "carried_actor": "already-touching", "track_actor": "running-at-rest",
               "contact_actor": "valid-finger-arrived", "track_contact": "running-finger-arrived", "track_feet": "feet"}
    run = {}
    graded, drawn = [], []
    async def grade(_ctx, name, schema, guide, body, refs, **kw):
        assert name == "commercial_resting_pose" and schema is film.RestingLook
        assert "活动手本身必须" in guide and body["future_gesture"] == p.contact
        graded.append(tuple(refs))
        ok = refs[0] != "already-touching"
        return {"evidence": refs, "report": {"ok": ok, "reason": "Examined resting pose.",
                           "correction": "" if ok else "Lower the free wrist and keep the product on the body.",
                           "gesture_hand_at_rest": ok, "same_person_item_and_support": True,
                           "actual_posture": "The non-supporting arm is down." if ok else "The finger is already at the contact."}}
    async def photograph(_ctx, _prefix, name, prompt, refs, **kw):
        drawn.append((refs, prompt, kw))
        return "commute-at-rest"
    async def record(_ctx, _prefix, data, _idx, key, result):
        return data.setdefault("director_decisions", {}).setdefault(key, result)
    monkeypatch.setattr(film.reference_quality, "ask", grade)
    monkeypatch.setattr(film, "_still", photograph)
    monkeypatch.setattr(film, "_record", record)
    args = (None, "job", run, p, library, ["original-product"], ["clean-card"])
    await film._resting_costumes(*args)
    assert len(drawn) == 1 and drawn[0][0] == ["already-touching", "actor", "clean-card"]
    assert "其余指和手掌保持原有必要的握持" in drawn[0][1] and drawn[0][2]["language"] == "vi"
    assert len(graded) == 3 and all("original-product" in refs for refs in graded)
    assert film._wear_reference(shot(11), library, False) == "commute-at-rest"
    assert film._wear_reference(shot(11), library, True) == "valid-finger-arrived"
    assert film._wear_reference(shot(12), library, False) == "running-finger-arrived"
    assert film._wear_reference(shot(17), library, False) == "running-at-rest"
    assert film._wear_reference(shot(17), library, True) == "running-finger-arrived"
    assert film._wear_reference(shot(13), library, False) == "feet"
    assert film._wear_reference(shot(25), library, False) == "commute-at-rest"
    await film._resting_costumes(*args)
    p.mode = "stationary"
    await film._resting_costumes(*args)
    assert len(graded) == 3 and len(drawn) == 1


def test_first_match_keeps_earlier_arm_entrance_when_camera_travels_after_arrival(monkeypatch):
    frames = [Image.new("RGB", (32, 18), (40, 42, 45)) for _ in range(90)]
    deltas = [.5] * 89
    deltas[13:20] = [3., 5., 7., 8., 7., 5., 3.]
    monkeypatch.setattr(film, "_motion_deltas", lambda actual: deltas)
    monkeypatch.setattr(film, "_arrival_windows", lambda *args: [(32, 21), (36, 14)])
    ranges = film._physical_options(frames, shot(11, 14), None)
    assert any(first <= 12 and first + count - 1 >= 28 and count <= 28 for first, count in ranges)
    assert (32, 21) in ranges and len(ranges) == 4
    assert (32, 21) == film._physical_options(frames, shot(17, 52), None)[0]


def test_exact_first_match_playback_requires_a_real_intermediate_phase_and_natural_rate():
    from pydantic import ValidationError

    good = dict(ok=True, reason="The hand enters, travels, and reaches the visible target.", clear_first=True,
                seen_entering=True, continuous_reach=True, natural_timing=True, arrived_at_cut=True,
                first_entry=2, first_contact=11, observed_path="F2 enters, F5 crosses, F11 arrives.")
    assert film.MatchPlayback.model_validate(good).ok
    for change in ({"first_contact": 2}, {"natural_timing": False}, {"continuous_reach": False},
                   {"clear_first": False}, {"arrived_at_cut": False}, {"first_entry": None}):
        with pytest.raises(ValidationError):
            film.MatchPlayback.model_validate({**good, **change})
    old = {"ok": False, "row": 4, "has_clear_approach": False, "reached_feature": True}
    p = plan()
    p.score_revision = 2
    prior = {"ok": False, "report": old, "evidence": ["entry", "choices"]}
    assert film._obsolete_selection(p, 11, prior) == "first-match-window"
    assert not film._obsolete_selection(p, 11, {**prior, "entry_window_version": 2})
    short = film.MatchPlayback.model_validate({**good, "ok": False, "arrived_at_cut": False, "first_contact": None,
                                              "correction": "Hold longer for contact."})
    assert film._finish_entry_windows(10, 21, 124, 14, short) == [(10, 28), (13, 28)]
    assert not film._finish_entry_windows(10, 21, 124, 14, film.MatchPlayback.model_validate(good))


def test_normal_playback_can_cut_a_held_match_and_a_clean_wide_before_a_costly_reshoot():
    p = plan()
    p.score_revision = 2
    deltas = [.8] * 123
    deltas[46:59] = [25.] * 13  # The unwanted late dissolve is the most energetic region.
    matching = film._windows(shot(12, 17), 124, deltas, plan=p)
    assert (8, 26) in matching and (0, 17) in matching
    wide = film._windows(shot(16, 27), 124, deltas, plan=p)
    assert wide[:2] == [(0, 27), (13, 27)] and len(wide) <= 4
    assert (0, 17) == film._windows(shot(12, 17), 124, deltas, plan=plan())[0]


def test_opening_uses_look_before_pickup_and_offscreen_product_is_acceptable():
    from pydantic import ValidationError, create_model

    schema = create_model("OpeningWearTest", __base__=(film.BeforePickup, film.PlaybackDecision, film.FirstWearSelection))
    opening = {"ok": True, "reason": "The actor turns towards the window.", "row": 1,
               "wear_site": "Bare head.", "competing_item": False, "product_before_cut": "out_of_frame"}
    assert schema.model_validate(opening).ok
    with pytest.raises(ValidationError):
        schema.model_validate({**opening, "product_before_cut": "already_lifted_or_worn"})
    with pytest.raises(ValidationError):
        schema.model_validate({**opening, "ok": False, "correction": "Touch closer to the geometric center."})
    p = plan("wear")
    p.score_revision = 2
    assert (0, 82) in film._windows(shot(1, 102), 124, [1.] * 123, plan=p)
    assert film.BEFORE_PICKUP in film.shot_request("job", p, shot(1, 102), "head", None, "", 1).prompt
    assert film.BEFORE_PICKUP not in film.shot_request("job", p, shot(2), "head", None, "", 1).prompt


@pytest.mark.parametrize("mode", ["wear", "carry", "stationary"])
def test_closing_travel_gear_preserves_the_actual_product_including_when_the_gear_is_the_product(mode):
    from pydantic import ValidationError

    p, s = plan(mode), shot(9, 23)
    p.score_revision = 2
    s["product_visible"] = False
    lib = {"protagonist": "actor", "home": "kitchen"}
    if mode != "stationary":
        lib["carried_actor"] = "actor-actually-using-it"
    prompt = film.shot_request("job", p, s, "start", None, "", 1).prompt
    assert "扣件" in prompt and ("留在原安装处" in prompt if mode == "stationary" else p.carry in prompt)
    refs = film._setup_refs(p, s, lib, ["product-card"], {8: {"review_tail": "actually-worn-at-cut"}}, "blocking")
    assert refs[0][1] == "actually-worn-at-cut"
    for state in ("in_planned_position", "out_of_frame", "closing_the_product_itself"):
        assert film.DepartureSelection(ok=True, row=1, reason="The fastener closes.", product_state=state).ok
    with pytest.raises(ValidationError):
        film.DepartureSelection(ok=True, row=1, reason="Object put in the wrong bag.", product_state="visibly_displaced_or_duplicated")


@pytest.mark.asyncio
async def test_historical_departure_uses_the_owned_third_take_and_the_actual_preceding_product_state(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    p, s = plan("wear"), shot(9, 23)
    p.score_revision = 2
    note = "Keep the item on the wearer."
    old = {"ok": True, "report": {"ok": True, "row": 2}, "source": "the-real-clip", "setup": ["head", None],
           "evidence": ["head", "room", "old-sheet"], "cut": {"take": 3, "source": "the-real-clip"}}
    run = {"director_decisions": {f"{film.recipe.REVIEW}:take:9:old": old}, "commercial_repairs": [
        {"target": 9, "revision": 1, "report": {"kind": "commercial", "correction": note}}]}
    job_key = "job:shot9:c1234567890abcdef:3"
    lookup = AsyncMock(return_value=(job_key, 3))
    paid = SimpleNamespace(status="completed", output_asset_id="the-real-clip", request_data={"input_asset_ids": ["head"]})
    current = {8: {"review_tail": "the-real-wearer"}}
    grading = AsyncMock(return_value={"ok": True, "report": {"ok": True, "row": 2, "product_state": "out_of_frame"},
                                      "cut": {"source": "the-real-clip"}})
    async def record(_ctx, _prefix, data, _idx, key, val):
        return data["director_decisions"].setdefault(key, val)
    receipt = AsyncMock(return_value=paid)
    monkeypatch.setattr(film, "_camera_key_for_source", lookup)
    monkeypatch.setattr(film, "_existing_job", receipt)
    monkeypatch.setattr(film, "_inspect", grading)
    monkeypatch.setattr(film, "_record", record)
    args = (None, tmp_path, "job", run, p, s, {}, ["merchant-photo"], current)
    assert await film._historic_phase_cut(*args, note, 1) == {"source": "the-real-clip", "take": 3}
    assert await film._historic_phase_cut(*args, note, 1) == {"source": "the-real-clip", "take": 3}
    assert await film._historic_phase_cut(*args, "Merchant changed the bag", 1) is None
    grading.assert_awaited_once()
    assert grading.await_args.args[-1] == current and receipt.await_args.args[-1] == job_key


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["clean_earlier", "same_interval", "wrong_edge", "unrelated", "published", "still_held"])
async def test_home_seam_reuses_only_a_different_paid_opening_with_the_matching_actual_next_shot(monkeypatch, tmp_path, variant):
    from unittest.mock import AsyncMock

    p = plan("wear")
    p.score_revision = 2
    old = {"shot": 1, "source": "old-paid", "start": 0, "frames": 102, "output_frames": 102, "speed": 1.,
           "review_board": "old-actual-board", "take": 1}
    before = {"ok": True, "evidence": ["opening", "room"], "cut": old}
    new = {**old, "frames": 82, "speed": 82 / 102, "review_board": "earlier-actual-board"}
    seam = {"ok": False, "after": 1, "shot": 1, "outgoing": "Product in hand.", "incoming": "Product back on the table.",
            "cut_assets": ["old-actual-board", "pickup-board"]}
    if variant == "unrelated":
        seam["after"] = 4
    run = {"director_decisions": {f"{film.recipe.REVIEW}:scene:home:abc": seam, f"{film.recipe.REVIEW}:take:1:abc": before}}
    if variant == "published":
        run["wan_final_key"] = "published"
    checked = AsyncMock(return_value={"ok": variant != "still_held", "report": {"product_before_cut": "at_rest"},
                                      "cut": old if variant == "same_interval" else new})
    monkeypatch.setattr(film, "_recut_checkpoint", checked)
    cuts = {2: {"review_head": "real-in", "review_board": "pickup-board"}}
    adjacent = [("next shot", "different-in" if variant == "wrong_edge" else "real-in")]
    result = await film._recut_repeated_pickup(None, tmp_path, "job", run, p, shot(1), {}, [], cuts, adjacent)
    assert result == (new if variant == "clean_earlier" else None)
    assert checked.await_count == int(variant in ("clean_earlier", "same_interval", "still_held"))


@pytest.mark.asyncio
async def test_new_handoff_editor_checks_nine_actual_frames_per_candidate_in_one_viewing(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from video import media_probe

    p = plan()
    p.score_revision = 2
    photo = Image.new("RGB", film.REVIEW_SIZE)
    queried = []
    async def download(_ctx, aid, path):
        if aid == "ending":
            photo.save(path, "PNG")
    async def asset(_ctx, name, _picture):
        return name
    async def select(_ctx, _name, schema, prompt, body, refs, **_opts):
        queried.append(refs)
        assert film.PLAYBACK_STANDARD in prompt and issubclass(schema, film.EnactedSelection)
        assert body["available_rows"] == [1, 2] and "ROW" in prompt
        assert refs[-2].endswith("-0-120-r1") and refs[-1].endswith("-24-63-r2")
        fields = {"row": 1, "ok": True, "reason": "The first shows the complete exchange.",
                  "opening": "Separate hands.", "middle": "Both hands support one item.", "ending": "Receiver bears it.",
                  "same_item": True, "before_contact": True, "continuous_action": True, "supported": True, "reached": True}
        return {"report": schema.model_validate(fields).model_dump(), "evidence": refs}
    monkeypatch.setattr(film.product_story, "_download", download)
    monkeypatch.setattr(film, "ff", AsyncMock(return_value=photo.tobytes() * 120))
    monkeypatch.setattr(media_probe, "ensure_meta", AsyncMock(return_value={"has_audio": False}))
    monkeypatch.setattr(film, "_physical_options", lambda *_: [(0, 120), (24, 63)])
    monkeypatch.setattr(film, "_reference_asset", asset)
    monkeypatch.setattr(film, "_actual_frame", AsyncMock(return_value="recorded-frame"))
    monkeypatch.setattr(film.reference_quality, "ask", select)
    extra_review = AsyncMock(side_effect=AssertionError("The selected nine-frame action was already reviewed"))
    monkeypatch.setattr(film, "_single_performance", extra_review)
    result = await film._inspect(SimpleNamespace(user_id="tester"), tmp_path, p, shot(26, 63), "paid",
                                 ["first", "ending"], {"office": "staged-office"}, ["actual-product"], {})
    assert len(queried) == 1 and result["ok"] and result["playback_review"] == film.PLAYBACK_REVIEW
    assert result["cut"]["start"] == 0 and result["cut"]["motion_performance"] == queried[0][-2]
    assert result["performance_checks"][0]["frames"] == [0, 15, 30, 45, 60, 74, 89, 104, 119]
    extra_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_grounded_pair_reviews_send_the_good_opening_to_the_camera_without_more_redraws(monkeypatch):
    from unittest.mock import AsyncMock

    p = plan()
    p.score_revision = 2
    run = {}
    stills = AsyncMock(side_effect=["first", "last", "retouched"])
    review = AsyncMock(return_value={"report": {**GROUNDED, "ok": False, "reason": "Entire fingertip is on the other end.",
                                                "visible_problem": "missing_action_or_broken_cut",
                                                "first_correction": "", "last_correction": "Move from the other end."}})
    async def record(_ctx, _prefix, _r, _idx, key, value):
        return run.setdefault("director_decisions", {}).setdefault(key, value)
    monkeypatch.setattr(film, "_still", stills)
    monkeypatch.setattr(film.reference_quality, "ask", review)
    monkeypatch.setattr(film, "_record", record)
    result = await film._frames(None, "job", p, shot(17, 52), {"track": "set", "protagonist": "actor",
                                "product_cards": ["card"]}, ["actual"], {}, "guide", "", run)
    assert result == ("first", None) and stills.await_count == 3 and review.await_count == 2
    assert all(film.PLAYBACK_STANDARD in c.args[3] for c in review.await_args_list)


def test_seventeenth_landing_uses_the_frozen_contact_as_primary_and_preserves_frontal_view():
    p = plan("wear")
    s = shot(17, 52)
    lib = {"track": "road", "protagonist": "lead", "track_actor": "running", "track_contact": "verified-touch"}
    ending = film._setup_refs(p, s, lib, ["real-product"], {}, "layout", "start", ending=True)
    assert [asset for _, asset in ending][:3] == ["verified-touch", "start", "road"]
    assert "正面中景" in ending[0][0] and "平面横坐标" in ending[0][0]
    assert "verified-touch" not in [id for _, id in film._setup_refs(p, s, lib, ["real-product"], {}, "layout")]


@pytest.mark.asyncio
@pytest.mark.parametrize("recognizable", [True, False])
async def test_track_landing_only_repositions_a_visibly_identified_part_in_the_actual_front_frame(monkeypatch, recognizable):
    from unittest.mock import AsyncMock

    p = plan("wear")
    library = {"track": "track-set", "track_contact": "frozen-pose"}
    answer = {"report": {"same_cast_support_and_front_camera": True, "only_fingertip_is_misplaced": recognizable,
                         "identifiable_landmark": "A visible rim seam in the front camera.",
                         "visible_tip": [321, 470], "actual_target": [427, 450] if recognizable else None}}
    audit = AsyncMock(return_value=answer)
    still = AsyncMock(return_value="corrected-front")
    async def record(_ctx, _prefix, data, _i, key, result):
        return data.setdefault("director_decisions", {}).setdefault(key, result)
    monkeypatch.setattr(film.reference_quality, "ask", audit)
    monkeypatch.setattr(film, "_record", record)
    monkeypatch.setattr(film, "_edit_track_finger", still)
    actual = await film._retarget_track_landing(None, "job", {}, p, shot(17, 52), "actual-front", library,
                                                ["real-product"], "Move from the side to the visible seam.", 0)
    assert audit.await_args.args[-1] == ["actual-front", "frozen-pose", "track-set", "real-product"]
    if recognizable:
        assert actual == "corrected-front"
        assert still.await_args.args[3:5] == ("actual-front", "frozen-pose")
        assert still.await_args.args[5].local_shift() == ((321, 470), (427, 450))
    else:
        assert actual is None
        still.assert_not_awaited()


def test_local_finger_patch_preserves_every_pixel_away_from_the_moving_phalanx():
    photo = Image.new("RGB", (1000, 560), (10, 21, 32))
    box, cropped, annotated, mask = film._finger_patch(photo, (460, 250), (490, 230), (410, 310))
    assert box[2] - box[0] == box[3] - box[1] and cropped.size == annotated.size
    assert 0 < box[0] < 410 < 490 < box[2] < 1000 and mask.getextrema() == (0, 255)
    changed = photo.copy()
    changed.paste(Image.new("RGB", cropped.size, (180, 150, 140)), box, mask)
    assert changed.getpixel((490, 129)) == (180, 150, 140)
    assert changed.getpixel((50, 50)) == changed.getpixel((940, 510)) == changed.getpixel((470, 224)) == (10, 21, 32)


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen", [False, True])
async def test_track_costume_starts_with_resting_commute_or_recovers_the_existing_approved_pair(monkeypatch, frozen):
    from unittest.mock import AsyncMock

    p = plan("wear")
    p.track_wardrobe = "A yellow running shirt and gray shoes."
    lib = {"carried_actor": "old-commute", "resting_carried_actor": "resting-commute", "protagonist": "face",
           "contact_actor": "verified-touch"}
    pics = ["old-track", "old-track-pose", "old-commute", "verified-touch", "product"]
    report = {"ok": True, "reason": "Wardrobe and carried product match.", "neutral_correction": "",
              "pose_correction": "", "shoes_box": [300, 880, 600, 970]}
    run = {"director_decisions": ({f"{film.recipe.REVIEW}:track-wardrobe:{film._fingerprint(pics,p.track_wardrobe)}":
                                   {"evidence": pics, "report": report}} if frozen else {})}
    camera = AsyncMock(side_effect=["new-track", "new-track-pose"])
    audit = AsyncMock(return_value={"report": report})
    save = AsyncMock(side_effect=lambda _ctx, _pfx, _run, _idx, _key, answer: answer)
    monkeypatch.setattr(film, "_still", camera)
    monkeypatch.setattr(film, "_record", save)
    monkeypatch.setattr(film.reference_quality, "ask", audit)
    monkeypatch.setattr(film, "_shoe_detail", AsyncMock(return_value="shoes"))
    await film._running_cast(None, "job", run, p, lib, ["product"])
    assert lib["track_feet"] == "shoes" and lib["track_contact"] == ("old-track-pose" if frozen else "new-track-pose")
    if frozen:
        camera.assert_not_awaited()
        audit.assert_not_awaited()
    else:
        assert camera.await_args_list[0].args[-1] == ["resting-commute"]
        assert camera.await_args_list[1].args[-1] == ["verified-touch", "new-track"]
