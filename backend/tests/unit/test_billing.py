"""Financial invariants, metering coverage and the real signed callback boundary."""
import json
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from api.billing import router, usage_date_range
from auth.middleware import get_current_user
from billing import providers as provider_module
from billing.payments import create_order, settle_payment
from billing.pricing import normalize_usage, quote
from billing.providers import Checkout, HttpPaymentProvider, PaidReceipt, signature
from billing.service import BillingError, UsageMeter
from core.identifier import ascending
from db.base import get_db_session
from db.models.billing import CreditBalance, CreditLedger, PaymentOrder, UsageEvent
from db.models.message import Message
from db.repository.user_repo import PgUserRepo
from session.session import create_session
from tool.tool import ToolContext


def test_official_prices_and_exact_single_token():
    assert quote("openai/gpt-5.6-luna", normalize_usage({"input": 1})).credits == Decimal("0.0000002")
    assert quote("proxy/qwen3.8-flash", normalize_usage({"input": 1_000_000, "output": 1_000_000})).credits == Decimal("3.5")
    assert quote("openai/gemini-3.8-flash", normalize_usage({"input": 1_000_000})).credits == Decimal("5.084025")
    assert quote("openai/claude-opus-5", normalize_usage({"input": 1_000_000})).credits == Decimal("33.8935")
    assert quote("openai/deepseek-chat", {"input": 1, "output": 0}).credits is None
    assert quote("openai/made-up-model", {"input": 1, "output": 0}).credits is None


def test_cache_normalization_inclusive_vs_anthropic_native():
    native = normalize_usage({"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 300,
        "cache_creation_input_tokens": 200, "cache_creation": {"ephemeral_1h_input_tokens": 80}})
    wrapped = normalize_usage(SimpleNamespace(prompt_tokens=600, completion_tokens=50,
        cache_read_input_tokens=300, cache_creation_input_tokens=200,
        cache_creation={"ephemeral_1h_input_tokens": 80}))
    assert native == wrapped
    assert native["input"] == 600 and native["total"] == 650
    assert normalize_usage(native) == native  # re-normalization must not add cache twice
    write_only = normalize_usage({"input": 200, "cache_read": 0, "cache_write": 200, "cache": 200})
    assert normalize_usage(write_only)["cache_read"] == 0
    responses = normalize_usage({"input_tokens": 1000, "output_tokens": 100,
        "input_tokens_details": {"cached_tokens": 900}, "output_tokens_details": {"reasoning_tokens": 80}})
    assert responses["total"] == 1100 and responses["cache_read"] == 900
    assert quote("gpt-5.6-luna", responses).credits == Decimal("0.000158")
    assert normalize_usage({"prompt_tokens": 1000, "completion_tokens": 1, "prompt_cache_hit_tokens": 400})["cache_read"] == 400
    with pytest.raises(ValueError):
        normalize_usage({"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 11}})


def test_cache_write_prices_and_long_context_boundary():
    usage = normalize_usage({"input": 1000, "cache_read": 300, "cache_write": 200, "cache_write_1h": 80, "output": 50})
    assert quote("claude-opus-5", usage).credits == Decimal("0.036943915")
    short = quote("gpt-5.6-sol", normalize_usage({"input": 272000, "output": 10}))
    long = quote("gpt-5.6-sol", normalize_usage({"input": 272001, "output": 10}))
    assert short.credits == Decimal("1.0882")
    assert long.credits == Decimal("2.176308")
    assert long.snapshot["long_context"] is True


@pytest.mark.parametrize("at,price", [("2026-09-04T01:00:00+00:00", "3"),
    ("2026-09-04T04:00:00+00:00", "1.5"), ("2026-09-05T02:00:00+00:00", "1.5"),
    ("2026-09-04T06:00:00+00:00", "3"), ("2026-09-04T10:00:00+00:00", "1.5")])
def test_deepseek_beijing_peak_windows(at, price):
    assert quote("deepseek-v4-flash", {"input": 1_000_000, "output": 0}, at=datetime.fromisoformat(at)).credits == Decimal(price)


@pytest.fixture
async def account(monkeypatch):
    # These tests isolate payment/metering arithmetic. Commercial allowances and
    # paid-plan eligibility run without mocks in test_subscription_plans.py.
    from unittest.mock import AsyncMock
    monkeypatch.setattr("billing.subscriptions.ensure_period_allowance", AsyncMock())
    monkeypatch.setattr("api.billing.ensure_period_allowance", AsyncMock())
    monkeypatch.setattr("billing.payments.require_topup_allowed", AsyncMock())
    monkeypatch.setenv("BILLING_MODE", "shadow")
    user = await PgUserRepo().create(id=ascending("test_user"), username="billing_" + uuid4().hex,
        email=uuid4().hex + "@example.test", password_hash="unused")
    session = await create_session(user_id=user["id"], title="Billing test", model="openai/gpt-5.6-luna")
    return user, session


@pytest.fixture
def payment_adapter(monkeypatch):
    config = {"base_url": "https://payments.example.test", "api_key": "test-only",
        "webhook_secret": "test-webhook-secret-32-characters-long", "display_name": "Test gateway",
        "checkout_hosts": ["checkout.example.test"]}
    adapter = HttpPaymentProvider(config)

    async def checkout(**kwargs):
        return Checkout("https://checkout.example.test/" + kwargs["order_id"], "external-" + kwargs["order_id"])

    monkeypatch.setattr(adapter, "create_checkout", checkout)
    monkeypatch.setattr(provider_module, "_registered", {"test": adapter})
    monkeypatch.setenv("PAYMENT_PROVIDERS_JSON", "{}")
    monkeypatch.setenv("PAYMENT_PUBLIC_BASE_URL", "https://app.example.test")
    return adapter


async def make_order(user, amount=1000):
    return await create_order(workspace_id=user["default_workspace_id"], user_id=user["id"],
        provider_name="test", amount_fen=amount, request_key=uuid4().hex)


async def fund(user):
    order = await make_order(user)
    receipt = PaidReceipt(order_id=order["id"], payment_id=uuid4().hex, amount_fen=1000, currency="CNY", status="paid")
    await settle_payment("test", receipt)
    return order, receipt


async def test_shadow_enforce_duplicate_settlement_and_snapshot(account, payment_adapter, monkeypatch):
    user, session = account
    shadow = await UsageMeter.start(model_id=session.model, session_id=session.id, user_id=user["id"])
    amount = await shadow.finish({"input": 1000, "output": 100})
    assert amount == Decimal("0.00032")
    async with get_db_session() as db:
        assert (await db.get(CreditBalance, user["default_workspace_id"])).balance == 0
    await fund(user)
    monkeypatch.setenv("BILLING_MODE", "enforce")
    meter = await UsageMeter.start(model_id=session.model, session_id=session.id, user_id=user["id"])
    # Reloading the tariff during a call must not change that call's price.
    monkeypatch.setenv("BILLING_RATES_FILE", "/nonexistent/changed-after-call.json")
    assert await meter.finish({"input": 1000, "output": 100}) == amount
    assert await replace(meter, finished=False).finish({"input": 999999}) == amount
    async with get_db_session() as db:
        assert (await db.get(CreditBalance, user["default_workspace_id"])).balance == Decimal("9.99968")
        assert await db.scalar(select(func.count()).select_from(CreditLedger).where(CreditLedger.reference_id == meter.event_id)) == 1
        event = await db.get(UsageEvent, meter.event_id)
        assert event.workspace_id == session.workspace_id
        assert event.status == "charged" and event.pricing["conversion"] == "gpt_numeric_1_to_1"


async def test_insufficient_balance_and_missing_provider_usage(account, monkeypatch):
    user, session = account
    monkeypatch.setenv("BILLING_MODE", "enforce")
    with pytest.raises(BillingError, match="积分不足"):
        await UsageMeter.start(model_id=session.model, session_id=session.id)
    with pytest.raises(BillingError, match="verified"):
        await UsageMeter.start(model_id="unknown", session_id=session.id)
    monkeypatch.setenv("BILLING_MODE", "shadow")
    meter = await UsageMeter.start(model_id=session.model, session_id=session.id)
    await meter.finish(None)
    async with get_db_session() as db:
        row = await db.get(UsageEvent, meter.event_id)
        assert row.status == "unreported" and row.credits is None


async def test_signed_webhook_idempotency_tampering_and_cross_order_reuse(account, payment_adapter):
    user, _ = account
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        order = await make_order(user)
        receipt = {"order_id": order["id"], "payment_id": uuid4().hex, "amount_fen": 1000, "currency": "CNY", "status": "paid"}

        async def send(payload, timestamp=None, tamper=False):
            body = json.dumps(payload).encode()
            stamp = timestamp or str(int(time.time()))
            sig = signature(payment_adapter.secret, stamp, body)
            return await client.post("/api/billing/webhooks/test", content=body + (b" " if tamper else b""),
                headers={"x-payment-timestamp": stamp, "x-payment-signature": sig})

        assert (await send(receipt, tamper=True)).status_code == 401
        assert (await send(receipt, timestamp="1")).status_code == 401
        assert (await send({**receipt, "amount_fen": 1})).status_code == 409
        assert (await send(receipt)).json() == {"accepted": True, "duplicate": False}
        assert (await send(receipt)).json() == {"accepted": True, "duplicate": True}
        assert (await send({**receipt, "payment_id": "different"})).status_code == 409
        another = await make_order(user)
        assert (await send({**receipt, "order_id": another["id"]})).status_code == 409
    async with get_db_session() as db:
        assert (await db.get(CreditBalance, user["default_workspace_id"])).balance == 10
        assert await db.scalar(select(func.count()).select_from(CreditLedger).where(CreditLedger.workspace_id == user["default_workspace_id"])) == 1


async def test_order_idempotency_and_workspace_api_isolation(account, payment_adapter):
    user, session = account
    key = uuid4().hex
    kwargs = dict(workspace_id=user["default_workspace_id"], user_id=user["id"],
                  provider_name="test", amount_fen=1000, request_key=key)
    first = await create_order(**kwargs)
    assert (await create_order(**kwargs))["id"] == first["id"]
    with pytest.raises(BillingError):
        await create_order(**{**kwargs, "amount_fen": 2000})
    for _ in range(3):
        meter = await UsageMeter.start(model_id=session.model, session_id=session.id)
        await meter.finish({"input": 1})
    stranger = await PgUserRepo().create(id=ascending("stranger"), username="stranger_" + uuid4().hex,
        email=uuid4().hex + "@example.test", password_hash="unused")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user["id"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        page1 = (await client.get("/api/billing/usage?page=1&page_size=2")).json()
        page2 = (await client.get("/api/billing/usage?page=2&page_size=2")).json()
        assert page1["total"] == 3 and page1["total_pages"] == 2
        assert len(page1["items"]) == 2 and len(page2["items"]) == 1
        assert {x["id"] for x in page1["items"]}.isdisjoint({x["id"] for x in page2["items"]})
        assert (await client.get("/api/billing/usage?page_size=101")).status_code == 422
        assert (await client.get("/api/billing/balance", headers={"X-Workspace-Id": stranger["default_workspace_id"]})).status_code == 403
        app.dependency_overrides[get_current_user] = lambda: {"user_id": stranger["id"]}
        assert (await client.get(f"/api/billing/orders/{first['id']}")).status_code == 404
        assert (await client.get("/api/billing/summary")).json()["total_tokens"] == 0


async def test_stream_cancellation_and_error_after_usage_are_metered(account, monkeypatch):
    from agent import llm
    user, session = account

    async def fake(*args, **kwargs):
        yield {"type": "usage", "usage": {"input": 10, "output": 2}}
        yield {"type": "text_delta", "text": "partial"}
        yield {"type": "error", "error": RuntimeError("interrupted")}

    monkeypatch.setattr(llm, "_stream_responses_api", fake)
    ctx = ToolContext(session_id=session.id, user_id=user["id"], message_id="message-observed")
    stream = llm.stream_llm(None, [], [], {}, session.model, ctx)
    assert (await anext(stream))["type"] == "text_delta"
    await stream.aclose()
    async with get_db_session() as db:
        event = (await db.scalars(select(UsageEvent).where(UsageEvent.message_id == ctx.message_id))).one()
        assert event.total_tokens == 12 and event.credits == Decimal("0.0000044")
        assert event.status == "shadow"


async def test_historical_backfill_is_not_recharged(account):
    from billing.backfill import backfill_history
    user, session = account
    msg_id = ascending("message")
    async with get_db_session() as db:
        db.add(Message(id=msg_id, session_id=session.id, user_id=user["id"], role="assistant",
                       model_id=session.model, tokens={"input": 100, "output": 20}, created_at=datetime.now(timezone.utc)))
    await backfill_history()
    await backfill_history()
    async with get_db_session() as db:
        records = (await db.scalars(select(UsageEvent).where(UsageEvent.message_id == msg_id))).all()
        assert len(records) == 1 and records[0].status == "historical"
        assert (await db.get(CreditBalance, user["default_workspace_id"])).balance == 0


async def test_http_checkout_contract_and_redirect_allowlist(monkeypatch):
    config = {"base_url": "https://gateway.example.test", "api_key": "test-key",
        "webhook_secret": "a-test-secret-with-at-least-32-characters", "display_name": "Gateway",
        "checkout_hosts": ["checkout.example.test"]}
    adapter = HttpPaymentProvider(config)
    bad_url = False

    def handler(request):
        assert request.url == "https://gateway.example.test/checkouts"
        payload = json.loads(request.content)
        assert payload == {"version": 1, "order_id": "order-one", "amount_fen": 1000,
            "currency": "CNY", "callback_url": "https://app.example.test/callback"}
        assert request.headers["Idempotency-Key"] == "order-one"
        assert request.headers["X-Payment-Signature"] == signature(config["webhook_secret"],
            request.headers["X-Payment-Timestamp"], request.content)
        return httpx.Response(200, json={"provider_order_id": "ext-one", "checkout_url":
            "https://attacker.example.test" if bad_url else "https://checkout.example.test/one"})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    checkout = await adapter.create_checkout(order_id="order-one", amount_fen=1000,
        callback_url="https://app.example.test/callback")
    assert checkout.url == "https://checkout.example.test/one"
    bad_url = True
    with pytest.raises(BillingError, match="unapproved"):
        await adapter.create_checkout(order_id="order-one", amount_fen=1000,
            callback_url="https://app.example.test/callback")


async def test_timed_out_checkout_can_be_listed_and_resumed_without_another_order(account, payment_adapter, monkeypatch):
    user, _ = account
    calls = []

    async def checkout(**kwargs):
        calls.append(kwargs["order_id"])
        if len(calls) == 1:
            raise httpx.ReadTimeout("provider reply lost")
        return Checkout("https://checkout.example.test/resumed", "upstream-one")

    monkeypatch.setattr(payment_adapter, "create_checkout", checkout)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user["id"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/billing/orders", json={
            "provider": "test", "amount_fen": 125, "request_key": uuid4().hex})
        assert response.status_code == 502
        page = (await client.get("/api/billing/orders?page=1&page_size=1")).json()
        assert page["total"] == 1 and page["total_pages"] == 1
        order = page["items"][0]
        assert Decimal(order["credits"]) == Decimal("1.25")
        assert order["status"] == "pending" and order["checkout_url"] is None
        resumed = await client.post(f"/api/billing/orders/{order['id']}/checkout")
        assert resumed.status_code == 200 and resumed.json()["checkout_url"].endswith("/resumed")
        assert calls == [order["id"], order["id"]]
        assert Decimal((await client.get("/api/billing/balance")).json()["balance"]) == 0
        receipt = PaidReceipt(order_id=order["id"], payment_id=uuid4().hex, amount_fen=125, currency="CNY", status="paid")
        await settle_payment("test", receipt)
        assert (await client.post(f"/api/billing/orders/{order['id']}/checkout")).json()["status"] == "paid"
        assert len(calls) == 2
        assert Decimal((await client.get("/api/billing/balance")).json()["balance"]) == Decimal("1.25")


async def test_order_history_and_resume_are_scoped_to_payer_and_workspace(account, payment_adapter):
    from db.models.workspace import WorkspaceMember
    user, _ = account
    order = await make_order(user)
    stranger = await PgUserRepo().create(id=ascending("u"), username=uuid4().hex, password_hash="unused")
    async with get_db_session() as db:
        db.add(WorkspaceMember(workspace_id=user["default_workspace_id"], user_id=stranger["id"],
            role="member", status="active", created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": stranger["id"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for headers in ({}, {"X-Workspace-Id": user["default_workspace_id"]}):
            assert (await client.get("/api/billing/orders", headers=headers)).json()["total"] == 0
            assert (await client.get(f"/api/billing/orders/{order['id']}", headers=headers)).status_code == 404
            assert (await client.get("/api/billing/orders", params={"order_id": order["id"], "status": "pending"}, headers=headers)).json()["total"] == 0
            assert (await client.post(f"/api/billing/orders/{order['id']}/checkout", headers=headers)).status_code == (403 if headers else 404)


@pytest.mark.parametrize("amount", [0, -100, True, 100.5, "100", 100_000_001])
async def test_order_api_rejects_invalid_amounts(account, payment_adapter, amount):
    user, _ = account
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user["id"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/billing/orders", json={"provider": "test", "amount_fen": amount, "request_key": uuid4().hex})
        assert response.status_code == 422
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(PaymentOrder).where(PaymentOrder.user_id == user["id"])) == 0


@pytest.mark.parametrize("headers", [
    {"x-payment-timestamp": "²", "x-payment-signature": "bad"},
    {"x-payment-timestamp": str(int(time.time())), "x-payment-signature": "非ASCII"},
])
async def test_invalid_signature_headers_are_rejected(payment_adapter, headers):
    with pytest.raises(BillingError) as failure:
        await payment_adapter.verify_webhook(b"{}", headers)
    assert failure.value.code == "PAYMENT_INVALID_SIGNATURE"


async def test_payment_callback_can_arrive_before_checkout_response(account, payment_adapter, monkeypatch):
    user, _ = account

    async def checkout(**kwargs):
        await settle_payment("test", PaidReceipt(order_id=kwargs["order_id"], payment_id=uuid4().hex,
            amount_fen=kwargs["amount_fen"], currency="CNY", status="paid"))
        return Checkout("https://checkout.example.test/already-paid", "upstream-paid")

    monkeypatch.setattr(payment_adapter, "create_checkout", checkout)
    order = await make_order(user)
    assert order["status"] == "paid" and order["checkout_url"] is None
    async with get_db_session() as db:
        assert (await db.get(CreditBalance, user["default_workspace_id"])).balance == 10
        assert await db.scalar(select(func.count()).select_from(CreditLedger).where(CreditLedger.reference_id == order["id"])) == 1


@pytest.mark.parametrize("provider_id", [None, 123, "", "x" * 161])
async def test_http_checkout_rejects_malformed_provider_ids(payment_adapter, monkeypatch, provider_id):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "provider_order_id": provider_id, "checkout_url": "https://checkout.example.test/one"}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs))
    with pytest.raises(BillingError) as failure:
        await HttpPaymentProvider.create_checkout(payment_adapter, order_id="order-one", amount_fen=1000,
            callback_url="https://app.example.test/callback")
    assert failure.value.code == "PAYMENT_INVALID_CHECKOUT"


async def test_usage_date_filter_includes_the_whole_local_day_and_updates_totals(account):
    user, session = account
    # Shanghai Sep 5 is Sep 4 16:00 UTC through (but excluding) Sep 5 16:00 UTC.
    moments = [
        ("2026-09-04T15:59:59.999999+00:00", 16),
        ("2026-09-04T16:00:00+00:00", 1),
        ("2026-09-05T03:00:00+00:00", 2),
        ("2026-09-05T15:59:59.999999+00:00", 4),
        ("2026-09-05T16:00:00+00:00", 8),
    ]
    for moment, tokens in moments:
        meter = await UsageMeter.start(model_id=session.model, session_id=session.id)
        await meter.finish({"input": tokens})
        async with get_db_session() as db:
            event = await db.get(UsageEvent, meter.event_id)
            event.created_at = datetime.fromisoformat(moment)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user["id"]}
    params = {"date_from": "2026-09-05", "date_to": "2026-09-05", "tz": "Asia/Shanghai"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = (await client.get("/api/billing/usage", params={**params, "page_size": 2})).json()
        second = (await client.get("/api/billing/usage", params={**params, "page_size": 2, "page": 2})).json()
        totals = (await client.get("/api/billing/summary", params=params)).json()
        assert first["total"] == 3 and first["total_pages"] == 2
        assert [entry["total_tokens"] for entry in first["items"] + second["items"]] == [4, 2, 1]
        assert totals["total_tokens"] == 7 and Decimal(totals["total_credits"]) == Decimal("0.0000014")
        assert (await client.get("/api/billing/summary")).json()["total_tokens"] == 31
        assert (await client.get("/api/billing/usage", params={"date_from": "2026-09-05", "tz": "Asia/Shanghai"})).json()["total"] == 4
        assert (await client.get("/api/billing/usage", params={"date_to": "2026-09-05", "tz": "Asia/Shanghai"})).json()["total"] == 4
        empty = {"date_from": "2026-09-07", "date_to": "2026-09-07"}
        assert (await client.get("/api/billing/usage", params=empty)).json()["items"] == []
        assert (await client.get("/api/billing/summary", params=empty)).json()["total_tokens"] == 0


@pytest.mark.parametrize("params", [
    {"date_from": "2026-09-06", "date_to": "2026-09-05"},
    {"date_from": "2026-02-30"},
    {"date_to": "not-a-date"},
    {"date_to": "9999-12-31"},
    {"tz": "not/a/timezone"},
])
async def test_usage_and_summary_reject_invalid_date_filters(account, params):
    user, _ = account
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {"user_id": user["id"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/api/billing/usage", "/api/billing/summary"):
            assert (await client.get(path, params=params)).status_code == 422


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)])
def test_date_filter_respects_daylight_saving_time(day, hours):
    bounds = usage_date_range(day, day, "America/New_York")
    assert (bounds.end - bounds.start).total_seconds() == hours * 3600
