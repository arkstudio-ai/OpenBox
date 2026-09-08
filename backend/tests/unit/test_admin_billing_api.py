"""Operator billing views stay admin-only, read-only and free of payment links."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy
from fastapi import FastAPI
from sqlalchemy import select

from api.admin_billing import router
from auth.middleware import get_current_user
from db.base import get_db_session, get_engine
from db.models.audit_log import AuditLog
from db.models.billing import (
    BillingSubscription,
    CreditBalance,
    CreditLedger,
    PaymentOrder,
    UsageEvent,
)
from db.repository.user_repo import PgUserRepo


PATHS = ("/api/admin/billing/subscriptions", "/api/admin/billing/orders",
         "/api/admin/billing/workspaces/any")


def _client(identity: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: dict(identity)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _order(order_id, workspace_id, user_id, created_at, **fields) -> PaymentOrder:
    # Every order carries a checkout_url so the "never leaks" assertions bite.
    return PaymentOrder(
        id=order_id, workspace_id=workspace_id, user_id=user_id,
        request_key=uuid.uuid4().hex, provider="test", amount_fen=1000, currency="CNY",
        credits=Decimal("10"), checkout_url=f"https://checkout.test/{order_id}",
        provider_order_id=f"prov-{order_id}", created_at=created_at, **fields,
    )


@pytest.fixture
async def estate():
    """Three workspaces covering every subscription state, plus their orders."""
    suffix = uuid.uuid4().hex[:10]
    repo = PgUserRepo()
    operator = uuid.uuid4().hex[:10]
    admin = await repo.create(
        id=f"aops-{operator}", username=f"aops-{operator}",
        password_hash="unused", role="admin",
    )
    at = datetime.now(timezone.utc)
    accounts = {}
    for label in ("active", "expired", "free"):
        accounts[label] = await repo.create(
            id=f"abill-{label}-{suffix}", username=f"abill-{label}-{suffix}",
            password_hash="unused", email=f"{label}.{suffix}@paid.test",
        )
    live, lapsed, idle = (accounts[key] for key in ("active", "expired", "free"))
    live_ws = live["default_workspace_id"]
    ids = SimpleNamespace(
        live=f"ord-live-{suffix}", queued=f"ord-queue-{suffix}",
        lapsed=f"ord-lapsed-{suffix}", topup=f"ord-topup-{suffix}",
    )
    async with get_db_session() as db:
        db.add(_order(ids.live, live_ws, live["id"], at - timedelta(days=1),
                      kind="subscription", status="paid", paid_at=at - timedelta(days=1),
                      product={"plan": {"id": "pro"}, "cycle": "monthly"}))
        db.add(_order(ids.queued, live_ws, live["id"], at,
                      kind="subscription", status="paid", paid_at=at,
                      product={"plan": {"id": "pro"}, "cycle": "monthly"}))
        db.add(_order(ids.lapsed, lapsed["default_workspace_id"], lapsed["id"],
                      at - timedelta(days=60), kind="subscription", status="paid",
                      paid_at=at - timedelta(days=60),
                      product={"plan": {"id": "max"}, "cycle": "monthly"}))
        db.add(_order(ids.topup, idle["default_workspace_id"], idle["id"],
                      at - timedelta(days=3), kind="topup", status="pending"))
        db.add(BillingSubscription(
            order_id=ids.live, workspace_id=live_ws, plan_id="pro", cycle="monthly",
            plan={"id": "pro"}, starts_at=at - timedelta(days=1),
            ends_at=at + timedelta(days=29)))
        db.add(BillingSubscription(
            order_id=ids.queued, workspace_id=live_ws, plan_id="pro", cycle="monthly",
            plan={"id": "pro"}, starts_at=at + timedelta(days=29),
            ends_at=at + timedelta(days=59)))
        db.add(BillingSubscription(
            order_id=ids.lapsed, workspace_id=lapsed["default_workspace_id"],
            plan_id="max", cycle="monthly", plan={"id": "max"},
            starts_at=at - timedelta(days=60), ends_at=at - timedelta(days=30)))
        db.add(CreditBalance(workspace_id=live_ws, balance=Decimal("12.5"), updated_at=at))
        db.add(CreditLedger(
            id=f"led-{suffix}", workspace_id=live_ws, idempotency_key=f"led-{suffix}",
            kind="allowance", amount=Decimal("20"), balance_after=Decimal("12.5"),
            reference_id=ids.live, created_at=at - timedelta(days=1)))
        for index, (status, tokens, credits, age) in enumerate((
            ("charged", 100, Decimal("1.5"), 1), ("charged", 200, Decimal("2.5"), 2),
            ("shadow", 50, Decimal("0.5"), 3),
            # Older than the 30-day window: must not reach the summary.
            ("charged", 999, Decimal("99"), 45),
        )):
            db.add(UsageEvent(
                id=f"use-{suffix}-{index}", idempotency_key=f"use-{suffix}-{index}",
                workspace_id=live_ws, user_id=live["id"], session_id=f"ses-{suffix}",
                session_title="usage", model_id="gpt-5.6-luna", kind="message",
                tokens={"input": tokens}, total_tokens=tokens, credits=credits,
                status=status, pricing={}, created_at=at - timedelta(days=age)))
    async with _client({"user_id": admin["id"], "role": "admin"}) as client:
        yield SimpleNamespace(client=client, suffix=suffix, admin=admin, at=at,
                              orders=ids, active=live, expired=lapsed, free=idle)


async def test_non_admin_is_refused_on_every_endpoint():
    suffix = uuid.uuid4().hex[:10]
    user = await PgUserRepo().create(
        id=f"abill-plain-{suffix}", username=f"abill-plain-{suffix}", password_hash="unused")
    async with _client({"user_id": user["id"], "role": "user"}) as client:
        for path in PATHS:
            assert (await client.get(path)).status_code == 403


async def test_subscription_state_is_computed_per_workspace(estate):
    response = await estate.client.get(
        "/api/admin/billing/subscriptions", params={"q": estate.suffix})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    rows = {row["workspace"]["id"]: row for row in body["items"]}

    live = rows[estate.active["default_workspace_id"]]
    assert live["state"] == "active"
    assert (live["plan_id"], live["cycle"]) == ("pro", "monthly")
    assert live["queued_count"] == 1
    assert isinstance(live["balance"], str) and Decimal(live["balance"]) == Decimal("12.5")
    assert live["owner"]["email"] == f"active.{estate.suffix}@paid.test"
    assert live["workspace"]["kind"] == "personal"
    assert live["last_paid_at"] is not None

    lapsed = rows[estate.expired["default_workspace_id"]]
    assert lapsed["state"] == "expired"
    assert (lapsed["plan_id"], lapsed["cycle"], lapsed["ends_at"]) == ("free", None, None)

    idle = rows[estate.free["default_workspace_id"]]
    assert idle["state"] == "free"
    assert (idle["queued_count"], Decimal(idle["balance"])) == (0, Decimal(0))
    assert idle["last_paid_at"] is None


async def test_subscription_filters_by_state_plan_and_owner_email(estate):
    async def ids(**params):
        response = await estate.client.get(
            "/api/admin/billing/subscriptions", params={"q": estate.suffix, **params})
        assert response.status_code == 200, response.text
        return {row["workspace"]["id"] for row in response.json()["items"]}

    assert await ids(state="active") == {estate.active["default_workspace_id"]}
    assert await ids(state="expired") == {estate.expired["default_workspace_id"]}
    assert await ids(state="free") == {estate.free["default_workspace_id"]}
    assert await ids(plan="pro") == {estate.active["default_workspace_id"]}
    # "free" means no live term, so the lapsed workspace belongs there too.
    assert await ids(plan="free") == {
        estate.expired["default_workspace_id"], estate.free["default_workspace_id"]}

    by_email = await estate.client.get("/api/admin/billing/subscriptions", params={
        "q": f"expired.{estate.suffix}@paid.test"})
    assert [row["workspace"]["id"] for row in by_email.json()["items"]] == [
        estate.expired["default_workspace_id"]]
    assert (await estate.client.get(
        "/api/admin/billing/subscriptions", params={"plan": "platinum"})).status_code == 422


async def test_the_subscription_page_costs_the_same_whatever_its_size(estate):
    """The active term is fetched for the whole page, not once per row.

    Everything else on this page is already batched with an ``IN (...)``; the
    live term used to be asked for row by row, sequentially, so a full page
    added `limit` round-trips to a read-only operator view.
    """
    async def statements_for(limit: int) -> list[str]:
        seen: list[str] = []

        def before(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement)

        engine = get_engine().sync_engine
        sqlalchemy.event.listen(engine, "before_cursor_execute", before)
        try:
            response = await estate.client.get(
                "/api/admin/billing/subscriptions",
                params={"q": estate.suffix, "limit": limit},
            )
        finally:
            sqlalchemy.event.remove(engine, "before_cursor_execute", before)
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == limit
        return seen

    one_row = await statements_for(1)
    whole_page = await statements_for(3)
    assert len(whole_page) == len(one_row), (
        f"a 3-row page cost {len(whole_page)} statements against "
        f"{len(one_row)} for a 1-row page"
    )
    subscription_reads = [
        text for text in whole_page
        if "billing_subscriptions" in text and text.lstrip().upper().startswith("SELECT")
    ]
    # The live term, the queued count, and "has ever subscribed" — one each.
    assert len(subscription_reads) == 3, subscription_reads


async def test_page_size_is_capped_at_two_hundred(estate):
    assert (await estate.client.get(
        "/api/admin/billing/subscriptions", params={"limit": 200})).status_code == 200
    for path in ("/api/admin/billing/subscriptions", "/api/admin/billing/orders"):
        assert (await estate.client.get(path, params={"limit": 201})).status_code == 422


async def test_orders_paginate_and_never_expose_checkout_url(estate):
    page = await estate.client.get("/api/admin/billing/orders", params={"q": estate.suffix})
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["total"] == 4
    assert [row["id"] for row in body["items"]] == [
        estate.orders.queued, estate.orders.live, estate.orders.topup, estate.orders.lapsed]
    assert "checkout_url" not in page.text
    assert all("checkout_url" not in row for row in body["items"])

    live = next(row for row in body["items"] if row["id"] == estate.orders.live)
    assert live["amount_fen"] == 1000
    assert isinstance(live["credits"], str) and Decimal(live["credits"]) == Decimal("10")
    assert live["provider_order_id"] == f"prov-{estate.orders.live}"
    assert live["workspace_name"] == estate.active["username"]
    assert live["user"]["email"] == f"active.{estate.suffix}@paid.test"
    assert (live["kind"], live["plan_id"], live["cycle"]) == ("subscription", "pro", "monthly")

    first = await estate.client.get(
        "/api/admin/billing/orders", params={"q": estate.suffix, "limit": 2})
    second = await estate.client.get(
        "/api/admin/billing/orders", params={"q": estate.suffix, "limit": 2, "offset": 2})
    assert [row["id"] for row in first.json()["items"]] == [
        estate.orders.queued, estate.orders.live]
    assert [row["id"] for row in second.json()["items"]] == [
        estate.orders.topup, estate.orders.lapsed]
    assert (first.json()["total"], second.json()["total"]) == (4, 4)


async def test_orders_filter_by_status_kind_and_day_range(estate):
    async def ids(**params):
        response = await estate.client.get(
            "/api/admin/billing/orders", params={"q": estate.suffix, **params})
        assert response.status_code == 200, response.text
        return {row["id"] for row in response.json()["items"]}

    assert await ids(status="pending") == {estate.orders.topup}
    assert await ids(kind="topup") == {estate.orders.topup}
    assert await ids(provider="nope") == set()
    today = estate.at.date().isoformat()
    yesterday = (estate.at - timedelta(days=1)).date().isoformat()
    # "from" today keeps only the order placed just now; "to" yesterday drops it.
    assert await ids(**{"from": today}) == {estate.orders.queued}
    assert estate.orders.queued not in await ids(**{"to": yesterday})
    inverted = await estate.client.get(
        "/api/admin/billing/orders", params={"from": today, "to": yesterday})
    assert inverted.status_code == 422


async def test_workspace_detail_summarises_history_orders_and_usage(estate):
    workspace_id = estate.active["default_workspace_id"]
    response = await estate.client.get(f"/api/admin/billing/workspaces/{workspace_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workspace"]["id"] == workspace_id
    assert body["owner"]["email"] == f"active.{estate.suffix}@paid.test"
    assert body["member_count"] == 1
    assert Decimal(body["balance"]) == Decimal("12.5")
    assert body["plan_id"] == "pro"
    assert body["subscription"]["order_id"] == estate.orders.live
    assert [entry["order_id"] for entry in body["queued"]] == [estate.orders.queued]
    assert [entry["order_id"] for entry in body["history"]] == [
        estate.orders.queued, estate.orders.live]
    assert [row["id"] for row in body["orders"]] == [estate.orders.queued, estate.orders.live]
    assert [row["reference_id"] for row in body["ledger"]] == [estate.orders.live]
    assert "checkout_url" not in response.text

    usage = {row["status"]: row for row in body["usage"]["items"]}
    assert body["usage"]["days"] == 30
    # The 45-day-old event is outside the window, so charged stops at two.
    assert (usage["charged"]["events"], usage["charged"]["total_tokens"]) == (2, 300)
    assert Decimal(usage["charged"]["credits"]) == Decimal("4")
    assert Decimal(usage["shadow"]["credits"]) == Decimal("0.5")

    missing = await estate.client.get("/api/admin/billing/workspaces/no-such-workspace")
    assert missing.status_code == 404


async def test_every_read_records_an_audit_event(estate):
    workspace_id = estate.active["default_workspace_id"]
    await estate.client.get("/api/admin/billing/subscriptions", params={"q": estate.suffix})
    await estate.client.get("/api/admin/billing/orders", params={"q": estate.suffix})
    await estate.client.get(f"/api/admin/billing/workspaces/{workspace_id}")
    async with get_db_session() as db:
        rows = (await db.scalars(select(AuditLog).where(
            AuditLog.user_id == estate.admin["id"],
            AuditLog.action == "admin.view_billing",
        ))).all()
    assert {row.resource_type for row in rows} == {
        "billing_subscription", "payment_order", "workspace"}
    detail = next(row for row in rows if row.resource_type == "workspace")
    assert (detail.workspace_id, detail.resource_id) == (workspace_id, workspace_id)
