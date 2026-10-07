"""Actual PostgreSQL lock/idempotency checks in disposable local schemas."""
import asyncio
from decimal import Decimal
from uuid import uuid4

from fastapi import Request
from sqlalchemy import func, select

from billing.admin import CreditGrant, SubscriptionGrant, manage_billing
from billing.subscriptions import utc
from db.base import get_db_session
from db.models.audit_log import AuditLog
from db.models.billing import BillingSubscription, CreditBalance, CreditLedger
from db.repository.user_repo import PgUserRepo
from tests.integration.test_billing_postgres import postgres  # noqa: F401


async def accounts():
    repo = PgUserRepo()
    admin = await repo.create(id=uuid4().hex, username=uuid4().hex, password_hash='unused', role='admin')
    owner = await repo.create(id=uuid4().hex, username=uuid4().hex, password_hash='unused')
    return admin['id'], owner['default_workspace_id']


def request():
    return Request({'type':'http', 'headers':[], 'client':('127.0.0.1',1)})


async def test_parallel_credit_retries_and_distinct_grants(postgres):
    admin, wid = await accounts()
    same = CreditGrant(request_key=uuid4().hex, reason='retry proof', credits='0.123456')
    operations = [manage_billing(wid, admin, 'grant_credits', same, request()) for _ in range(12)]
    operations += [manage_billing(wid, admin, 'grant_credits',
        CreditGrant(request_key=uuid4().hex, reason='independent grant', credits='1.000001'), request()) for _ in range(6)]
    result = await asyncio.gather(*operations)
    assert sum(not row['replayed'] for row in result) == 7
    async with get_db_session() as db:
        assert (await db.get(CreditBalance, wid)).balance == Decimal('6.123462')
        assert await db.scalar(select(func.count()).select_from(CreditLedger).where(CreditLedger.workspace_id == wid)) == 7
        assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.workspace_id == wid)) == 7


async def test_parallel_subscription_grants_are_contiguous_and_retry_safe(postgres, monkeypatch):
    monkeypatch.setattr('sandbox.desktop_activation.subscription_sandbox_enabled', lambda: False)
    admin, wid = await accounts()
    request_key = uuid4().hex
    same = SubscriptionGrant(request_key=request_key, reason='renewal', plan_id='pro', cycle='monthly')
    operations = [manage_billing(wid, admin, 'grant_subscription', same, request()) for _ in range(8)]
    operations += [manage_billing(wid, admin, 'grant_subscription', SubscriptionGrant(
        request_key=uuid4().hex, reason='another month', plan_id='pro', cycle='monthly'), request()) for _ in range(3)]
    result = await asyncio.gather(*operations)
    assert sum(not row['replayed'] for row in result) == 4
    async with get_db_session() as db:
        terms = list(await db.scalars(select(BillingSubscription).where(BillingSubscription.workspace_id == wid).order_by(BillingSubscription.starts_at)))
        assert len(terms) == 4
        assert all(utc(left.ends_at) == utc(right.starts_at) for left, right in zip(terms, terms[1:]))
        assert (await db.get(CreditBalance, wid)).balance == Decimal('280')
