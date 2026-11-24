from types import SimpleNamespace

import pytest
from light import commercial_film as film
from light import commercial_plan as recipe
from light import commercial_scene_recovery as recovery


def archive(monkeypatch, target=9):
    plan = SimpleNamespace(format=recipe.FORMAT, score_revision=2, mode="wear")
    receipts = {}
    run = {"wan_review_version": recipe.REVIEW, "director_decisions": receipts,
           "commercial_repairs": [{"target": target, "revision": 1, "report": {"kind": "commercial"}}]}
    assets = {}
    async def owned(ctx, ids):
        return {i: assets[i] for i in ids if i in assets}
    monkeypatch.setattr(recovery, "_assets", owned)

    def add(scene, revision, when, *, accepted):
        pairs = film.SCENE_JOINS[scene]
        shots = {}
        for i in range(pairs[0], pairs[-1] + 2):
            cut = {"shot": i, "source": f"video-{i}-{revision}", "review_board": f"board-{i}-{revision}",
                   "review_head": f"head-{i}-{revision}", "review_tail": f"tail-{i}-{revision}", "start": 2, "speed": 1,
                   "frames": recipe.BEATS[i - 1].frames, "output_frames": recipe.BEATS[i - 1].frames,
                   "review_version": recipe.REVIEW, "take": 1}
            if i == 26:
                cut["motion_performance"] = {"frames": [1, 5, 9]}
            key = f"{recipe.REVIEW}:take:{i}:{revision}"
            shots[i] = (key, {"cut": cut, "ok": True})
            receipts[key] = shots[i][1]
            for field in ("source", "review_board", "review_head", "review_tail"):
                assets[cut[field]] = when, "video/mp4" if field == "source" else "image/jpeg"
        sheet = f"seams-{scene}-{revision}"
        assets[sheet] = when, "image/jpeg"
        receipts[f"{recipe.REVIEW}:scene:{scene}:{revision}"] = {
            "ok": accepted, "cut_assets": [r["cut"]["review_board"] for _, r in shots.values()],
            "pairs": [[i, i + 1] for i in pairs], "sheets": [sheet]}
        return shots

    return plan, run, assets, add


@pytest.mark.asyncio
async def test_latest_owned_negative_assembly_wins_even_if_earlier_positive_was_inserted_last(monkeypatch):
    plan, run, assets, add = archive(monkeypatch)
    latest = add("home", "new", 200, accepted=False)
    add("home", "old", 100, accepted=True)
    cuts = await recovery.restore(None, run, plan, {})
    assert sorted(cuts) == [1, 2, 3, 4, 5, 6, 7, 8, 10, 11]
    assert all(c == latest[i][1]["cut"] for i, c in cuts.items())
    assert "wan_cuts" not in run
    del assets["seams-home-new"]
    assert not await recovery.restore(None, run, plan, {})


@pytest.mark.asyncio
async def test_frozen_cuts_and_anchor_transitive_dependents_are_not_reintroduced(monkeypatch):
    plan, run, _, add = archive(monkeypatch, target=25)
    home = add("home", "new", 2, accepted=True)
    add("office", "new", 3, accepted=False)
    old_two = {**home[2][1]["cut"], "review_board": "board-before-user-edit", "review_tail": "tail-before-user-edit"}
    home[3][1]["continuity_anchors"] = ["tail-2-new"]
    run["wan_cuts"] = [old_two]
    cuts = await recovery.restore(None, run, plan, {})
    assert not {2, 3, 25, 26, 27, 28}.intersection(cuts)
    assert 1 in cuts and run["wan_cuts"] == [old_two]


@pytest.mark.asyncio
async def test_cross_room_edges_bind_paid_fingerprint_and_actual_prior_tail(monkeypatch):
    plan, run, _, add = archive(monkeypatch)
    track = add("track", "g", 3, accepted=True)
    key, receipt = track[12]
    cuts = {11: {"review_tail": "actual-contact", "review_board": "seen-at-home"}}
    receipt["setup"] = ["opening", "designed-end"]
    receipt["evidence"] = ["opening", "set", "designed-end", "actual-contact", "original-product"]
    assert 12 in await recovery.restore(None, run, plan, cuts)
    receipt["evidence"][3] = "other-contact"
    assert 12 not in await recovery.restore(None, run, plan, cuts)
    del receipt["setup"]
    receipt["evidence"][3] = "actual-contact"
    assert 12 not in await recovery.restore(None, run, plan, cuts)
    old = run["director_decisions"].pop(key)
    anchor_key = film._fingerprint(["actual-contact"], "", 1)
    run["director_decisions"][f"{recipe.REVIEW}:take:12:{anchor_key}"] = old
    assert 12 in await recovery.restore(None, run, plan, cuts)
    old["continuity_anchors"] = ["conflicting-contact"]
    assert 12 not in await recovery.restore(None, run, plan, cuts)


@pytest.mark.asyncio
async def test_superseded_conflicted_unowned_and_obsolete_shots_fall_back_to_camera(monkeypatch):
    plan, run, assets, add = archive(monkeypatch, target=22)
    home = add("home", "g", 50, accepted=True)
    receipts = run["director_decisions"]
    receipts["superseding-review"] = {"ok": False, "replaces_review": home[4][0]}
    assets[home[5][1]["cut"]["source"]] = (50, "image/jpeg")
    del assets[home[6][1]["cut"]["review_head"]]
    home[9][1].update(report={"row": 1}, evidence=["no-departure-position"])
    receipts[home[7][0] + ":other"] = {**home[7][1], "cut": {**home[7][1]["cut"], "start": 5}}
    assert set(await recovery.restore(None, run, plan, {})) == {1, 2, 3, 8, 10, 11}
    add("home", "conflicting-time", 50, accepted=True)
    assert not await recovery.restore(None, run, plan, {})


@pytest.mark.asyncio
async def test_published_historical_unreadable_and_non_user_retries_take_the_original_path(monkeypatch):
    plan, run, _, add = archive(monkeypatch)
    add("home", "r1", 50, accepted=True)
    async def unexpected(ctx, ids):
        pytest.fail("Historical or published work must not consult a new scene snapshot")
    monkeypatch.setattr(recovery, "_assets", unexpected)
    for overrides in ({"wan_final_key": "published"}, {"output": {"asset": "file"}}, {"wan_review_version": "other"},
                      {"commercial_repairs": []}, {"commercial_repairs": [{"target": 9, "report": {"kind": "other"}}]}):
        assert not await recovery.restore(None, {**run, **overrides}, plan, {})
    plan.score_revision = 1
    assert not await recovery.restore(None, run, plan, {})
    async def unavailable(ctx, ids):
        raise OSError("Media unavailable")
    plan.score_revision = 2
    monkeypatch.setattr(recovery, "_assets", unavailable)
    assert not await recovery.restore(None, run, plan, {})
