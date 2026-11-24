from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from light import commercial_calibration as calibration, commercial_plan, production
from tests.unit.test_commercial_plan import treatment
from tool.tool import ToolContext


def _plan():
    return commercial_plan.Plan.model_validate(treatment(
        carry="Tay trái giữ thân bình ngang eo.", contact="Ngón trỏ phải chạm quai ngang eo."))


def _evidence():
    report = dict(actor_matches=True, product_matches=True, support_matches=True, single_product=True,
                  feature_xy=[550, 545], landmark_xy=[550, 705], reason="Quai ở bụng trên.")
    return {"checks": [dict(photograph="photo-a", report=report.copy()),
                       dict(photograph="photo-b", report=report.copy())]}


def _proposal():
    return dict(conflicts_with_merchant=False, same_actor_product_and_support=True,
                same_hand_side_and_feature=True, same_touch_is_reachable=True, only_natural_pose_details_differ=True,
                observed="Quai ở bụng trên, tay trái vẫn nâng bình.",
                edits=[dict(slot=name, original="ngang eo", replacement="ngang bụng trên")
                       for name in ("carry", "contact")])


def _install_form(monkeypatch, plan):
    run = {"commercial_auto_staging": True, "remake_stage": "remake_frames", "job_prefix": "job",
           "action": "render", "remake_adaptation": plan.model_dump()}
    row = SimpleNamespace(light_production={"template": "product_story", "run": run,
                          "reference_copy": "Làm quảng cáo cho bình nước.", "script": "previous"})
    db = SimpleNamespace(scalar=AsyncMock(return_value=None))

    @asynccontextmanager
    async def database():
        yield db

    ctx = ToolContext(session_id="session", user_id="owner", workspace_id="workspace", run_id="run", run_generation=1)
    lock = AsyncMock(return_value=row)
    monkeypatch.setattr(production, "_lock", lock)
    monkeypatch.setattr(calibration, "get_db_session", database)
    return ctx, row, db, lock


def test_only_matching_phrases_in_list_or_scalar_directions_are_replaced():
    plan = _plan()
    values = _proposal()["edits"] + [dict(slot="opening_3", original="ngay ngắn", replacement="ở bụng trên")]
    adjusted = calibration._apply(plan, [calibration.Edit(**edit) for edit in values])
    assert adjusted.carry == "Tay trái giữ thân bình ngang bụng trên."
    assert adjusted.contact == "Ngón trỏ phải chạm quai ngang bụng trên."
    assert adjusted.insert_openings[2] == "Tay giữ bình ở bụng trên."
    changed = {key for key, value in plan.model_dump().items() if adjusted.model_dump()[key] != value}
    assert changed == {"carry", "contact", "insert_openings"}
    for edits in ([values[2]], [dict(values[0], original="không tồn tại")], [dict(values[0], replacement="ngang eo")]):
        with pytest.raises(ValueError):
            calibration._apply(plan, [calibration.Edit(**edit) for edit in edits])
    with pytest.raises(ValueError):
        calibration.Edit(**dict(values[0], slot="call_line"))


@pytest.mark.parametrize("one_practical_pose", [True, False])
async def test_two_independent_reviews_save_the_same_plan_for_script_and_filming_and_replay(monkeypatch, one_practical_pose):
    plan = _plan()
    ctx, row, _, lock = _install_form(monkeypatch, plan)
    second = dict(respects_merchant=True, same_physical_action=True, position_matches_photo=True,
                  consistent_everywhere=True, reason="Cả ba lần chạm đều đúng tầm tay.")
    ask = AsyncMock(side_effect=[{"report": _proposal()}, {"report": second}])
    monkeypatch.setattr(calibration.reference_quality, "ask", ask)
    run = deepcopy(row.light_production["run"])
    observations = _evidence()
    if one_practical_pose:
        observations["checks"] = observations["checks"][1:]
    args = (ctx, "job", run, plan, "photo-b", "cast", ["user-frame-a", "user-frame-b"], observations)
    result = await calibration.align_carry(*args)
    assert result.carry == "Tay trái giữ thân bình ngang bụng trên."
    assert row.light_production["script"] == commercial_plan.display_script(result)
    assert run["remake_adaptation"] == row.light_production["run"]["remake_adaptation"] == row.light_production["remake_adaptation"]
    receipt = run["commercial_carry_calibration"]
    assert receipt["before_plan_digest"] == calibration._digest(plan)
    assert receipt["after_plan_digest"] == calibration._digest(result)
    assert [item.args[1] for item in ask.await_args_list] == ["align_commercial_carry_height", "verify_commercial_carry_height"]
    assert all(item.args[5] == ["photo-b", "cast", "user-frame-a", "user-frame-b"] for item in ask.await_args_list)
    assert await calibration.align_carry(*args) == result
    assert await calibration.align_carry(*args[:4], "different-photo", *args[5:]) is None
    assert ask.await_count == 2
    lock.assert_awaited_with(lock.await_args.args[0], "session", "owner", "workspace", run_fence=ctx.run_fence)


@pytest.mark.parametrize("blocked", ["preview", "shot-dispatched", "cut-saved", "wrong-stage", "different-photo",
                                      "wrong-product", "merchant-height", "merchant-grip", "inconsistent-revision"])
async def test_fixed_constraints_and_published_shots_preserve_the_original_script(monkeypatch, blocked):
    plan = _plan()
    ctx, row, db, _ = _install_form(monkeypatch, plan)
    run = row.light_production["run"]
    checks = _evidence()
    if blocked == "preview":
        run.pop("commercial_auto_staging")
    elif blocked == "shot-dispatched":
        db.scalar.return_value = "pending-shot"
    elif blocked == "cut-saved":
        run["wan_cuts"] = [{"shot": 1}]
    elif blocked == "wrong-stage":
        run["remake_stage"] = "wan_shots"
    elif blocked == "different-photo":
        checks["checks"][1]["photograph"] = "photo-a"
    elif blocked == "wrong-product":
        checks["checks"][1]["report"]["product_matches"] = False

    proposal = {**_proposal(), "conflicts_with_merchant": blocked in {"merchant-height", "merchant-grip"}}
    second = dict(respects_merchant=True, same_physical_action=True, position_matches_photo=True,
                  consistent_everywhere=False, reason="Chưa khớp cảnh chạy.")
    ask = AsyncMock(side_effect=[{"report": proposal}, {"report": second}])
    monkeypatch.setattr(calibration.reference_quality, "ask", ask)
    assert await calibration.align_carry(ctx, "job", run, plan, "photo-b", "cast", ["source"], checks) is None
    assert ask.await_count == {"merchant-height": 1, "merchant-grip": 1, "inconsistent-revision": 2}.get(blocked, 0)
    assert row.light_production["script"] == "previous" and run["remake_adaptation"] == plan.model_dump()
    assert "commercial_carry_calibration" not in run
