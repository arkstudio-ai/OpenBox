from copy import deepcopy
from types import SimpleNamespace

import pytest
from PIL import Image

from light import commercial_film as film
from light import commercial_frame_audit as pixels
from light import commercial_playback_review as review


class Plan(SimpleNamespace):
    def model_dump(self):
        return vars(self)


def fixture(monkeypatch):
    p = Plan(format=review.recipe.FORMAT, score_revision=2, language="vi", mode="carry", product="Bình", carry="Ở bên hông.", transfer_mode="pass", track_wardrobe="")
    shots = [{"id": i, "action": f"Thao tác {i}", "frame": "Ban đầu", "end": "Cuối cảnh", "product_visible": True}
             for i, _ in enumerate(review.recipe.BEATS, 1)]
    cuts = {i: {"shot": i, "source": f"camera-{i}", "review_board": f"board-{i}", "start": 2, "frames": beat.frames,
                "output_frames": beat.frames, "speed": 1.0, "review_version": review.recipe.REVIEW}
            for i, beat in enumerate(review.recipe.BEATS, 1)}
    monkeypatch.setattr(review.recipe, "shots", lambda _: shots)
    return p, cuts


def observed(concern=None):
    return {"observations": [{"shot": i, "visible": f"Saw the photographed action in shot {i}."} for i in review.ANCHORS],
            "concern": concern}


async def record(ctx, prefix, run, number, key, decision):
    return run.setdefault("director_decisions", {}).setdefault(key, decision)


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_older_seams", [False, True])
async def test_normal_delivery_and_crash_recovery_use_one_shared_screen_and_no_per_frame_vlm(tmp_path, monkeypatch, saved_older_seams):
    p, cuts = fixture(monkeypatch)
    old_key = review.recipe.REVIEW + ":joins"
    prior = {"approved": True, "cut_assets": [cuts[i]["review_board"] for i in cuts]}
    run = {"director_decisions": {old_key: deepcopy(prior)}} if saved_older_seams else {}
    calls = []
    async def signature(ctx, prefix, run, plan, cuts, material):
        return "signature", await record(ctx, prefix, run, 28, "signature", {"ok": True, "report": {"ok": True}})
    async def scene(ctx, folder, prefix, run, plan, cuts, material, name):
        pairs = film.SCENE_JOINS[name]
        result = {"ok": True, "sheets": [f"seams-{name}"], "pairs": [[i, i + 1] for i in pairs],
                  "cut_assets": [cuts[i]["review_board"] for i in range(pairs[0], pairs[-1] + 2)]}
        return name, await record(ctx, prefix, run, 28, name, result)
    async def ask(ctx, name, schema, prompt, data, images, **kwargs):
        calls.append(name)
        assert name == "commercial_normal_playback" and images == ["product", "actor", *[f"board-{i}" for i in review.ANCHORS]]
        return {"report": schema.model_validate(observed()).model_dump(), "evidence": images}
    def no_scan(*args, **kwargs):
        pytest.fail("A clean watched film must not use the developer-only exhaustive scan or re-encode a clip.")
    monkeypatch.setattr(film, "_signature_joins", signature)
    monkeypatch.setattr(film, "_scene_joins", scene)
    monkeypatch.setattr(film, "_record", record)
    monkeypatch.setattr(review.reference_quality, "ask", ask)
    monkeypatch.setattr(pixels, "inspect", no_scan)
    monkeypatch.setattr(pixels, "_edited_cut", no_scan)
    assert await film._check_joins(None, tmp_path, "run", run, p, cuts, ["product"], cast={"protagonist": "actor"}) == []
    decisions = run["director_decisions"]
    proof = film._edit_proof(decisions, p, prior["cut_assets"], cuts)
    assert proof["playback_check"] in decisions and "frame_checks" not in proof
    if saved_older_seams:
        assert decisions[old_key] == prior and proof is not decisions[old_key]
    assert pixels.certified(decisions, proof, p, cuts)
    changed = {**cuts, 26: {**cuts[26], "start": 4}}
    assert not pixels.certified(decisions, proof, p, changed)
    run["wan_cuts"] = list(cuts.values())
    assert film._approved_edit(run, p) == cuts
    assert await film._check_joins(None, tmp_path, "run", run, p, cuts, ["product"]) == []
    assert calls == ["commercial_normal_playback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("severity,count,ok", [("sustained_obvious_defect", 1, True), ("clear_or_minor", 1, True),
                                               ("sustained_obvious_defect", 5, False), ("whole_frame_flash", 1, False)])
async def test_only_verified_normal_speed_failure_reopens_the_one_affected_shot(tmp_path, monkeypatch, severity, count, ok):
    p, cuts = fixture(monkeypatch)
    i = 26
    first = pixels._batches(i)[0][0]
    center = first + round((cuts[i]["output_frames"] - 1) / 2)
    calls, run = [], {}
    async def ask(ctx, name, schema, guide, data, images, **kwargs):
        calls.append(name)
        if name == "commercial_normal_playback":
            result = observed({"shot": i, "cell": 3, "reason": "Check the actual handoff."})
        else:
            assert name == "commercial_check_playback_moment" and images[-1].startswith("normal-playback-")
            assert data["first_frame"] <= center <= data["last_frame"]
            result = {"severity": severity, "first": center, "last": center + count - 1,
                      "observation": "Inspect the actual support in these frames.", "correction": "Keep the same item supported."}
        return {"report": schema.model_validate(result).model_dump()}
    async def edited(ctx, directory, cut, identity):
        assert cut == cuts[i]
        return directory / "selected-cut.mp4"
    async def frames(local, lo, hi, *, small, language):
        assert language == "vi"
        if not small:
            assert lo == hi == center - first
            return [Image.new("RGB", (1280, 720))]
        assert hi - lo == 12
        return [Image.new("RGB", (320, 180), (20, n, 50)) for n in range(lo, hi + 1)]
    async def store(ctx, label, data):
        return label
    monkeypatch.setattr(review.reference_quality, "ask", ask)
    monkeypatch.setattr(pixels, "_edited_cut", edited)
    monkeypatch.setattr(pixels, "_frames", frames)
    monkeypatch.setattr(review, "_reference_asset", store)
    args = (None, tmp_path, "run", run, p, cuts, ["product"], {}, record)
    key, out = await review.inspect(*args)
    assert out["ok"] is ok
    proof = {"cut_assets": out["boards"], "playback_check": key}
    assert pixels.certified(run["director_decisions"], proof, p, cuts) is ok
    if ok:
        bad = deepcopy(run)
        bad["director_decisions"][key]["targets"] = []
        bad["director_decisions"][key]["details"] = []
        assert not pixels.certified(bad["director_decisions"], proof, p, cuts)
    else:
        assert [issue["shot"] for issue in out["issues"]] == [i]
        assert out["issues"][0]["range"] == [center - 6, center + 6]
    assert (await review.inspect(*args))[1] == out
    assert calls == ["commercial_normal_playback", "commercial_check_playback_moment"]


def test_previous_visible_problem_must_match_window_and_persist_in_consecutive_frames(monkeypatch):
    _, cuts = fixture(monkeypatch)
    i = 4
    start = pixels._batches(i)[0][0]
    prev = {"version": pixels.VERSION, "ok": False, "shot": i, "board": cuts[i]["review_board"],
            "window": pixels.window(cuts[i]), "native_checks": ["past"]}
    def state(frames):
        return {"previous": prev, "past": {"confirmed": [{"frame": f, "observation": "The container is floating."} for f in frames]}}
    assert review._old_suspicions(state([start + 1]), cuts) == []
    assert review._old_suspicions(state([start, start + 2, start + 4, start + 6]), cuts) == []
    assert review._old_suspicions(state(range(start, start + 5)), cuts)[0][0] == i
    assert not review._old_suspicions(state(range(start, start + 5)), {**cuts, i: {**cuts[i], "start": 42}})


def test_saved_plan_without_camera_preprocessing_field_keeps_its_earlier_digest(monkeypatch):
    old, _ = fixture(monkeypatch)
    saved = dict(old.model_dump())
    assert "obscured_product_videos" not in saved
    for inputs in ([], ["actual-video-asset"]):
        loaded = Plan(**saved, obscured_product_videos=inputs)
        assert pixels.visual_plan(loaded) == saved and loaded.obscured_product_videos == inputs
        assert pixels.plan_id(loaded) == pixels._digest("shot-roles-and-effect-source-v2", saved)
        assert review.plan_key(loaded) == pixels._digest(review.VERSION, saved)
