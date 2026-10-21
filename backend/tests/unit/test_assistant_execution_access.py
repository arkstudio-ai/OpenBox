"""Expired-plan attachment failures preserve evidence without retries or new tasks."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from agent import inbox
from agent.driver import reserve_run
from assistant.commands import accept_task_command
from assistant.history import read_history
from assistant.policy import AssistantError
from assistant.reads import get_task
from assistant.reporting import read_result_sources
from assistant.results import deliver_task_result
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult
from db.models.billing import BillingSubscription, CreditBalance
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from sandbox.entitlement import SandboxSubscriptionRequired, sandbox_access_status
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401


async def seed_reference(owner, workspace, main):
    async with get_db_session() as db:
        db.add(FileAsset(id="reference-image", user_id=owner, workspace_id=workspace,
            session_id=main.id, project_id=main.project_id, name="milk-frog.png",
            oss_key="fixture/milk-frog.png", mime="image/png", size=23, status="ready",
            source="user", is_deleted=False, transient=False, created_at=datetime.now(timezone.utc)))


async def seed_term(workspace, *, active):
    at = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(BillingSubscription(id="fixture-plan", workspace_id=workspace,
            plan_id="max", cycle="monthly", plan={}, starts_at=at - timedelta(days=31),
            ends_at=at + timedelta(days=1) if active else at - timedelta(days=1)))
        balance = await db.get(CreditBalance, workspace)
        if balance:
            balance.balance = Decimal("2091")
        else:
            db.add(CreditBalance(workspace_id=workspace, balance=Decimal("2091"), updated_at=at))


@pytest.fixture(autouse=True)
def paid_execution(monkeypatch):
    monkeypatch.setattr("sandbox.entitlement.subscription_sandbox_enabled", lambda: True)


async def test_expired_plan_with_credits_creates_no_task_or_command_on_repeated_attempts():
    owner, _, workspace, main, kwargs = await setup_task()
    await seed_reference(owner, workspace, main)
    await seed_term(workspace, active=False)
    for index in range(3):
        with pytest.raises(AssistantError) as denied:
            await accept_task_command(**{**kwargs, "idempotency_key": f"retry-{index}",
                "prompt": "用这张奶蛙图片做五秒视频", "attachments": ["reference-image"]})
        assert denied.value.code == SandboxSubscriptionRequired.code
        assert "积分余额不能代替套餐权限" in str(denied.value)
    async with get_db_session() as db:
        for model in (AssistantCommand, AssistantTask):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
        assert await db.scalar(select(func.count()).select_from(Session)) == 1
        assert not (await db.get(FileAsset, "reference-image")).is_deleted
        assert (await db.get(CreditBalance, workspace)).balance == Decimal("2091")
    # Ordinary text work remains allowed under the existing product rule.
    assert (await accept_task_command(**kwargs))["state"] == "accepted"


async def test_active_plan_acceptance_replays_after_expiry_without_duplicate_work():
    owner, _, workspace, main, kwargs = await setup_task()
    await seed_reference(owner, workspace, main)
    await seed_term(workspace, active=True)
    args = {**kwargs, "attachments": ["reference-image"]}
    accepted = await accept_task_command(**args)
    async with get_db_session() as db:
        (await db.get(BillingSubscription, "fixture-plan")).ends_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await accept_task_command(**args) == accepted
    with pytest.raises(AssistantError):
        await accept_task_command(**{**args, "idempotency_key": "different-attempt"})
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantTask)) == 1
        assert (await db.get(AgentInboxItem, accepted["inbox_id"])).attachments == ["reference-image"]


async def test_runtime_expiry_closes_once_and_reaches_all_assistant_reads(monkeypatch):
    owner, _, workspace, main, kwargs = await setup_task()
    await seed_reference(owner, workspace, main)
    await seed_term(workspace, active=True)
    accepted = await accept_task_command(**{**kwargs, "attachments": ["reference-image"]})
    lease = await reserve_run(accepted["execution_session_id"], owner)
    await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    transfers = []
    async def refuse(*args, **kwargs):
        transfers.append(True)
        raise SandboxSubscriptionRequired()
    monkeypatch.setattr("sandbox.assets.deliver_asset_ids", refuse)
    try:
        delivered = await inbox.deliver_claimed_attachments(lease)
        assert not delivered.runnable_item_ids and len(transfers) == 1
        async with get_db_session() as db:
            item = await db.get(AgentInboxItem, accepted["inbox_id"])
            assert item.outcome == "delivery_error" and item.delivery_attempts == 1
            assert item.delivery_last_error["retryable"] is False
            assert item.attachments == ["reference-image"]
            message = await db.get(Message, delivered.result_message_id)
            assert message.error["code"] == SandboxSubscriptionRequired.code
            assert "参考附件已保留" in message.error["message"]
            assert await db.scalar(select(func.count()).select_from(Part).where(
                Part.message_id == message.id)) == 0
            result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
            assert SandboxSubscriptionRequired.code in result.summary
            result_id, message_id = result.id, message.id
        actor = dict(user_id=owner, workspace_id=workspace, main_id=main.id)
        task = await get_task(**actor, task_id=accepted["task_id"])
        assert task["latest_result"]["execution_error"]["code"] == SandboxSubscriptionRequired.code
        assert task["latest_result"]["last_error_code"] is None  # Reporting is a separate channel.
        from types import SimpleNamespace
        from voice.work import overview
        spoken = await overview(SimpleNamespace(user_id=owner, workspace_id=workspace,
                                               main_session_id=main.id), {})
        visible = next(item for item in spoken["items"] if item["session_id"] == lease.session_id)
        assert visible["execution_error"]["code"] == SandboxSubscriptionRequired.code
        for summary in (False, True):
            read = await read_result_sources(**actor, result_id=result_id, summary=summary)
            assert read["execution_error"]["retryable"] is False
        page = await read_history(**actor, session_id=lease.session_id, message_ids=[message_id])
        assert len(page["items"]) == 1 and page["items"][0]["type"] == "error"
        assert SandboxSubscriptionRequired.code in page["items"][0]["text"]
        receipt = await deliver_task_result(result_id)
        async with get_db_session() as db:
            report = await db.get(AgentInboxItem, receipt["inbox_id"])
            assert SandboxSubscriptionRequired.code in report.prompt
            assert "non-retryable" in report.prompt
        await inbox.deliver_claimed_attachments(lease)
        assert len(transfers) == 1
    finally:
        await lease.release(session_status="idle")


async def test_access_status_does_not_equate_credits_with_execution_rights(monkeypatch):
    owner, _, workspace, main, _ = await setup_task()
    await seed_term(workspace, active=False)
    async with get_db_session() as db:
        status = await sandbox_access_status(db, workspace)
        assert not status["allowed"] and status["code"] == SandboxSubscriptionRequired.code
    from assistant.status_tools import resources
    async def running(_):
        return {"status": "running", "updated_at": datetime.now(timezone.utc)}
    monkeypatch.setattr("db.repository.cloud_desktop_repo.cloud_desktop_repo.get_for_workspace", running)
    view = await resources(user_id=owner, workspace_id=workspace, main_id=main.id)
    assert view["cloud_desktop"]["state"] == "running"
    assert not view["execution_access"]["allowed"]
    monkeypatch.setattr("sandbox.entitlement.subscription_sandbox_enabled", lambda: False)
    async with get_db_session() as db:
        assert (await sandbox_access_status(db, workspace))["allowed"]


def test_error_projection_keeps_known_cause_but_never_copies_private_debug_fields():
    from assistant.execution_errors import execution_error_view
    result = execution_error_view({
        "code": "ATTACHMENT_DELIVERY_FAILED", "message": "Attachment transfer did not complete.",
        "stack": "private-stack", "last_error": {"code": "delivery_failed", "retryable": True,
                                               "url": "https://private.invalid/?secret=value"}})
    assert result == {"code": "ATTACHMENT_DELIVERY_FAILED", "message": "Attachment transfer did not complete.",
                      "reason_code": "delivery_failed", "retryable": True}
