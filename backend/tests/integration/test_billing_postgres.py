"""Real PostgreSQL concurrency checks in a disposable, isolated local schema.

Enable with BILLING_TEST_POSTGRES_URL. This never writes application tables.
"""
import asyncio
import os
from importlib import import_module
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import db.base as base
from billing.payments import settle_payment
from billing.plans import plan_catalog
from billing.providers import PaidReceipt
from billing.service import BillingError, UsageMeter, now
from core.identifier import ascending
from db.models.billing import BillingSubscription, CreditBalance, CreditLedger, PaymentOrder
from db.repository.user_repo import PgUserRepo
from session.session import create_session


def create_migrated_schema(connection):
    billing_tables = {"credit_balances", "credit_ledger", "payment_orders", "usage_events", "billing_subscriptions", "payment_order_requests", "desktop_activations"}
    base.Base.metadata.create_all(connection, tables=[
        table for table in base.Base.metadata.sorted_tables if table.name not in billing_tables
    ])
    # Exercise the shipped migration, not ORM create_all for the billing tables.
    migration = import_module("db.migrations.versions.a8c0e2f4b6d9_credit_billing")
    with Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
        import_module("db.migrations.versions.b9d1f3a5c7e9_subscription_plans").upgrade()
        import_module("db.migrations.versions.c0e2f4a6b8d0_payment_request_aliases").upgrade()
        import_module("db.migrations.versions.d1f3a5b7c9e1_cancel_uncreated_orders").upgrade()
        import_module("db.migrations.versions.a4b6c8d0e2f5_desktop_activation_outbox").upgrade()
        import_module("db.migrations.versions.e3a5c7d9f1b2_admin_subscription_management").upgrade()


@pytest.fixture
async def postgres(monkeypatch):
    url = os.environ.get("BILLING_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Local PostgreSQL URL is not configured")
    assert make_url(url).host in {"localhost", "127.0.0.1"}
    schema = "billing_test_" + uuid4().hex
    admin = create_async_engine(url)
    previous_engine, previous_factory = base._engine, base._session_factory
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(create_migrated_schema)
        base._engine = engine
        base._session_factory = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setenv("BILLING_MODE", "enforce")
        yield
    finally:
        base._engine, base._session_factory = previous_engine, previous_factory
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def new_account():
    user = await PgUserRepo().create(id=ascending("u"), username=uuid4().hex, password_hash="unused")
    session = await create_session(user_id=user["id"], title="Concurrency", model="gpt-5.6-luna")
    return user, session


async def order_for(user):
    async with base.get_db_session() as db:
        order = PaymentOrder(id=ascending("pay"), workspace_id=user["default_workspace_id"], user_id=user["id"],
            request_key=uuid4().hex, provider="test", amount_fen=1000, currency="CNY", credits=Decimal(10),
            status="pending", created_at=now())
        db.add(order)
    return order


async def test_concurrent_receipts_and_usage_have_no_lost_or_duplicate_credits(postgres):
    user, session = await new_account()
    order = await order_for(user)
    receipt = PaidReceipt(order_id=order.id, payment_id="one-payment", amount_fen=1000, currency="CNY", status="paid")
    receipts = await asyncio.gather(*(settle_payment("test", receipt) for _ in range(12)))
    assert sum(not result["duplicate"] for result in receipts) == 1
    meters = await asyncio.gather(*(UsageMeter.start(model_id=session.model, session_id=session.id) for _ in range(12)))
    await asyncio.gather(*(meter.finish({"input": 1000, "output": 100}) for meter in meters))
    # Concurrent re-delivery of the same usage must also be idempotent.
    await asyncio.gather(*(meters[0].finish({"input": 1000, "output": 100}) for _ in range(12)))
    async with base.get_db_session() as db:
        account = await db.get(CreditBalance, user["default_workspace_id"])
        assert account.balance == Decimal("19.99616")  # top-up + one free weekly grant - usage
        assert await db.scalar(select(func.count()).select_from(CreditLedger)) == 14
        assert await db.scalar(select(func.sum(CreditLedger.amount))) == account.balance


async def test_same_payment_cannot_fund_two_workspaces_even_when_callbacks_race(postgres):
    one, _ = await new_account()
    two, _ = await new_account()
    orders = [await order_for(user) for user in (one, two)]
    receipts = [PaidReceipt(order_id=order.id, payment_id="shared-payment", amount_fen=1000, currency="CNY", status="paid") for order in orders]
    results = await asyncio.gather(*(settle_payment("test", receipt) for receipt in receipts), return_exceptions=True)
    assert sum(isinstance(result, BillingError) for result in results) == 1
    async with base.get_db_session() as db:
        assert await db.scalar(select(func.sum(CreditBalance.balance))) == 10
        assert await db.scalar(select(func.count()).select_from(CreditLedger)) == 1


async def test_subscription_callbacks_grant_once_and_preserve_queued_terms(postgres, monkeypatch):
    import sandbox.desktop_activation as activation
    monkeypatch.setattr(activation, "subscription_sandbox_enabled", lambda: True)
    user, _ = await new_account()

    async def plan_order(plan_id):
        plan = plan_catalog().plan(plan_id)
        async with base.get_db_session() as db:
            order = PaymentOrder(id=ascending("pay"), workspace_id=user["default_workspace_id"], user_id=user["id"],
                request_key=uuid4().hex, provider="test", amount_fen=plan.prices_fen["yearly"],
                currency="CNY", credits=plan.credits, kind="subscription", status="pending", created_at=now(),
                product={"plan": plan.model_dump(mode="json"), "cycle": "yearly", "version": "test"})
            db.add(order)
        return order

    one = await plan_order("pro")
    receipt = PaidReceipt(order_id=one.id, payment_id="subscription-one", amount_fen=one.amount_fen, currency="CNY", status="paid")
    results = await asyncio.gather(*(settle_payment("test", receipt) for _ in range(12)))
    assert sum(not result["duplicate"] for result in results) == 1
    next_orders = [await plan_order("max") for _ in range(2)]
    await asyncio.gather(*(settle_payment("test", PaidReceipt(order_id=order.id, payment_id=order.id,
        amount_fen=order.amount_fen, currency="CNY", status="paid")) for order in next_orders))
    async with base.get_db_session() as db:
        terms = (await db.scalars(select(BillingSubscription).order_by(BillingSubscription.starts_at))).all()
        assert len(terms) == 3
        assert terms[0].ends_at == terms[1].starts_at
        assert terms[1].ends_at == terms[2].starts_at
        assert (await db.get(CreditBalance, user["default_workspace_id"])).balance == 280
        assert await db.scalar(select(func.count()).select_from(CreditLedger)) == 1
        from db.models.desktop_activation import DesktopActivation
        assert await db.scalar(select(func.count()).select_from(DesktopActivation)) == 1
        job = await db.get(DesktopActivation, user["default_workspace_id"])
        assert job.state == "queued" and job.lease_until is None

    async with base.get_db_session() as db:
        await db.execute(delete(DesktopActivation))  # This test owns its isolated schema.
    await asyncio.gather(*(activation.DesktopActivationService().backfill() for _ in range(3)))
    async with base.get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(DesktopActivation)) == 1

    # Multiple worker processes contend on a real PostgreSQL UPDATE, not an
    # in-process lock. Only one may perform the cloud workflow.
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def advance(self, workspace_id, token):
        calls.append(token)
        entered.set()
        await release.wait()
        await self._save(workspace_id, token, state="ready", step="ready")
    monkeypatch.setattr(activation.DesktopActivationService, "_advance", advance)
    worker = activation.DesktopActivationService()
    task = asyncio.create_task(worker.process(user["default_workspace_id"]))
    await entered.wait()
    try:
        assert not any(await asyncio.gather(*(
            activation.DesktopActivationService().process(user["default_workspace_id"]) for _ in range(8)
        )))
    finally:
        release.set()
        await task
    assert len(calls) == 1

async def test_different_request_keys_race_to_one_pending_order_and_stay_idempotent(postgres, monkeypatch):
    from billing import providers
    from billing.payments import create_order
    from billing.providers import Checkout
    from db.models.billing import PaymentOrderRequest
    monkeypatch.setenv("PAYMENT_PUBLIC_BASE_URL", "https://app.example.test")
    monkeypatch.setenv("PAYMENT_PROVIDERS_JSON", "{}")
    monkeypatch.delenv("BILLING_PLANS_FILE", raising=False)
    class Provider:
        display_name = "Test"
        async def create_checkout(self, **kwargs):
            return Checkout("https://checkout.example.test/" + kwargs["order_id"], kwargs["order_id"])
    monkeypatch.setattr(providers, "_registered", {"test": Provider()})
    user, _ = await new_account()
    keys = [uuid4().hex for _ in range(12)]
    async def checkout(key):
        return await create_order(workspace_id=user["default_workspace_id"], user_id=user["id"],
            provider_name="test", request_key=key, kind="subscription", plan_id="pro", cycle="monthly")
    orders = await asyncio.gather(*(checkout(key) for key in keys))
    assert len({order["id"] for order in orders}) == 1
    order = orders[0]
    await settle_payment("test", PaidReceipt(order_id=order["id"], payment_id="concurrent-checkout",
        amount_fen=order["amount_fen"], currency="CNY", status="paid"))
    replayed = await asyncio.gather(*(checkout(key) for key in keys))
    assert all(item["id"] == order["id"] and item["status"] == "paid" for item in replayed)
    async with base.get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(PaymentOrderRequest).where(
            PaymentOrderRequest.order_id == order["id"])) == len(keys)
        assert (await db.get(CreditBalance, user["default_workspace_id"])).balance == 280


async def test_uncreated_cancellation_keeps_timezone_and_cannot_overwrite_a_racing_payment(postgres):
    from datetime import timedelta
    from billing.payments import cancel_uncreated_order, order_view
    from billing.providers import TradeNotCreated
    user, _ = await new_account()
    order = await order_for(user)
    await cancel_uncreated_order("test", TradeNotCreated(order.id))
    async with base.get_db_session() as db:
        saved = await db.get(PaymentOrder, order.id)
        assert saved.cancelled_at.utcoffset() == timedelta(0)
        assert order_view(saved)["reconcile_required"] is True
    receipt = PaidReceipt(order_id=order.id, payment_id="late-" + order.id, amount_fen=1000, currency="CNY", status="paid")
    await asyncio.gather(settle_payment("test", receipt), cancel_uncreated_order("test", TradeNotCreated(order.id)))
    async with base.get_db_session() as db:
        saved = await db.get(PaymentOrder, order.id)
        assert saved.status == "paid" and order_view(saved)["reconcile_required"] is False
        assert (await db.get(CreditBalance, user["default_workspace_id"])).balance == 10
