"""Commercial rules use real grants, order settlement and workspace permissions."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from api.billing import router
from auth.middleware import get_current_user
from billing import providers, service, subscriptions, payments
from billing.payments import settle_payment
from billing.plans import plan_catalog
from billing.providers import Checkout, PaidReceipt
from billing.service import UsageMeter
from billing.subscriptions import add_months
from core.identifier import ascending
from db.base import get_db_session
from db.models.billing import BillingSubscription, CreditBalance, CreditLedger, PaymentOrder
from db.models.workspace import WorkspaceMember
from db.repository.user_repo import PgUserRepo
from session.session import create_session


@pytest.fixture
async def shop(monkeypatch):
    instant = [datetime(2026, 9, 5, 1, tzinfo=timezone.utc)]
    for module in (service, subscriptions, payments):
        monkeypatch.setattr(module, "now", lambda: instant[0])
    monkeypatch.delenv("BILLING_PLANS_FILE", raising=False)
    monkeypatch.setenv("BILLING_MODE", "enforce")
    monkeypatch.setenv("PAYMENT_PROVIDERS_JSON", "{}")
    monkeypatch.setenv("PAYMENT_PUBLIC_BASE_URL", "https://app.example.test")
    checkouts = []

    class Provider:
        display_name = "Test checkout"
        async def create_checkout(self, **kwargs):
            checkouts.append(kwargs)
            return Checkout("https://checkout.example.test/" + kwargs["order_id"], kwargs["order_id"])

    monkeypatch.setattr(providers, "_registered", {"test": Provider()})
    user = await PgUserRepo().create(id=ascending("u"), username=uuid4().hex, password_hash="unused")
    session = await create_session(user_id=user["id"], title="Plan usage", model="gpt-5.6-luna")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user["id"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(client=client, app=app, user=user, session=session, clock=instant,
                              checkouts=checkouts)


async def subscribe(shop, plan="pro", cycle="monthly", key=None):
    response = await shop.client.post("/api/billing/orders", json={
        "kind": "subscription", "plan_id": plan, "cycle": cycle,
        "provider": "test", "request_key": key or uuid4().hex,
    })
    assert response.status_code == 200, response.text
    return response.json()


async def pay(order, payment_id=None):
    return await settle_payment("test", PaidReceipt(order_id=order["id"], payment_id=payment_id or uuid4().hex,
        amount_fen=order["amount_fen"], currency="CNY", status="paid"))


async def balance(shop):
    response = await shop.client.get("/api/billing/balance")
    assert response.status_code == 200, response.text
    return Decimal(response.json()["balance"])


async def test_free_weekly_allowance_and_model_use_share_one_grant(shop):
    assert await balance(shop) == 10
    assert await balance(shop) == 10
    assert (await shop.client.get("/api/billing/subscription")).json()["plan_id"] == "free"
    response = await shop.client.post("/api/billing/orders", json={
        "provider": "test", "amount_fen": 1000, "request_key": uuid4().hex,
    })
    assert response.status_code == 403 and response.json()["detail"]["code"] == "PAID_PLAN_REQUIRED"
    shop.clock[0] = datetime(2026, 9, 6, 15, 59, tzinfo=timezone.utc)
    assert await balance(shop) == 10
    shop.clock[0] = datetime(2026, 9, 6, 16, tzinfo=timezone.utc)
    meter = await UsageMeter.start(model_id=shop.session.model, session_id=shop.session.id)
    await meter.finish({"input": 1000, "output": 100})
    assert await balance(shop) == Decimal("19.99968")
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(CreditLedger).where(
            CreditLedger.workspace_id == shop.user["default_workspace_id"], CreditLedger.kind == "allowance")) == 2


async def test_catalog_prices_paid_activation_and_monthly_yearly_grants(shop):
    catalog = (await shop.client.get("/api/billing/plans")).json()
    assert [(p["id"], p["prices_fen"], p["credits"]) for p in catalog["plans"]] == [
        ("free", {"monthly": 0, "yearly": 0}, "10"),
        ("pro", {"monthly": 49900, "yearly": 598800}, "280"),
        ("max", {"monthly": 199900, "yearly": 2398800}, "1680"),
    ]
    order = await subscribe(shop, cycle="yearly")
    assert order["amount_fen"] == 598800 and Decimal(order["credits"]) == 280
    assert await balance(shop) == 10  # checkout creation does not activate the plan
    assert await pay(order, "annual-one") == {"accepted": True, "duplicate": False}
    assert await pay(order, "annual-one") == {"accepted": True, "duplicate": True}
    assert await balance(shop) == 290
    status = (await shop.client.get("/api/billing/subscription")).json()
    assert status["plan_id"] == "pro" and status["topup_allowed"] is True
    assert status["ends_at"].startswith("2027-09-05T01:00:00")
    shop.clock[0] = datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
    assert await balance(shop) == 570
    assert await balance(shop) == 570
    shop.clock[0] = datetime(2027, 9, 5, 1, tzinfo=timezone.utc)
    status = (await shop.client.get("/api/billing/subscription")).json()
    assert status["plan_id"] == "free" and status["topup_allowed"] is False
    assert await balance(shop) == 580


@pytest.mark.parametrize("catalog_file", [None, "plans.payment-test.json"])
@pytest.mark.parametrize("plan_id,credits", [("pro", Decimal(280)), ("max", Decimal(1680))])
@pytest.mark.parametrize("cycle", ["monthly", "yearly"])
async def test_catalog_prices_and_payment_test_override_preserve_entitlements(
    shop, monkeypatch, catalog_file, plan_id, credits, cycle,
):
    if catalog_file:
        monkeypatch.setenv("BILLING_PLANS_FILE", str(Path(__file__).parents[2] / "billing" / catalog_file))
    expected_prices = {"monthly": 10, "yearly": 10} if catalog_file else {
        "pro": {"monthly": 49900, "yearly": 598800},
        "max": {"monthly": 199900, "yearly": 2398800},
    }[plan_id]
    catalog = (await shop.client.get("/api/billing/plans")).json()
    assert next(plan for plan in catalog["plans"] if plan["id"] == "free")["prices_fen"] == {
        "monthly": 0, "yearly": 0,
    }
    assert catalog["topup"] == {
        "min_amount_fen": 100, "max_amount_fen": 10_000_000,
        "presets_fen": [1000, 5000, 10000, 50000], "credits_per_yuan": "1",
    }
    order = await subscribe(shop, plan=plan_id, cycle=cycle)
    assert order["amount_fen"] == expected_prices[cycle] and Decimal(order["credits"]) == credits
    assert shop.checkouts[-1]["order_id"] == order["id"]
    assert shop.checkouts[-1]["amount_fen"] == expected_prices[cycle]
    await pay(order)
    async with get_db_session() as db:
        saved = await db.get(PaymentOrder, order["id"])
        term = await db.get(BillingSubscription, order["id"])
        assert saved.product["version"] == catalog["version"]
        assert term.plan["prices_fen"] == expected_prices
        assert Decimal(term.plan["credits"]) == credits and term.plan["credit_period"] == "monthly"
    status = (await shop.client.get("/api/billing/subscription")).json()
    assert status["plan_id"] == plan_id and status["cycle"] == cycle
    assert status["ends_at"] == add_months(shop.clock[0], 12 if cycle == "yearly" else 1).isoformat()
    assert await balance(shop) == credits


async def test_tampered_order_prices_invalid_plans_and_member_orders_are_rejected(shop):
    body = {"kind": "subscription", "plan_id": "max", "cycle": "monthly", "provider": "test", "request_key": uuid4().hex}
    for altered in ({"amount_fen": 1}, {"credits": "999999"}, {"plan_id": "free"}, {"cycle": "daily"}):
        assert (await shop.client.post("/api/billing/orders", json={**body, **altered})).status_code == 422
    async with get_db_session() as db:
        member = await db.get(WorkspaceMember, (shop.user["default_workspace_id"], shop.user["id"]))
        member.role = "member"
    assert (await shop.client.post("/api/billing/orders", json=body)).status_code == 403
    assert (await shop.client.get("/api/billing/subscription")).json()["can_manage"] is False


async def test_paid_topups_preserve_subscription_and_enforce_amount_rules(shop):
    await pay(await subscribe(shop))
    before = (await shop.client.get("/api/billing/subscription")).json()
    assert await balance(shop) == 280
    for amount in (99, 10_000_001, 1.5, True):
        response = await shop.client.post("/api/billing/orders", json={"provider": "test", "amount_fen": amount, "request_key": uuid4().hex})
        assert response.status_code == 422
    order = (await shop.client.post("/api/billing/orders", json={"provider": "test", "amount_fen": 125, "request_key": uuid4().hex})).json()
    assert Decimal(order["credits"]) == Decimal("1.25")
    await pay(order)
    assert await balance(shop) == Decimal("281.25")
    assert (await shop.client.get("/api/billing/subscription")).json() == before


async def test_renewal_keeps_paid_time_and_grants_only_when_term_starts(shop):
    first = await subscribe(shop)
    await pay(first)
    assert await balance(shop) == 280
    key = uuid4().hex
    second = await subscribe(shop, plan="max", key=key)
    assert (await subscribe(shop, plan="max", key=key))["id"] == second["id"]
    await pay(second)
    assert await balance(shop) == 280
    status = (await shop.client.get("/api/billing/subscription")).json()
    assert status["plan_id"] == "pro"
    assert len(status["queued"]) == 1
    assert status["queued"][0]["starts_at"] == status["ends_at"]
    assert status["queued"][0]["ends_at"].startswith("2026-11-05T01:00:00")
    shop.clock[0] = datetime(2026, 10, 5, 1, tzinfo=timezone.utc)
    assert await balance(shop) == 1960
    status = (await shop.client.get("/api/billing/subscription")).json()
    assert status["plan_id"] == "max" and not status["queued"]


async def test_pending_subscription_keeps_price_snapshot_after_catalog_change(shop, monkeypatch):
    key = uuid4().hex
    order = await subscribe(shop, key=key)
    changed = plan_catalog().model_copy(deep=True)
    changed.plan("pro").credits = Decimal(999)
    changed.plan("pro").prices_fen["monthly"] = 123400
    monkeypatch.setattr(payments, "plan_catalog", lambda: changed)
    assert (await subscribe(shop, key=key))["amount_fen"] == 49900
    await pay(order)
    assert await balance(shop) == 280
    async with get_db_session() as db:
        assert (await db.get(BillingSubscription, order["id"])).plan["credits"] == "280"


async def test_price_restoration_keeps_existing_orders_and_prices_new_orders_correctly(shop, monkeypatch):
    current = plan_catalog()
    previous = current.model_copy(deep=True)
    previous.version = "temporary-test-pricing"
    previous.plan("pro").prices_fen = {"monthly": 10, "yearly": 10}
    monkeypatch.setattr(payments, "plan_catalog", lambda: previous)
    key = uuid4().hex
    old_order = await subscribe(shop, key=key)
    assert old_order["amount_fen"] == 10
    monkeypatch.setattr(payments, "plan_catalog", lambda: current)
    assert (await subscribe(shop, key=key))["amount_fen"] == 10
    new_order = await subscribe(shop)
    assert new_order["id"] != old_order["id"] and new_order["amount_fen"] == 49900
    await pay(old_order)
    async with get_db_session() as db:
        assert (await db.get(BillingSubscription, old_order["id"])).plan["prices_fen"]["monthly"] == 10
        assert (await db.get(PaymentOrder, new_order["id"])).status == "pending"


def test_month_end_and_leap_year_terms_are_clamped():
    assert add_months(datetime(2026, 1, 31, 1, tzinfo=timezone.utc), 1) == datetime(2026, 2, 28, 1, tzinfo=timezone.utc)
    assert add_months(datetime(2028, 2, 29, 1, tzinfo=timezone.utc), 12) == datetime(2029, 2, 28, 1, tzinfo=timezone.utc)

async def test_repeated_package_clicks_reuse_pending_order_and_all_retry_keys_survive_payment(shop):
    keys = [uuid4().hex for _ in range(3)]
    orders = [await subscribe(shop, key=key) for key in keys]
    assert len({order["id"] for order in orders}) == 1
    assert (await shop.client.get("/api/billing/orders")).json()["total"] == 1
    await pay(orders[0])
    for key in keys:
        replay = await subscribe(shop, key=key)
        assert replay["id"] == orders[0]["id"] and replay["status"] == "paid" and replay["checkout_url"] is None
    assert await balance(shop) == 280
    # A deliberate new renewal after settlement remains possible.
    assert (await subscribe(shop))["id"] != orders[0]["id"]


async def test_closed_retry_keys_stay_closed_and_product_changes_are_not_deduplicated(shop):
    from billing.payments import apply_receipt
    from billing.providers import CancelledReceipt
    keys = [uuid4().hex, uuid4().hex]
    first, retry = [await subscribe(shop, key=key) for key in keys]
    assert first["id"] == retry["id"]
    assert (await subscribe(shop, cycle="yearly"))["id"] != first["id"]
    assert (await subscribe(shop, plan="max"))["id"] != first["id"]
    await apply_receipt("test", CancelledReceipt(order_id=first["id"], payment_id="closed", amount_fen=first["amount_fen"], currency="CNY", status="cancelled"))
    for key in keys:
        assert (await subscribe(shop, key=key))["status"] == "cancelled"
    assert (await subscribe(shop))["id"] != first["id"]
    conflict = await shop.client.post("/api/billing/orders", json={
        "kind": "subscription", "plan_id": "max", "cycle": "monthly", "provider": "test", "request_key": keys[1]})
    assert conflict.status_code == 409
