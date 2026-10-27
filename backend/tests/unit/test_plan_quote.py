"""Legacy persisted quote cards remain answerable; new tool schemas no longer offer estimates."""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from api.v1.ids import internal_id
from billing import credit_cap, plan_quote
from billing.plan_quote import PlanItem
from db.base import get_db_session
from db.models.billing import CreditBalance
from db.models.session import Session as SessionRow
from db.models.video_job import VideoJob
from tests.unit.test_v1_routes import _session, client, default_config, key_identity, seed_scope  # noqa: F401
from tool import video_production as vp
from tool.tool import ToolContext


async def _platform_session(uid: str, wid: str, *, model: str = "wan3.0-video", resolution: str = "1080p") -> str:
    """A web session (no API key): priced from the platform rate card."""
    from session import session as session_mod

    created = await session_mod.create_session(model="openai/gemini-3.8-flash", agent="build", title="plan",
                                               user_id=uid, workspace_id=wid)
    async with get_db_session() as db:
        await db.execute(update(SessionRow).where(SessionRow.id == created.id)
                         .values(video_model=model, video_resolution=resolution))
    return created.id


async def _stored_plan(sid: str) -> dict | None:
    async with get_db_session() as db:
        return (await db.get(SessionRow, sid)).plan_quote


async def test_quote_plan_prices_on_the_server_and_registers_the_plan(default_config, monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "shadow")
    uid, wid = await seed_scope()
    sid = await _platform_session(uid, wid)
    items = [PlanItem.model_validate(item) for item in [
        # The model says 5; wan3.0-video at the session's 1080p is 2.70/s → 27.
        {"kind": "generation", "shot": 1, "seconds": 10, "claimed_credits": "5"},
        {"kind": "generation", "shot": 2, "seconds": 5, "resolution": "720p"},   # 1.40/s → 7
        {"kind": "transcribe", "seconds": 15},                                   # fun-asr 0.05/min, one minute minimum
        {"kind": "compose", "seconds": 15},                                      # 1080p 0.0015/s, billed as 60 s → 0.09
    ]]
    result = await plan_quote.register(sid, items)
    assert result['total'] == '34.14'
    stored = await _stored_plan(sid)
    assert stored["total"] == "34.14" and stored["confirmed_at"] is None and stored["created_at"]
    assert [row["credits"] for row in stored["items"]] == ["27", "7", "0.05", "0.09"]
    assert stored["items"][0]["model"] == "wan3.0-video" and stored["items"][0]["resolution"] == "1080p"
    assert stored["items"][0]["claimed_credits"] == "5" and "claimed_credits" not in stored["items"][1]
    assert stored["items"][1]["resolution"] == "720p" and stored["items"][3]["resolution"] == "1080p"
    assert stored["items"][2]["model"] == "fun-asr"
    # An item without a verified price registers nothing; the earlier plan stays.
    with pytest.raises(plan_quote.PlanUnpriced):
        await plan_quote.register(sid, [PlanItem(kind='generation', shot=1, seconds=10, model='no-such-model')])
    assert (await _stored_plan(sid))["total"] == "34.14"
    for action in ('estimate', 'quote_plan'):
        with pytest.raises(ValueError):
            vp.VideoGenerateArgs(action=action, prompt='shot')


async def _card_two(monkeypatch, internal_sid: str, total: str) -> dict:
    """Card 2 as the skill files it, served through patched question lookups; ``seen`` records a reply."""
    from question import question as q_mod

    seen: dict = {}
    questions = [q_mod.Question(**{
        "header": "分镜与报价", "question": f"共 5 镜，预估 {total} 积分，可以开始生成吗？", "kind": "plan_confirm",
        "custom": True, "multiple": False,
        "options": [{"label": "可以", "estimated_credits": total}, {"label": "修改拆段或 prompt"}],
    })]

    async def get_request(request_id, user_id):
        return SimpleNamespace(id=request_id, status="pending", session_id=internal_sid, questions=questions)

    async def reply(request_id, answers, user_id="default"):
        seen["reply"] = (request_id, answers)
        return {"ok": True}

    monkeypatch.setattr(q_mod, "get_request", get_request)
    monkeypatch.setattr(q_mod, "reply", reply)
    return seen


async def test_confirming_the_plan_needs_the_whole_balance_in_enforce_mode(default_config, monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "enforce")
    uid, wid = await seed_scope()
    async with client(key_identity(uid, wid)) as http:
        public_sid = await _session(http)
        sid = internal_id(public_sid, "session")
        assert (await http.get(f"/sessions/{public_sid}")).json()["estimate"] is None
        plan = await plan_quote.register(sid, [PlanItem(kind="generation", shot=n, seconds=15) for n in range(1, 6)])
        total = Decimal(plan["total"])
        # The free weekly allowance (10) is credited before the check, as the paid steps would see it.
        assert total > 10 and plan["available"] == "10"
        assert plan["balance_sufficient"] is False and Decimal(plan["shortfall"]) == total - 10
        seen = await _card_two(monkeypatch, sid, plan["total"])
        # Asking for changes commits to nothing and passes.
        edit = await http.post(f"/sessions/{public_sid}/questions/qst_Q1", json={"answers": [["修改拆段或 prompt"]]})
        assert edit.status_code == 200 and seen.pop("reply") == ("Q1", [["修改拆段或 prompt"]])
        refused = await http.post(f"/sessions/{public_sid}/questions/qst_Q1", json={"answers": [["可以"]]})
        assert refused.status_code == 402, refused.text
        error = refused.json()["error"]
        assert error["code"] == "INSUFFICIENT_CREDITS_FOR_PLAN"
        assert error["details"] == {"required": plan["total"], "available": "10", "shortfall": plan["shortfall"]}
        assert "reply" not in seen
        async with get_db_session() as db:
            assert (await db.scalars(select(VideoJob).where(VideoJob.session_id == sid))).all() == []
        shown = (await http.get(f"/sessions/{public_sid}")).json()["estimate"]
        assert shown["total"] == plan["total"] and shown["balance_sufficient"] is False
        assert shown["shortfall"] == plan["shortfall"] and shown["exceeds_cap"] is False
        assert shown["confirmed_at"] is None and shown["created_at"].endswith("Z")
        assert [item["shot"] for item in shown["items"]] == [1, 2, 3, 4, 5]
        # Topped up, the same answer goes through.
        async with get_db_session() as db:
            (await db.get(CreditBalance, wid)).balance = total + 1
        ok = await http.post(f"/sessions/{public_sid}/questions/qst_Q1", json={"answers": [["可以"]]})
        assert ok.status_code == 200, ok.text
        assert seen["reply"] == ("Q1", [["可以"]])
        assert (await http.get(f"/sessions/{public_sid}")).json()["estimate"]["balance_sufficient"] is True


async def test_confirming_the_plan_over_max_credits_is_refused_and_recorded(default_config, monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "off")
    uid, wid = await seed_scope()
    async with client(key_identity(uid, wid)) as http:
        public_sid = await _session(http)
        sid = internal_id(public_sid, "session")
        await credit_cap.set_cap(sid, Decimal("60"))
        plan = await plan_quote.register(sid, [PlanItem(kind="generation", shot=n, seconds=15) for n in range(1, 6)])
        assert Decimal(plan["total"]) > 60
        # Outside enforce the balance is not checked; the cap always is.
        assert plan["exceeds_cap"] is True and plan["balance_sufficient"] is True and plan["available"] is None
        assert plan["credit_cap"] == {"max_credits": "60", "spent": "0", "remaining": "60"}
        seen = await _card_two(monkeypatch, sid, plan["total"])
        refused = await http.post(f"/sessions/{public_sid}/questions/qst_Q1", json={"answers": [["可以"]]})
        assert refused.status_code == 402, refused.text
        error = refused.json()["error"]
        assert error["code"] == "CREDIT_CAP_EXCEEDED"
        assert error["details"] == {"step": "plan", "expected": plan["total"], "spent": "0", "max_credits": "60"}
        assert "reply" not in seen
        shown = (await http.get(f"/sessions/{public_sid}")).json()
        assert shown["credit_cap"]["exceeded"]["step"] == "plan"
        assert shown["credit_cap"]["exceeded"]["expected"] == plan["total"]
        assert shown["estimate"]["exceeds_cap"] is True
        # The next request with a higher cap lets the same plan through.
        await credit_cap.set_cap(sid, Decimal("1000"))
        ok = await http.post(f"/sessions/{public_sid}/questions/qst_Q1", json={"answers": [["可以"]]})
        assert ok.status_code == 200 and seen["reply"] == ("Q1", [["可以"]])
        assert (await http.get(f"/sessions/{public_sid}")).json()["estimate"]["exceeds_cap"] is False


async def test_gate_only_bites_on_the_confirming_option_and_marks_confirmation(default_config, monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "off")
    uid, wid = await seed_scope()
    sid = await _platform_session(uid, wid)
    questions = [
        {"question": "讲稿可以吗", "options": [{"label": "可以"}]},
        {"question": "报价", "kind": "plan_confirm",
         "options": [{"label": "可以", "estimated_credits": "27"}, {"label": "需要修改"}]},
    ]
    assert plan_quote.affirmative_plan_answer(questions, [["可以"], ["可以"]]) is True
    assert plan_quote.affirmative_plan_answer(questions, [["可以"], ["需要修改"]]) is False
    assert plan_quote.affirmative_plan_answer(questions, [["可以"], ["可以，开始吧"]]) is False  # free text commits nothing
    assert plan_quote.affirmative_plan_answer(questions[:1], [["可以"]]) is False
    assert plan_quote.affirmative_plan_answer(questions, None) is False
    # Without a registered plan there is nothing to check.
    assert await plan_quote.gate_answer(sid, questions, [["可以"], ["可以"]]) is None
    await plan_quote.register(sid, [PlanItem(kind="generation", shot=1, seconds=10)])
    assert (await plan_quote.gate_answer(sid, questions, [["可以"], ["需要修改"]])) is None
    assert (await plan_quote.gate_answer(sid, questions, [["可以"], ["可以"]]))["total"] == "27"
    async with get_db_session() as db:
        row = await db.get(SessionRow, sid)
        plan_quote.mark_confirmed(row, questions, [["可以"], ["需要修改"]])
        assert row.plan_quote["confirmed_at"] is None
        plan_quote.mark_confirmed(row, questions, [["可以"], ["可以"]])
        assert row.plan_quote["confirmed_at"] is not None
    async with get_db_session() as db:
        row = await db.get(SessionRow, sid)
    view = await plan_quote.view(row)
    assert set(view) == {"total", "items", "exceeds_cap", "balance_sufficient", "shortfall", "created_at", "confirmed_at"}
    assert view["total"] == "27" and view["exceeds_cap"] is False and view["confirmed_at"].endswith("Z")
    assert await plan_quote.view(SimpleNamespace(id="x", plan_quote=None)) is None


def test_legacy_question_projection_survives_but_new_tool_schema_omits_quotes():
    from api.v1 import public
    from tool.question_tool import QuestionArgs

    card = {"question": "报价", "header": "分镜", "kind": "plan_confirm",
            "options": [{"label": "可以", "estimated_credits": "233.1"}, {"label": "改"}]}
    out = public._public_question(card)
    assert out["kind"] == "plan_confirm"
    assert out["options"] == [{"label": "可以", "description": "", "estimated_credits": "233.1"},
                              {"label": "改", "description": ""}]
    plain = public._public_question({"question": "x", "options": [{"label": "a"}]})
    assert "kind" not in plain and plain["options"] == [{"label": "a", "description": ""}]
    schema = QuestionArgs.model_json_schema()
    assert 'estimated_credits' not in str(schema) and 'plan_confirm' not in str(schema)
