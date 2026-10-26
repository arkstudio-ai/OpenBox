"""Private procedures cannot bypass evidence, validation, revision or ownership."""
import json
import pytest
from sqlalchemy import select

from core.config import OpenBoxConfig
from db.base import get_db_session
from db.models.memory_v2 import MemorySource, MemorySourceLink
from memory import experiences, service
from memory.policy import resolve_access_scope, MemoryAccessDenied
from skill.user_library import get_owned_skill, list_owned_skills
from tests.unit.test_memory_write_protocol import setup
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401

CONTENT = {"title": "导出门店视频前核对字幕", "conditions": "用于门店宣传视频的最终导出",
    "steps": ["核对店名和地址", "预览字幕边缘和音画同步", "导出后播放检查"],
    "expected_result": "字幕没有溢出，店名和地址正确", "failure_boundaries": "信息不确定就停止并询问",
    "test_plan": "用一段测试视频导出，逐个播放字幕出现的时间段"}


async def seed(monkeypatch):
    owner, other, workspace, _, _ = await setup(monkeypatch)
    config = OpenBoxConfig(model="test/model", memory={"v2_write": True, "experience_learning": True,
        "automatic_knowledge": False})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(experiences, "get_config", lambda: config)
    saved = await service.create_note(user_id=owner, workspace_id=workspace,
        summary="门店视频导出前核对店名地址，预览字幕边缘和音画同步，导出后播放检查；信息不确定就询问。")
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=owner, workspace_id=workspace)
        sources = list((await db.scalars(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == saved["id"]))).all())
        row = await experiences.propose_in_session(db, scope, operation_id="qa-experience", content=CONTENT, sources=sources)
        identity, source_id = row.id, sources[0].id
    return dict(user_id=owner, workspace_id=workspace, identity=identity), other, source_id


async def test_candidate_validation_enable_edit_and_retire_are_versioned(monkeypatch):
    args, _, _ = await seed(monkeypatch)
    with pytest.raises(service.MemoryConflict):
        await experiences.enable(**args, expected_revision=1)
    failed = await experiences.review(**args, expected_revision=1, passed=False, notes="测试导出后的字幕发生了边缘溢出，需要修正")
    assert failed["state"] == "candidate"
    passed = await experiences.review(**args, expected_revision=2, passed=True, notes="调整安全区域后重新导出，逐段检查字幕没有溢出")
    enabled = await experiences.enable(**args, expected_revision=passed["revision"])
    skill = await get_owned_skill(args["user_id"], enabled["skill_id"], workspace_id=args["workspace_id"])
    assert skill is not None
    with pytest.raises(service.MemoryConflict):
        await experiences.revise(**args, expected_revision=1, content=CONTENT)
    revised = await experiences.revise(**args, expected_revision=enabled["revision"],
        content={**CONTENT, "steps": CONTENT["steps"] + ["再核对结尾的店名"]})
    assert revised["state"] == "candidate" and not revised["validation"]
    assert await get_owned_skill(args["user_id"], enabled["skill_id"], workspace_id=args["workspace_id"]) is None
    assert not await list_owned_skills(args["user_id"], args["workspace_id"])
    passed = await experiences.review(**args, expected_revision=revised["revision"], passed=True,
        notes="按新版本再次导出，字幕和结尾店名都已经逐段核对正确")
    enabled2 = await experiences.enable(**args, expected_revision=passed["revision"])
    skill2 = await get_owned_skill(args["user_id"], enabled2["skill_id"], workspace_id=args["workspace_id"])
    assert skill2["version"] > skill["version"]
    await experiences.retire(**args, expected_revision=enabled2["revision"])
    assert await get_owned_skill(args["user_id"], enabled2["skill_id"], workspace_id=args["workspace_id"]) is None


async def test_revoke_original_evidence_hides_enabled_content_and_private_skill(monkeypatch):
    args, other, source_id = await seed(monkeypatch)
    with pytest.raises(MemoryAccessDenied):
        await experiences.review(**{**args, "user_id": other}, expected_revision=1,
            passed=True, notes="别的用户不能确认这个私有经验记录")
    passed = await experiences.review(**args, expected_revision=1, passed=True, notes="用测试素材完整验证字幕流程，结果符合预期")
    enabled = await experiences.enable(**args, expected_revision=passed["revision"])
    async with get_db_session() as db:
        (await db.get(MemorySource, source_id)).status = "REVOKED"
    result = await experiences.listing(user_id=args["user_id"], workspace_id=args["workspace_id"])
    assert result["items"][0]["state"] == "needs_review"
    assert result["items"][0]["content"] is None
    assert await get_owned_skill(args["user_id"], enabled["skill_id"], workspace_id=args["workspace_id"]) is None
    assert not await list_owned_skills(args["user_id"], args["workspace_id"])


@pytest.mark.parametrize("query", ["帮门店视频做一下字幕检查", "导出门店宣传片之前要核对哪些内容？"])
async def test_enabled_method_discovered_when_router_underestimates_experience(monkeypatch, query):
    from memory import packet, readers
    args, other, source_id = await seed(monkeypatch)
    from core.config import get_config
    passed = await experiences.review(**args, expected_revision=1, passed=True,
        notes="用测试视频核对字幕、地址和音画同步，结果符合预期")
    await experiences.enable(**args, expected_revision=passed["revision"])
    config = get_config().memory
    config.retrieval_v2 = config.route_jev = True
    config.recall_deadline_seconds = 2
    turn = packet.TurnContext(args["user_id"], args["workspace_id"], query)
    async def route(*a, **kw):
        return {"read_policy": "allowed", "memory_forbidden": False,
            "memory": {"needed": True}, "task": {"needed": False},
            "needs": {"experience_useful": .3}}
    async def search(**kw):
        return {"items": [], "stable_background": {"items": [], "budget": {"characters": 0}}}
    monkeypatch.setattr("memory.read_routing.route", route)
    monkeypatch.setattr("memory.retrieval.search_memory", search)
    from dataclasses import replace
    before = await readers.catalogs(replace(turn, read_policy="allowed"),
        {"experience_useful": .3}, config, recall_needed=True)
    assert [item["id"] for item in before] == [args["identity"]]
    bundle = await packet.prepare_context(turn, config)
    assert [item["id"] for item in bundle["catalogs"]] == [args["identity"]], json.dumps(bundle, default=str)
    assert "steps" not in bundle["catalogs"][0]
    assert bundle["budget"]["tokens"] <= config.recall_token_budget
    allowed_turn = bundle["_turn"]
    assert await readers.catalogs(replace(allowed_turn, query="天气如何"),
        {"experience_useful": .3}, config, recall_needed=True) == []
    assert await readers.catalogs(replace(allowed_turn, read_policy="current_only"),
        {"experience_useful": .3}, config, recall_needed=True) == []
    assert await readers.catalogs(replace(allowed_turn, user_id=other),
        {"experience_useful": .3}, config, recall_needed=True) == []
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=args["user_id"], workspace_id=args["workspace_id"])
        memory_id = await db.scalar(select(MemorySourceLink.memory_id).where(MemorySourceLink.source_id == source_id))
    await service.forget_memory(user_id=args["user_id"], workspace_id=args["workspace_id"], memory_id=memory_id)
    refreshed = await packet.refresh_packet(bundle, scope, config)
    assert refreshed["catalogs"] == []


async def test_private_experience_never_grants_execution_and_renewal_is_read_fresh(monkeypatch):
    """Real SQL entitlement boundary; no cloud provisioning or remote execution."""
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    from db.models.billing import BillingSubscription, CreditBalance
    from sandbox.entitlement import require_sandbox_subscription, SandboxSubscriptionRequired
    from core.config import get_config

    args, other, _ = await seed(monkeypatch)
    config = get_config()
    config.sandbox_provider, config.wuying_routing = "wuying", "per_desktop"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(BillingSubscription(id="experience-expired-plan", workspace_id=args["workspace_id"],
            plan_id="max", cycle="monthly", plan={}, starts_at=now - timedelta(days=31),
            ends_at=now - timedelta(days=1)))
        balance = await db.get(CreditBalance, args["workspace_id"])
        if balance is not None:
            balance.balance = Decimal("2091")
        else:
            db.add(CreditBalance(workspace_id=args["workspace_id"], balance=Decimal("2091"), updated_at=now))
    # Saving/reviewing a private procedure is not a cloud execution grant.
    passed = await experiences.review(**args, expected_revision=1, passed=True,
        notes="验证记录来自套餐仍有效时的完整字幕导出检查，当前只保存经验。")
    enabled = await experiences.enable(**args, expected_revision=passed["revision"])
    assert await get_owned_skill(args["user_id"], enabled["skill_id"], workspace_id=args["workspace_id"])
    assert await get_owned_skill(other, enabled["skill_id"], workspace_id=args["workspace_id"]) is None
    with pytest.raises(SandboxSubscriptionRequired) as denied:
        await require_sandbox_subscription(args["workspace_id"])
    assert "积分余额不能代替套餐权限" in str(denied.value)
    async with get_db_session() as db:
        (await db.get(BillingSubscription, "experience-expired-plan")).ends_at = now + timedelta(days=30)
    # No restart/cache clearing; the next gate observes renewed membership.
    assert (await require_sandbox_subscription(args["workspace_id"])).plan_id == "max"
    assert await get_owned_skill(other, enabled["skill_id"], workspace_id=args["workspace_id"]) is None
    async with get_db_session() as db:
        assert (await db.get(CreditBalance, args["workspace_id"])).balance == Decimal("2091")
