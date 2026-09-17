"""Real payment/DB/access paths; cloud operations are always mocked (no spend)."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, update, text

from billing import payments, service, subscriptions
from billing.plans import plan_catalog
from billing.providers import PaidReceipt
from db.base import get_db_session
from db.models.billing import PaymentOrder, BillingSubscription
from db.models.desktop_activation import DesktopActivation
from db.repository.user_repo import PgUserRepo
from sandbox import desktop_activation as activation, entitlement, wuying_ecd as ecd
from sandbox.channel import wuying_channel
from sandbox.client import SandboxClient
from sandbox.desktop_activation import DesktopActivationService, activation_status
from sandbox.entitlement import SandboxSubscriptionRequired
from sandbox.wuying_desktop_service import wuying_desktop_service
from sandbox.wuying_ecd import create_desktop as create_cloud_desktop


@pytest.fixture
async def account(monkeypatch):
    clock = [datetime(2026, 9, 7, tzinfo=timezone.utc)]
    for module in (service, subscriptions, payments, activation, entitlement):
        monkeypatch.setattr(module, "now", lambda: clock[0])
    config = SimpleNamespace(sandbox_provider="wuying", wuying_routing="per_desktop",
        wuying_region_id="cn-hangzhou", pool_enabled=False, pool_assign_on_provision=True)
    monkeypatch.setattr(entitlement, "get_config", lambda: config)
    monkeypatch.setattr(activation, "get_config", lambda: config)
    user = await PgUserRepo().create(id=uuid4().hex, username=uuid4().hex, password_hash="test")
    user_id, workspace_id = user["id"], user["default_workspace_id"]
    remote = {}

    async def create(workspace, **kwargs):
        assert workspace == workspace_id and kwargs["monthly"] is True
        await kwargs["before_submit"]()
        remote["desktop"] = {"desktop_id": "ecd-" + workspace_id, "status": "Running",
            "charge_type": "PrePaid", "expired_time": (clock[0] + timedelta(days=31)).isoformat()}
        return remote["desktop"]["desktop_id"]

    async def install(record, **kwargs):
        assert isinstance(record, dict)
        await activation.desktops.update(record["id"], action_api_key_ciphertext="encrypted", tunnel_state="pending")
        return await activation.desktops.get(record["id"])

    async def verify(record, **kwargs):
        assert isinstance(record, dict)
        await activation.desktops.update(record["id"], tunnel_state="up")
        return await activation.desktops.get(record["id"])

    monkeypatch.setattr(ecd, "create_desktop", AsyncMock(side_effect=create))
    monkeypatch.setattr(ecd, "list_desktops", AsyncMock(side_effect=lambda **kw: list(remote.values())))
    monkeypatch.setattr(ecd, "describe_desktop", AsyncMock(side_effect=lambda did: remote.get("desktop")))
    monkeypatch.setattr(ecd, "ensure_end_user", AsyncMock(return_value=(ecd.eu_id_for(workspace_id), "password")))
    for name in ("modify_entitlement", "disconnect_desktop_sessions", "wait_desktop_ready", "start_desktop", "tag_desktop", "renew_desktop"):
        monkeypatch.setattr(ecd, name, AsyncMock())
    monkeypatch.setattr(ecd, "verify_ownership", AsyncMock(return_value=ecd.eu_id_for(workspace_id)))
    monkeypatch.setattr(wuying_channel, "install", AsyncMock(side_effect=install))
    monkeypatch.setattr(wuying_channel, "verify", AsyncMock(side_effect=verify))
    return SimpleNamespace(user_id=user_id, workspace_id=workspace_id, clock=clock, remote=remote, config=config)


async def order_for(account, *, kind="subscription", paid=True):
    plan = plan_catalog().plan("pro")
    amount_fen = plan.prices_fen["monthly"] if kind == "subscription" else 49900
    order_id = uuid4().hex
    async with get_db_session() as db:
        db.add(PaymentOrder(id=order_id, workspace_id=account.workspace_id, user_id=account.user_id,
            provider="test", request_key=uuid4().hex, amount_fen=amount_fen, currency="CNY", kind=kind,
            product={"plan": plan.model_dump(mode="json"), "cycle": "monthly"} if kind == "subscription" else None,
            credits=plan.credits, status="pending", created_at=account.clock[0]))
    receipt = PaidReceipt(order_id=order_id, payment_id=uuid4().hex, amount_fen=amount_fen, currency="CNY", status="paid")
    if paid:
        await payments.settle_payment("test", receipt)
    return receipt


async def job_for(account):
    async with get_db_session() as db:
        return await db.get(DesktopActivation, account.workspace_id)


async def make_due(account):
    async with get_db_session() as db:
        row = await db.get(DesktopActivation, account.workspace_id)
        row.next_run_at = account.clock[0]


async def test_free_status_is_read_only_and_provision_denied(account):
    await order_for(account, paid=False)
    assert (await activation_status(account.workspace_id))["state"] == "subscription_required"
    assert await job_for(account) is None
    with pytest.raises(SandboxSubscriptionRequired):
        await wuying_desktop_service.provision(account.workspace_id)
    ecd.create_desktop.assert_not_called()
    ecd.list_desktops.assert_not_called()


async def test_free_late_topup_does_not_enable_desktop(account):
    await order_for(account, kind="topup")
    assert await job_for(account) is None
    ecd.create_desktop.assert_not_called()


async def test_verified_payment_outbox_is_atomic_and_idempotent(account, monkeypatch):
    receipt = await order_for(account, paid=False)
    original = activation.enqueue_paid_activation
    async def fail_after_enqueue(db, order):
        await original(db, order)
        raise RuntimeError("crash before commit")
    monkeypatch.setattr(activation, "enqueue_paid_activation", fail_after_enqueue)
    with pytest.raises(RuntimeError):
        await payments.settle_payment("test", receipt)
    async with get_db_session() as db:
        assert (await db.get(PaymentOrder, receipt.order_id)).status == "pending"
        assert await db.get(BillingSubscription, receipt.order_id) is None
        assert await db.get(DesktopActivation, account.workspace_id) is None
    monkeypatch.setattr(activation, "enqueue_paid_activation", original)
    await payments.settle_payment("test", receipt)
    assert (await job_for(account)).state == "queued"
    assert (await job_for(account)).request_id == receipt.order_id
    assert (await payments.settle_payment("test", receipt))["duplicate"] is True
    ecd.create_desktop.assert_not_called()  # Payment never does cloud I/O.


async def test_new_process_resumes_without_status_request_and_reuses_on_renewal(account):
    await order_for(account)
    worker = DesktopActivationService()
    assert account.workspace_id in await worker.due()
    assert await worker.process(account.workspace_id)
    before = await activation.desktops.get_for_workspace(account.workspace_id)
    assert before["status"] == "running"
    assert (await activation_status(account.workspace_id))["state"] == "running"
    account.clock[0] += timedelta(days=32)
    with pytest.raises(SandboxSubscriptionRequired):
        await entitlement.require_sandbox_subscription(account.workspace_id)
    assert await DesktopActivationService().process(account.workspace_id)
    expired = await activation.desktops.get_for_workspace(account.workspace_id)
    assert expired["desktop_id"] == before["desktop_id"] and expired["workspace_id"] == account.workspace_id
    ecd.modify_entitlement.assert_awaited_with(before["desktop_id"], [])
    ecd.disconnect_desktop_sessions.assert_awaited_once()
    assert (await activation_status(account.workspace_id))["state"] == "subscription_required"
    account.remote["desktop"]["expired_time"] = (account.clock[0] + timedelta(days=31)).isoformat()
    await order_for(account)
    assert await DesktopActivationService().process(account.workspace_id)
    assert (await activation.desktops.get_for_workspace(account.workspace_id))["id"] == before["id"]
    ecd.create_desktop.assert_awaited_once()
    ecd.modify_entitlement.assert_awaited_with(before["desktop_id"], list(ecd.ensure_end_user.return_value[:1]))


async def test_crash_after_cloud_creation_recovers_tags_without_rebuy(account, monkeypatch):
    await order_for(account)
    original_create = ecd.create_desktop.side_effect
    async def create_and_crash(*args, **kwargs):
        await original_create(*args, **kwargs)
        raise asyncio.CancelledError()
    ecd.create_desktop.side_effect = create_and_crash
    with pytest.raises(asyncio.CancelledError):
        await DesktopActivationService().process(account.workspace_id)
    assert (await job_for(account)).purchase_kind == "create"
    assert await DesktopActivationService().process(account.workspace_id)
    ecd.create_desktop.assert_awaited_once()
    assert (await job_for(account)).state == "ready"


async def test_browser_runtime_failure_retries_same_desktop_without_repurchase(account):
    from sandbox.browser_runtime import BrowserRuntimeUnavailable
    await order_for(account)
    verify = wuying_channel.verify.side_effect
    wuying_channel.verify.side_effect = BrowserRuntimeUnavailable('dependency install failed')
    assert not await DesktopActivationService().process(account.workspace_id)
    assert (await job_for(account)).state != 'ready'
    before = await activation.desktops.get_for_workspace(account.workspace_id)
    assert before['status'] != 'running'
    wuying_channel.verify.side_effect = verify
    await make_due(account)
    assert await DesktopActivationService().process(account.workspace_id)
    after = await activation.desktops.get_for_workspace(account.workspace_id)
    assert after['desktop_id'] == before['desktop_id']
    assert (await job_for(account)).state == 'ready'
    ecd.create_desktop.assert_awaited_once()


async def test_unknown_purchase_result_is_reconciled_not_repeated(account):
    await order_for(account)
    async def timeout(workspace, **kwargs):
        await kwargs["before_submit"]()
        raise TimeoutError("lost response")
    ecd.create_desktop.side_effect = timeout
    assert not await DesktopActivationService().process(account.workspace_id)
    await make_due(account)
    assert not await DesktopActivationService().process(account.workspace_id)
    assert (await job_for(account)).state == "needs_attention"
    await activation.retry_activation(account.workspace_id)
    assert not await DesktopActivationService().process(account.workspace_id)
    ecd.create_desktop.assert_awaited_once()


async def test_channel_install_interruption_replays_with_same_desktop(account):
    await order_for(account)
    original_install = wuying_channel.install.side_effect
    async def partial(record, **kwargs):
        await original_install(record)
        raise ConnectionError("script delivery interrupted")
    wuying_channel.install.side_effect = partial
    assert not await DesktopActivationService().process(account.workspace_id)
    before = await activation.desktops.get_for_workspace(account.workspace_id)
    wuying_channel.install.side_effect = original_install
    await make_due(account)
    assert await DesktopActivationService().process(account.workspace_id)
    assert wuying_channel.install.await_count == 2
    assert (await activation.desktops.get_for_workspace(account.workspace_id))["id"] == before["id"]
    ecd.create_desktop.assert_awaited_once()


async def test_lease_blocks_other_worker_then_recovers_after_process_death(account):
    await order_for(account)
    async with get_db_session() as db:
        job = await db.get(DesktopActivation, account.workspace_id)
        job.lease_owner = "dead-process"
        job.lease_until = account.clock[0] + timedelta(seconds=90)
    worker = DesktopActivationService()
    assert not await worker.process(account.workspace_id)
    ecd.create_desktop.assert_not_called()
    account.clock[0] += timedelta(seconds=91)
    assert await worker.process(account.workspace_id)
    ecd.create_desktop.assert_awaited_once()


async def test_two_live_workers_cannot_duplicate_create(account):
    await order_for(account)
    entered, release = asyncio.Event(), asyncio.Event()
    original_create = ecd.create_desktop.side_effect
    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_create(*args, **kwargs)
    ecd.create_desktop.side_effect = slow
    task = asyncio.create_task(DesktopActivationService().process(account.workspace_id))
    await entered.wait()
    assert not await DesktopActivationService().process(account.workspace_id)
    release.set()
    assert await task
    ecd.create_desktop.assert_awaited_once()


async def test_cached_client_rechecks_subscription_even_with_billing_off(account, monkeypatch):
    await order_for(account)
    monkeypatch.setenv("BILLING_MODE", "off")
    client = SandboxClient("invalid.example", 1, "test", workspace_id=account.workspace_id)
    await client._authorize_request(httpx.Request("GET", "http://unused"))
    account.clock[0] += timedelta(days=32)
    with pytest.raises(SandboxSubscriptionRequired):
        await client.execute("echo must-not-run")
    with pytest.raises(SandboxSubscriptionRequired):
        await client.upload_skill_archive(b"test", "test.zip")


async def test_backfill_recovers_historical_paid_order(account):
    await order_for(account)
    async with get_db_session() as db:
        await db.execute(delete(DesktopActivation).where(DesktopActivation.workspace_id == account.workspace_id))
    await DesktopActivationService().backfill()
    assert await job_for(account) is not None
    assert await DesktopActivationService().process(account.workspace_id)


async def test_expiry_during_setup_revokes_on_next_sweep(account):
    await order_for(account)
    async def expire(*args, **kwargs):
        account.clock[0] += timedelta(seconds=1)
        async with get_db_session() as db:
            await db.execute(update(BillingSubscription).where(
                BillingSubscription.workspace_id == account.workspace_id,
            ).values(ends_at=account.clock[0]))
    ecd.wait_desktop_ready.side_effect = expire
    assert not await DesktopActivationService().process(account.workspace_id)
    assert await DesktopActivationService().process(account.workspace_id)
    assert (await job_for(account)).state == "suspended"
    ecd.disconnect_desktop_sessions.assert_awaited_once()


@pytest.mark.parametrize("unavailable", ["free", "preparing"])
async def test_real_agent_loop_can_reply_without_sandbox(account, monkeypatch, unavailable):
    from agent import loop, processor
    from sandbox import sandbox_manager
    from sandbox.wuying_desktop_service import DesktopNotReady
    from session.session import create_session, create_user_message, get_messages

    if unavailable == "preparing":
        await order_for(account)
    # kv_store is migration-owned, not part of ORM create_all in the fixture.
    async with get_db_session() as db:
        await db.execute(text("CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"))
    session = await create_session(user_id=account.user_id, title="Plain chat", model="gpt-5.6-luna")
    await create_user_message(session_id=session.id, text="你好", agent="build", model=session.model, user_id=account.user_id)
    denied = SandboxSubscriptionRequired() if unavailable == "free" else DesktopNotReady({"state": "creating"})
    monkeypatch.setattr(sandbox_manager, "get_client", AsyncMock(side_effect=denied))
    monkeypatch.setattr(loop, "_ensure_title", AsyncMock())
    monkeypatch.setattr("agent.suggestions.generate_suggestions", AsyncMock())
    calls = []
    async def stream(**kwargs):
        calls.append(kwargs)
        assert kwargs["ctx"].sandbox is None
        assert kwargs["ctx"].sandbox_error["code"] == (
            "SANDBOX_SUBSCRIPTION_REQUIRED" if unavailable == "free" else "DESKTOP_NOT_READY")
        yield {"type": "text_delta", "text": "你好，可以正常聊天。"}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 1, "output": 1}}
    monkeypatch.setattr(processor, "stream_llm", stream)
    result = await loop.run_loop(session.id, user_id=account.user_id)
    if loop._background_tasks:
        await asyncio.gather(*list(loop._background_tasks))
    assert result is not None
    assert len(calls) == 1
    messages = await get_messages(session.id, user_id=account.user_id)
    assert any(m.finish == "stop" for m in messages)
    assert any(getattr(p, "text", "") == "你好，可以正常聊天。" for m in messages for p in m.parts)
    ecd.create_desktop.assert_not_called()


async def test_sandbox_tool_is_normal_error_but_non_sandbox_tool_still_runs(account):
    from pydantic import BaseModel
    from tool.tool import ToolContext, ToolResult, define_tool
    class Args(BaseModel):
        pass
    executor = AsyncMock(return_value=ToolResult(output="ok"))
    context = ToolContext(workspace_id=account.workspace_id, sandbox_error=SandboxSubscriptionRequired().payload)
    required = define_tool("test_sandbox", description="test", parameters=Args, execute=executor)
    result = await required.execute({}, context)
    assert result.metadata["error"] is True
    assert result.metadata["code"] == "SANDBOX_SUBSCRIPTION_REQUIRED"
    executor.assert_not_called()
    ordinary = define_tool("test_ordinary", description="test", parameters=Args, execute=executor, sandbox_required=False)
    assert (await ordinary.execute({}, context)).output == "ok"


async def test_real_provider_denies_cached_routes_and_unauthenticated_preview_after_expiry(account, monkeypatch):
    from core import config as config_module
    from core.config import OpenBoxConfig
    from sandbox.wuying import WuyingProvider
    monkeypatch.setattr(config_module, "get_config", lambda: OpenBoxConfig(wuying_routing="per_desktop"))
    provider = WuyingProvider()
    with pytest.raises(SandboxSubscriptionRequired):
        await provider.resolve_user_container(account.workspace_id)
    await order_for(account)
    await DesktopActivationService().process(account.workspace_id)
    record = await activation.desktops.get_for_workspace(account.workspace_id)
    account.clock[0] += timedelta(days=32)
    for action in (provider.resolve_user_container(account.workspace_id),
                   provider.get_container(record["desktop_id"]),
                   provider.forward_to_container(record["desktop_id"], "GET", "/files")):
        with pytest.raises(SandboxSubscriptionRequired):
            await action


async def test_monthly_cloud_purchase_uses_prepaid_one_month_and_no_auto_renew(account, monkeypatch):
    from core.config import OpenBoxConfig
    config = OpenBoxConfig(wuying_image_id="image-test", wuying_office_site_id="office-test",
        wuying_policy_group_id="policy-test", wuying_charge_type="PostPaid", wuying_auto_renew=True,
        wuying_period=3, wuying_period_unit="Year")
    monkeypatch.setattr(ecd, "get_config", lambda: config)
    before = AsyncMock()
    async def create(request):
        before.assert_awaited_once()
        assert (request.charge_type, request.period, request.period_unit) == ("PrePaid", 1, "Month")
        assert request.amount == 1 and request.auto_pay and not request.auto_renew
        return SimpleNamespace(body=SimpleNamespace(desktop_id=["ecd-monthly"]))
    monkeypatch.setattr(ecd, "ecd_client", lambda: SimpleNamespace(create_desktops_async=create))
    assert await create_cloud_desktop(account.workspace_id, monthly=True, before_submit=before) == "ecd-monthly"


async def test_renewal_crash_uses_new_expiry_without_second_purchase(account):
    await order_for(account)
    worker = DesktopActivationService()
    assert await worker.process(account.workspace_id)
    account.clock[0] += timedelta(days=32)
    await order_for(account)
    async def renew_and_crash(*args, **kwargs):
        assert (await job_for(account)).purchase_kind == "renew"
        account.remote["desktop"]["expired_time"] = (account.clock[0] + timedelta(days=31)).isoformat()
        raise asyncio.CancelledError()
    ecd.renew_desktop.side_effect = renew_and_crash
    with pytest.raises(asyncio.CancelledError):
        await worker.process(account.workspace_id)
    assert await DesktopActivationService().process(account.workspace_id)
    assert (await job_for(account)).purchase_kind is None
    ecd.renew_desktop.assert_awaited_once()
    ecd.create_desktop.assert_awaited_once()


async def test_late_creation_after_expiry_is_still_revoked(account):
    await order_for(account)
    original_create = ecd.create_desktop.side_effect
    async def uncertain(workspace, **kwargs):
        await kwargs["before_submit"]()
        raise TimeoutError("no response")
    ecd.create_desktop.side_effect = uncertain
    await DesktopActivationService().process(account.workspace_id)
    account.clock[0] += timedelta(days=32)
    await DesktopActivationService().process(account.workspace_id)
    # ECD eventually exposes the result while this account is already Free.
    await original_create(account.workspace_id, monthly=True, before_submit=AsyncMock())
    await make_due(account)
    assert await DesktopActivationService().process(account.workspace_id)
    ecd.disconnect_desktop_sessions.assert_awaited_once()


async def test_retained_machine_ownership_is_checked_before_any_mutation(account):
    await order_for(account)
    assert await DesktopActivationService().process(account.workspace_id)
    account.clock[0] += timedelta(days=32)
    await order_for(account)
    ecd.modify_entitlement.reset_mock()
    ecd.start_desktop.reset_mock()
    ecd.verify_ownership.side_effect = ecd.DesktopOwnershipError("wrong workspace")
    assert not await DesktopActivationService().process(account.workspace_id)
    ecd.modify_entitlement.assert_not_called()
    ecd.renew_desktop.assert_not_called()
    ecd.start_desktop.assert_not_called()


async def test_http_free_access_and_ghost_ticket_never_release_retained_machine(account, monkeypatch):
    from main import create_app
    from api import desktop
    from auth.middleware import get_current_user

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: {"user_id": account.user_id, "workspace_id": account.workspace_id}
    monkeypatch.setattr(desktop, "get_config", lambda: account.config)
    sdk = SimpleNamespace(get_connection_ticket_async=AsyncMock(side_effect=RuntimeError("InvalidDesktopId.NotFound")))
    monkeypatch.setattr(desktop, "_ecd_client", lambda region: sdk)
    monkeypatch.setattr(wuying_desktop_service, "release_ghost", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/desktop/status")).json()["state"] == "subscription_required"
        for response in (await client.get("/api/desktop/ticket"), await client.post("/api/desktop/provision")):
            assert response.status_code == 403
            assert response.json()["code"] == "SANDBOX_SUBSCRIPTION_REQUIRED"
        sdk.get_connection_ticket_async.assert_not_called()
        await order_for(account)
        assert (await client.get("/api/desktop/ticket")).status_code == 202
        assert await DesktopActivationService().process(account.workspace_id)
        assert (await client.get("/api/desktop/ticket")).json()["reason"] == "retained_desktop_unavailable"
        wuying_desktop_service.release_ghost.assert_not_called()
        assert await activation.desktops.get_for_workspace(account.workspace_id) is not None

        async def expired_ticket(request):
            account.clock[0] += timedelta(days=32)
            return SimpleNamespace(body=SimpleNamespace(ticket="expired-ticket", task_id=None, task_status="FINISHED"))
        sdk.get_connection_ticket_async.side_effect = expired_ticket
        assert (await client.get("/api/desktop/ticket")).status_code == 403
