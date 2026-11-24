from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from light import commercial_film as film
from light import commercial_wardrobe as wardrobe


def plan():
    return SimpleNamespace(mode="carry", score_revision=2, product="Bình nước", appearance="Bình cam quai đen",
        carry="Tay trái đỡ đáy bình.", contact="Tay phải chạm vai bình.", language="vi",
        track_wardrobe="Áo chạy xanh, quần đen, giày bạc.", protagonist="An mặc đồ đi làm.")


def coverage(**changes):
    return dict(same_person_and_running_outfit=True, true_product_support_and_position=True,
        neutral_hand_ready_to_move=True, contact_pose_matches=True, legs_and_shoes_match_and_are_visible=True,
        lower_crop_is_independent_of_old_hand=True, lower_box=[400, 600, 600, 995],
        shoes_box=[421, 883, 589, 979], observation="Cùng người, quần và đôi giày bạc.") | changes


def test_a_usable_lower_body_crop_includes_both_shoes_and_a_shared_garment():
    assert wardrobe.Coverage(**coverage()).accepted
    with pytest.raises(ValueError, match="Both shoes"):
        wardrobe.Coverage(**coverage(lower_box=[400, 600, 600, 950]))
    assert not wardrobe.Coverage(**coverage(neutral_hand_ready_to_move=False, lower_box=None)).accepted


async def test_real_shoot_can_use_two_complementary_views_and_resume_without_another_exposure(monkeypatch):
    p = plan()
    library = {"protagonist": "face", "carried_actor": "commute", "contact_actor": "commute-touch"}
    run = {}
    camera = AsyncMock(side_effect=["full-body", "track-touch", "resting-upper"])
    monkeypatch.setattr(film, "_still", camera)
    async def inspect(_ctx, name, schema, _guide, _data, photos, **kwargs):
        if name == "commercial_running_cast":
            has_feet = photos[0] == "full-body"
            return {"evidence": photos, "report": {
                "ok": False, "reason": "One source view is incomplete.", "pose_correction": "",
                "neutral_correction": "Rest the right hand." if has_feet else "The feet are out of frame.",
                "shoes_box": [431, 892, 595, 972] if has_feet else None}}
        assert name == "commercial_running_wardrobe_views" and schema is wardrobe.Coverage
        assert photos == ["resting-upper", "full-body", "track-touch", "commute", "commute-touch", "raw"]
        return {"evidence": photos, "report": coverage()}
    judge = AsyncMock(side_effect=inspect)
    monkeypatch.setattr(wardrobe.reference_quality, "ask", judge)
    async def record(_ctx, _prefix, current, _shot, key, item):
        return current.setdefault("director_decisions", {}).setdefault(key, item)
    async def detail(_ctx, original, box, *, lower_body=False):
        assert original == "full-body" and box == coverage()["lower_box" if lower_body else "shoes_box"]
        return "actual-legs" if lower_body else "actual-shoes"
    monkeypatch.setattr(film, "_record", record)
    monkeypatch.setattr(film, "_shoe_detail", detail)
    for _ in range(2):
        await film._running_cast(None, "film", run, p, library, ["raw"])
        assert library["track_actor"] == "resting-upper" and library["track_contact"] == "track-touch"
        assert library["track_lower_body"] == "actual-legs" and library["track_feet"] == "actual-shoes"
    assert judge.await_count == camera.await_count == 3


@pytest.mark.parametrize("wrong", ["actor", "costume", "material", "touch", "hand"])
async def test_cannot_reuse_a_conflicting_wardrobe_or_action(monkeypatch, wrong):
    p = plan()
    library = {"carried_actor": "commute", "contact_actor": "touch"}
    notes = {"director_decisions": {}}
    for name, shoes in (("full", [420, 895, 572, 979]), ("upper", None)):
        pictures = [name, "track-touch", "commute", "touch", "raw"]
        key = f"{film.recipe.REVIEW}:track-wardrobe:{film._fingerprint(pictures, p.track_wardrobe)}"
        notes["director_decisions"][key] = {"report": {"ok": False, "shoes_box": shoes}, "evidence": pictures}
    if wrong == "actor":
        library["carried_actor"] = "another-person"
    elif wrong == "costume":
        p.track_wardrobe = "Different outfit"
    elif wrong == "touch":
        library["contact_actor"] = "different-touch"
    check = AsyncMock(return_value={"report": coverage(neutral_hand_ready_to_move=False)})
    monkeypatch.setattr(wardrobe, "check_views", check)
    monkeypatch.setattr(film, "_record", AsyncMock(side_effect=lambda *_args: _args[-1]))
    raw = ["different-photo"] if wrong == "material" else ["raw"]
    assert not await wardrobe.resolve(None, "film", notes, p, library, raw)
    assert "track_actor" not in library and check.await_count == (1 if wrong == "hand" else 0)
