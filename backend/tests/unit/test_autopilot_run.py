"""D2/D4 deterministic half: budget stop, regenerate-then-drop, blocklist, report totals from billing rows."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autopilot import ledger
from autopilot.template import AutopilotTemplate
from tool.autopilot_run import AutopilotRunArgs, execute
from tool.tool import ToolContext

TPL = AutopilotTemplate(credits_cap_per_run="30", videos_per_run=2, model_tier="medium", topics_blocklist=["政治", "医疗"],
                        content_forms=["口播", "画面+旁白"]).public()


def _ctx():
    return ToolContext(user_id="u_" + uuid.uuid4().hex[:6], workspace_id="w_" + uuid.uuid4().hex[:6],
                       session_id="s_" + uuid.uuid4().hex[:8], message_id="m", part_id="p")


def _kv(result):
    return dict(l.split("=", 1) for l in result.output.splitlines() if "=" in l and " " not in l.split("=", 1)[0])


async def test_start_plans_from_the_template_and_budget_stop_is_final():
    ctx = _ctx()
    r = await execute(AutopilotRunArgs(action="start", template=TPL), ctx)
    plan = r.metadata["plan"]
    assert plan["model_id"] == "wan3.0-video" and plan["resolution"] == "720p" and plan["videos_planned"] == 2
    assert "never switch" in r.output
    ok = await execute(AutopilotRunArgs(action="reserve", kind="analysis", credits="0.15", note="v1"), ctx)
    assert ok.metadata["allowed"] is True
    ok = await execute(AutopilotRunArgs(action="reserve", kind="generation", credits=18, note="v1 shots"), ctx)
    assert ok.metadata["allowed"] is True and Decimal(ok.metadata["run"]["remaining"]) == Decimal("11.85")
    deny = await execute(AutopilotRunArgs(action="reserve", kind="generation", credits="12", note="v2 shots"), ctx)
    assert deny.metadata["allowed"] is False and "预算上限 30" in deny.output and "停止新的付费步骤" in deny.output
    # once stopped, even a tiny reservation is refused
    again = await execute(AutopilotRunArgs(action="reserve", kind="compose", credits="0.03"), ctx)
    assert again.metadata["allowed"] is False and "already stopped" in again.output
    st = await execute(AutopilotRunArgs(action="status"), ctx)
    assert st.metadata["run"]["stopped_reason"]


async def test_judge_shot_regenerates_once_then_drops_and_accepts_good_takes():
    ctx = _ctx()
    await execute(AutopilotRunArgs(action="start", template=TPL), ctx)
    bad = await execute(AutopilotRunArgs(action="judge_shot", shot="v1s1", planned_sec=6, actual_sec=6.4, similarity=0.7), ctx)
    assert bad.metadata["verdict"] == "regenerate" and bad.metadata["attempt"] == 1 and "相似度 0.70 低于 0.85" in bad.output
    bad2 = await execute(AutopilotRunArgs(action="judge_shot", shot="v1s1", planned_sec=6, actual_sec=9.5, similarity=0.9), ctx)
    assert bad2.metadata["verdict"] == "drop" and "时长 9.5s 偏离计划 6.0s" in bad2.output and "Drop this video" in bad2.output
    good = await execute(AutopilotRunArgs(action="judge_shot", shot="v2s1", planned_sec=6, actual_sec=6.9, similarity=0.86), ctx)
    assert good.metadata["verdict"] == "accept"
    # non-spoken shots ignore similarity
    visual = await execute(AutopilotRunArgs(action="judge_shot", shot="v2s2", planned_sec=5, actual_sec=5.5, similarity=None, spoken=False), ctx)
    assert visual.metadata["verdict"] == "accept"


async def test_candidate_blocklist_and_form_filter():
    ctx = _ctx()
    await execute(AutopilotRunArgs(action="start", template=TPL), ctx)
    r = await execute(AutopilotRunArgs(action="record", kind="candidate", data={"title": "医疗科普：三招护腰", "topics": ["健康"], "form": "口播"}), ctx)
    assert r.metadata["blocked_topic"] == "医疗" and "blocked_topic=医疗" in r.output
    r = await execute(AutopilotRunArgs(action="record", kind="candidate", data={"title": "装修留白", "topics": ["家居"], "form": "混剪"}), ctx)
    assert r.metadata["blocked_topic"] is None and "form_allowed=false" in r.output
    r = await execute(AutopilotRunArgs(action="record", kind="candidate", data={"title": "装修留白", "topics": ["家居"], "form": "口播"}), ctx)
    assert "form_allowed=true" in r.output


async def test_report_reads_spend_from_billing_rows():
    from db.base import get_db_session
    from db.models.billing import UsageEvent

    ctx = _ctx()
    await execute(AutopilotRunArgs(action="start", template=TPL), ctx)
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        for i, (kind, credits, status) in enumerate([("video_generate", "9.00", "shadow"), ("video_analyze", "0.09", "shadow"),
                                                      ("video_compose", "0.03", "charged"), ("chat", "0.50", "unpriced")]):
            db.add(UsageEvent(id=f"usage_{uuid.uuid4().hex[:10]}", idempotency_key=f"k{i}-{uuid.uuid4().hex[:6]}", workspace_id=ctx.workspace_id,
                              user_id=ctx.user_id, session_id=ctx.session_id, message_id=None, session_title="t", model_id="m", kind=kind,
                              tokens={}, total_tokens=0, credits=Decimal(credits), status=status, pricing={}, created_at=now))
    items = [{"source": "douhot", "hot_title": "留白装修", "form": "口播", "title": "装修高级感在留白", "topics": ["装修"], "credits": "9.12",
              "publish": {"mode": "auto", "status": "published", "item_id": "768", "visibility": "公开"}},
             {"source": "douhot", "hot_title": "露营", "form": "混剪", "dropped_reason": "形态不在模版允许范围"}]
    r = await execute(AutopilotRunArgs(action="report", items=items), ctx)
    assert r.metadata["total_credits"] == "9.12" and r.metadata["usage_events"] == 3  # unpriced row excluded
    assert r.metadata["published"] == 1 and r.metadata["dropped"] == 1
    md = r.output
    assert "本次实际落账 9.12 积分" in md and "✅ 已发布（公开） 作品 768" in md and "❌ 弃用：形态不在模版允许范围" in md
    assert "video_generate 9" in md


async def test_no_run_and_validation():
    ctx = _ctx()
    r = await execute(AutopilotRunArgs(action="reserve", kind="analysis", credits="1"), ctx)
    assert r.metadata.get("error") == "no_run"
    t = await execute(AutopilotRunArgs(action="tiers"), ctx)
    assert len(t.metadata["tiers"]) == 3 and "wan3.0-video" in t.output
    v = await execute(AutopilotRunArgs(action="validate_template", template={"credits_cap_per_run": "30", "model_tier": "ultra"}), ctx)
    assert v.metadata["valid"] is False
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        AutopilotRunArgs(action="reserve", kind="x")
    from agent.agent import AGENTS, BUILD_ONLY_WORKFLOW_TOOLS
    assert "autopilot_run" in BUILD_ONLY_WORKFLOW_TOOLS and "autopilot_run" in AGENTS["build"].tools


async def test_skill_is_loaded_and_names_the_tools():
    from skill import skill as sk

    names = {s.name: s for s in await sk.list_skills()}
    assert "marketing-autopilot" in names
    body = names["marketing-autopilot"].content
    for tool in ("autopilot_run", "hot_trends", "video_analyze", "video_generate", "video_compose", "desktop_publish", "douyin_publish", "cron"):
        assert tool in body
    assert "模版参数（预算授权）" in body and "reserve" in body and "judge_shot" in body
