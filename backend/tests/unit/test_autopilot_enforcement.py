"""F acceptance fixes: template lock and automatic budget reservation live in the tools,
tier resolution follows the registry, aborted cron runs fail, topics are not duplicated."""
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from autopilot import ledger, tiers
from autopilot.template import AutopilotTemplate


def _run(session, cap="30", tier="medium"):
    return ledger.start(session_id=session, workspace_id="w", user_id="u",
                        template=AutopilotTemplate(credits_cap_per_run=cap, model_tier=tier))


def test_resolution_follows_the_registry(video_gateway_config):
    # fixture registry: wan3.0-video declares 480p/720p/1080p → preferred 720p stands
    assert tiers.resolve_resolution("wan3.0-video", "720p") == ("720p", None)
    # MiniMax-H3 declares 480p/512p/768p/2k → preferred 768p stands
    assert tiers.resolve_resolution("MiniMax-H3", "768p")[0] == "768p"
    # a model that only declares 1080p (gw2's wan3.0-video today) → nearest higher
    video_gateway_config.video_generation.models[1].resolutions = ["1080p"]
    res, note = tiers.resolve_resolution("wan3.0-video", "720p")
    assert res == "1080p" and "720p 不可用" in note
    # unknown model → keep the preference
    assert tiers.resolve_resolution("nope", "720p") == ("720p", None)


def test_lock_and_guard(video_gateway_config):
    sid = "s_" + uuid.uuid4().hex[:6]
    assert ledger.guard_paid_step("s_other", kind="generation", credits="9") is None  # no run: nothing to enforce
    ledger.check_lock("s_other", model_id="anything", resolution="4k")
    run = _run(sid)
    plan = ledger.plan(run)
    assert plan["model_id"] == "wan3.0-video" and plan["resolution"] == "720p"
    ledger.check_lock(sid, model_id="wan3.0-video", resolution="720p")
    with pytest.raises(ledger.LockViolation, match="锁定了分辨率 720p"):
        ledger.check_lock(sid, model_id="wan3.0-video", resolution="1080p")
    with pytest.raises(ledger.LockViolation, match="锁定了模型"):
        ledger.check_lock(sid, model_id="MiniMax-H3", resolution="720p")
    assert "reserved 9" in ledger.guard_paid_step(sid, kind="generation", credits="9")
    ledger.guard_paid_step(sid, kind="generation", credits="18")
    with pytest.raises(ledger.BudgetStop, match="预算上限 30"):
        ledger.guard_paid_step(sid, kind="compose", credits="3.5")
    with pytest.raises(ledger.BudgetStop):
        ledger.guard_paid_step(sid, kind="analysis", credits="0.15")


def test_lock_follows_registry_resolution(video_gateway_config):
    video_gateway_config.video_generation.models[1].resolutions = ["1080p"]
    sid = "s_" + uuid.uuid4().hex[:6]
    plan = ledger.plan(_run(sid))
    assert plan["resolution"] == "1080p" and "改用 1080p" in plan["resolution_note"]
    ledger.check_lock(sid, model_id="wan3.0-video", resolution="1080p")
    with pytest.raises(ledger.LockViolation):
        ledger.check_lock(sid, model_id="wan3.0-video", resolution="720p")


async def test_hot_trends_live_collection_respects_the_cap(monkeypatch):
    import trends.service as ts
    from tool.hot_trends import HotTrendsArgs, execute
    from tool.tool import ToolContext

    async def site_status(workspace_id, site_key):
        return "bound"

    async def run_page(*a, **k):
        raise AssertionError("desktop must not be driven once the budget is exhausted")

    monkeypatch.setattr(ts, "site_status", site_status)
    monkeypatch.setattr(ts, "run_page", run_page)
    fixed = "2026-09-10-" + uuid.uuid4().hex[:6]
    monkeypatch.setattr(ts, "day_key", lambda when=None: fixed)
    ts._locks.clear()
    sid = "s_" + uuid.uuid4().hex[:6]
    run = _run(sid, cap="10", tier="low")
    ledger.guard_paid_step(sid, kind="generation", credits="9.9")
    ctx = ToolContext(user_id="u", workspace_id="w_" + uuid.uuid4().hex[:6], session_id=sid, message_id="m", part_id="p")
    r = await execute(HotTrendsArgs(source="douhot"), ctx)
    assert r.metadata.get("refused") and "预算上限 10" in r.output


def test_executor_flags_aborted_runs():
    from cron.executor import was_aborted

    msgs = [SimpleNamespace(role="user", finish=None), SimpleNamespace(role="assistant", finish="tool_calls"),
            SimpleNamespace(role="assistant", finish="aborted")]
    assert was_aborted(msgs) is True
    assert was_aborted([SimpleNamespace(role="assistant", finish="stop")]) is False
    assert was_aborted([]) is False


def test_cron_tool_defaults_to_shanghai():
    from tool.cron_tool import CronToolArgs

    assert CronToolArgs(action="add", name="n", schedule="0 9 * * *", task="t").timezone == "Asia/Shanghai"


async def test_desktop_publish_strips_duplicate_topics_from_intro(monkeypatch):
    import publish.desktop_service as svc

    captured = {}

    async def fake_precheck(caller, requested_mode=None):
        return svc.Precheck(mode="package", mode_reason="test", account=None, login_ok=False, budget=None)

    monkeypatch.setattr(svc, "precheck", fake_precheck)
    spec = svc.PublishSpec(asset_id="a", title="t", intro="首次装修避坑 #装修 #避坑指南 千万别跟风。 #装修", topics=["装修", "避坑指南"])
    from tool.tool import ToolContext
    ctx = ToolContext(user_id="u", workspace_id="w", session_id="s", message_id="m", part_id="p")
    with pytest.raises(svc.PublishRefusal):
        await svc.publish(svc.Caller(workspace_id="w", user_id="u"), spec, ctx=ctx)
    assert spec.intro == "首次装修避坑 千万别跟风。"
