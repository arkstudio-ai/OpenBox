"""D1: marketing-autopilot template — strict schema, tiers, cron persistence, executor prompt."""
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from autopilot import tiers
from autopilot.template import CONTENT_FORMS, AutopilotTemplate, parse_template
from cron import validation
from cron.executor import _build_cron_prompt
from cron.types import CronJobCreate, CronJobUpdate, CronScheduleEvery


def test_template_defaults_and_strictness():
    t = AutopilotTemplate(credits_cap_per_run="30")
    assert t.kind == "marketing_autopilot" and t.model_tier == "medium" and t.publish_mode == "auto"
    assert t.content_forms == list(CONTENT_FORMS) and t.tolerances.stt_similarity == 0.85
    assert t.public()["credits_cap_per_run"] == "30"
    with pytest.raises(ValidationError):
        AutopilotTemplate(credits_cap_per_run="30", unknown_field=1)
    with pytest.raises(ValidationError, match="unknown content form"):
        AutopilotTemplate(credits_cap_per_run="30", content_forms=["口播", "舞蹈"])
    with pytest.raises(ValidationError, match="cannot pay for one"):
        AutopilotTemplate(credits_cap_per_run="1", model_tier="medium")
    with pytest.raises(ValidationError):
        AutopilotTemplate(credits_cap_per_run="30", model_tier="ultra")
    with pytest.raises(ValidationError):
        AutopilotTemplate(credits_cap_per_run="30", videos_per_run=9)
    # dedupe + trim of word lists
    t = AutopilotTemplate(credits_cap_per_run="30", topics_blocklist=[" 政治 ", "政治", "医疗"], categories=["美食", "美食"])
    assert t.topics_blocklist == ["政治", "医疗"] and t.categories == ["美食"]
    assert parse_template(None) is None and parse_template(t.model_dump(mode="json")) == t


def test_tiers_map_to_models_and_reference_prices():
    d = {x["tier"]: x for x in tiers.describe_tiers()}
    assert d["high"]["model_id"] == "video-sd-720p-proⅠ" and d["medium"]["model_id"] == "wan3.0-video" and d["low"]["model_id"] == "MiniMax-H3"
    assert d["low"]["resolution"] == "768p" and all(x["reference_credits"] for x in d.values())
    assert tiers.minimum_credits_per_video("medium") > Decimal("9")
    with pytest.raises(KeyError):
        tiers.tier("ultra")


def test_validate_template_gives_a_field_path():
    validation.validate_template(None)
    validation.validate_template({})
    with pytest.raises(ValueError, match="Invalid template: model_tier"):
        validation.validate_template({"credits_cap_per_run": "30", "model_tier": "ultra"})
    with pytest.raises(ValueError, match="Invalid template"):
        validation.validate_template({"credits_cap_per_run": "0.5"})


class _FakeSession:
    def __init__(self, sid):
        self.id = sid
        self.model = "m"


@pytest.fixture
def svc(monkeypatch):
    import cron.service as service_mod
    import project.workspace as ws
    import session.session as sess
    from cron.service import CronService

    async def fake_get_session(sid, user_id=None, **kw):
        return _FakeSession(sid)

    async def fake_get_project(pid, user_id):
        return object()

    monkeypatch.setattr(sess, "get_session", fake_get_session)
    monkeypatch.setattr(ws, "get_project", fake_get_project)
    monkeypatch.setattr(service_mod, "arm_timer", lambda state: None)
    return CronService()


def _create(**overrides) -> CronJobCreate:
    base = dict(project_id="proj_" + uuid.uuid4().hex[:8], name="自动营销",
                schedule=CronScheduleEvery(kind="every", every_ms=86_400_000), task_prompt="按 marketing-autopilot 技能运行")
    base.update(overrides)
    return CronJobCreate(**base)


async def test_template_is_stored_read_back_updated_and_cleared(svc):
    user = "u_" + uuid.uuid4().hex[:8]
    tpl = AutopilotTemplate(credits_cap_per_run="45", categories=["美食"], model_tier="low", publish_mode="package").public()
    created = await svc.add(user, _create(template=tpl))
    job = await svc.get_job(created["id"], user)
    assert job["template"]["model_tier"] == "low" and job["template"]["categories"] == ["美食"] and job["template"]["publish_mode"] == "package"
    await svc.update(job["id"], user, CronJobUpdate(template={**tpl, "publish_mode": "auto"}))
    assert (await svc.get_job(job["id"], user))["template"]["publish_mode"] == "auto"
    await svc.update(job["id"], user, CronJobUpdate(template={}))
    assert (await svc.get_job(job["id"], user))["template"] is None
    plain = await svc.add(user, _create(name="普通", task_prompt="x"))
    assert (await svc.get_job(plain["id"], user))["template"] is None


async def test_add_and_update_reject_a_bad_template(svc):
    user = "u_" + uuid.uuid4().hex[:8]
    with pytest.raises(ValueError, match="Invalid template: content_forms"):
        await svc.add(user, _create(template={"credits_cap_per_run": "30", "content_forms": ["跳舞"]}))
    job = await svc.add(user, _create(template=AutopilotTemplate(credits_cap_per_run="30").public()))
    with pytest.raises(ValueError, match="Invalid template: model_tier"):
        await svc.update(job["id"], user, CronJobUpdate(template={"credits_cap_per_run": "30", "model_tier": "ultra"}))


def test_executor_prompt_carries_the_template_block():
    job = {"id": "cron_1", "name": "自动营销", "task_prompt": "跑一次", "template": {"kind": "marketing_autopilot", "credits_cap_per_run": "30", "model_tier": "medium"}}
    zh = _build_cron_prompt(job, "", "zh-CN")
    assert "模版参数（预算授权）" in zh and '"model_tier": "medium"' in zh and "不要再出确认卡" in zh
    en = _build_cron_prompt(job, "", "en-US")
    assert "Template (budget authorisation)" in en and "marketing-autopilot" in en
    plain = _build_cron_prompt({"id": "cron_2", "name": "n", "task_prompt": "t"}, "", "zh-CN")
    assert "模版参数" not in plain
