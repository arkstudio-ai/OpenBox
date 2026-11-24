from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from light import commercial_film as film, commercial_plan as recipe, commercial_recovery as recovery
from light.product_story_continuity import ReviewRejected


def _plan(revision):
    return SimpleNamespace(score_revision=revision, mode="carry", product="Bình cam", appearance="Bình màu cam",
                           carry="Bàn tay trái giữ bình.", contact="Chạm nắp.", transfer_mode="pass", language="vi")


def _rejected():
    return ReviewRejected(11, "Ngón tay vẫn ở tay nắm cửa.", "Chạm nắp.", kind="commercial")


@pytest.mark.asyncio
async def test_restage_reads_only_this_shots_real_evidence_and_checkpoints_one_instruction(monkeypatch):
    data = {
        f"{recipe.REVIEW}:endpoints:11:x": {"report": {"ok": False, "reason": "Trượt nắp."},
                                          "evidence": ["opening", "ending", "original"]},
        f"{recipe.REVIEW}:take:11:y": {"ok": False, "reason": "Tay giữ cửa.",
                                     "evidence": ["takes-first", "background", "choices"]},
        f"{recipe.REVIEW}:take:12:z": {"ok": False, "evidence": ["unrelated"]},
    }
    run = {"director_decisions": data}
    answer = {"report": {"actionable": True, "observed": "Tay nắm cửa.", "direction": "Giữ vòng nắp trước người."}}
    review = AsyncMock(return_value=answer)
    monkeypatch.setattr(recovery.reference_quality, "ask", review)

    async def commit(ctx, prefix, current, number, key, item):
        data[key] = item
        return item

    monkeypatch.setattr(film, "_record", commit)
    args = (None, "prefix", run, _plan(2), {"id": 11, "scene": "home"},
            {"contact_actor": "pose", "product_cards": ["card"], "home": "door"}, {}, _rejected())
    first = await recovery.restage(*args)
    assert first[0] == answer["report"]["direction"] and first[1][0][1] == "pose"
    assert review.await_args.args[5] == ["pose", "card", "door", "choices", "opening", "ending"]
    assert await recovery.restage(*args) == first
    review.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("revision,decisions", [
    (1, {f"{recipe.REVIEW}:take:11:x": {"ok": False, "evidence": ["image"]}}),
    (2, {f"{recipe.REVIEW}:take:11:x": {"ok": False, "reason": "provider unavailable"}}),
    (2, {f"{recipe.REVIEW}:take:11:x": {"report": {"ok": True}, "evidence": ["accepted"]}}),
])
async def test_restage_requires_a_new_plan_and_a_rejected_picture(monkeypatch, revision, decisions):
    ask = AsyncMock()
    monkeypatch.setattr(recovery.reference_quality, "ask", ask)
    result = await recovery.restage(None, "p", {"director_decisions": decisions}, _plan(revision),
                                    {"id": 11, "scene": "home"}, {}, {}, _rejected())
    assert result is None
    ask.assert_not_awaited()
