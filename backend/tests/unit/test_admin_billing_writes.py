"""Admin writes are exact, retry-safe, owner-scoped and atomic with the audit log."""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session as SyncSession

from api.admin_billing import router
from auth.middleware import get_current_user
from billing.service import now
from billing.subscriptions import active_subscription, utc
from db.base import get_db_session
from db.models.audit_log import AuditLog
from db.models.billing import BillingSubscription, CreditBalance, CreditLedger, PaymentOrder
from db.models.desktop_activation import DesktopActivation
from db.models.user import User
from db.models.workspace import Workspace
from db.repository.user_repo import PgUserRepo


@pytest.fixture
async def estate(monkeypatch):
    monkeypatch.setattr('sandbox.desktop_activation.subscription_sandbox_enabled', lambda: False)
    repo = PgUserRepo()
    admin = await repo.create(id=uuid4().hex, username=uuid4().hex, password_hash='unused', role='admin')
    owner = await repo.create(id=uuid4().hex, username=uuid4().hex, password_hash='unused')
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: {'user_id': admin['id'], 'role': 'admin'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client, owner['default_workspace_id'], admin, owner, app


def body(**kwargs):
    return {'request_key': uuid4().hex, 'reason': 'Customer support adjustment', **kwargs}


async def grant(estate, **kwargs):
    client, wid, *_ = estate
    response = await client.post(f'/api/admin/billing/workspaces/{wid}/subscriptions',
                                 json=body(plan_id='pro', cycle='monthly', **kwargs))
    assert response.status_code == 200, response.text
    return response.json()


async def test_credit_retry_posts_exactly_one_ledger_and_audit(estate):
    client, wid, admin, *_ = estate
    payload = body(credits='12.345678')
    path = f'/api/admin/billing/workspaces/{wid}/credits'
    first = await client.post(path, json=payload)
    second = await client.post(path, json=payload)
    assert first.status_code == second.status_code == 200
    assert not first.json()['replayed'] and second.json()['replayed']
    assert first.json()['operation_id'] == second.json()['operation_id']
    async with get_db_session() as db:
        assert (await db.get(CreditBalance, wid)).balance == Decimal('12.345678')
        ledger = (await db.scalars(select(CreditLedger).where(CreditLedger.workspace_id == wid))).one()
        assert ledger.kind == 'admin_grant' and ledger.amount == Decimal('12.345678')
        audit = await db.get(AuditLog, ledger.reference_id)
        assert audit.user_id == admin['id'] and audit.details['request']['reason'] == payload['reason']
        assert Decimal(audit.details['before']['balance']) == 0
        assert await db.scalar(select(func.count()).select_from(PaymentOrder).where(PaymentOrder.workspace_id == wid)) == 0
    assert (await client.post(path, json={**payload, 'credits': '50'})).status_code == 409
    detail = (await client.get(f'/api/admin/billing/workspaces/{wid}')).json()
    assert detail['can_manage'] is True
    assert len(detail['operations']) == 1
    operation = detail['operations'][0]
    assert operation['actor']['id'] == admin['id'] and operation['reason'] == payload['reason']
    assert operation['action'] == 'grant_credits' and Decimal(operation['balance']) == Decimal('12.345678')


@pytest.mark.parametrize('credits', ['0', '-1', '1000001', 'NaN', 'Infinity', '0.0000001', True, 0.1])
async def test_invalid_credit_amounts_are_rejected(estate, credits):
    client, wid, *_ = estate
    result = await client.post(f'/api/admin/billing/workspaces/{wid}/credits', json=body(credits=credits))
    assert result.status_code == 422
    async with get_db_session() as db:
        assert await db.get(CreditBalance, wid) is None


@pytest.mark.parametrize('change', [{'reason': '  '}, {'request_key': ''}, {'unrecognized': 'field'}])
async def test_invalid_operation_envelope_is_rejected(estate, change):
    client, wid, *_ = estate
    assert (await client.post(f'/api/admin/billing/workspaces/{wid}/credits', json={**body(credits='1'), **change})).status_code == 422


@pytest.mark.parametrize('role,active,deleted', [('user', True, False), ('admin', False, False), ('admin', True, True)])
async def test_stale_admin_jwt_cannot_write(estate, role, active, deleted):
    client, wid, admin, *_ = estate
    async with get_db_session() as db:
        actor = await db.get(User, admin['id'])
        actor.role, actor.is_active, actor.is_deleted = role, active, deleted
    assert (await client.post(f'/api/admin/billing/workspaces/{wid}/credits', json=body(credits='1'))).status_code == 403


async def test_normal_user_cannot_use_any_write_route(estate):
    client, wid, _, owner, app = estate
    app.dependency_overrides[get_current_user] = lambda: {'user_id': owner['id'], 'role': 'user'}
    root = f'/api/admin/billing/workspaces/{wid}'
    for method, path, data in [
        ('POST', '/credits', body(credits='1')),
        ('POST', '/subscriptions', body(plan_id='pro', cycle='monthly')),
        ('PATCH', '/subscriptions/any', body(plan_id='pro', ends_at=now().isoformat(), expected_revision='a'*64)),
        ('POST', '/subscriptions/any/cancel', body(expected_revision='a'*64)),
    ]:
        assert (await client.request(method, root+path, json=data)).status_code == 403


async def test_deleted_workspace_cannot_receive_credits(estate):
    client, wid, *_ = estate
    async with get_db_session() as db:
        (await db.get(Workspace, wid)).is_deleted = True
    response = await client.post(f'/api/admin/billing/workspaces/{wid}/credits', json=body(credits='1'))
    assert response.status_code == 409


async def test_grants_queue_without_payment_orders_or_duplicate_allowances(estate):
    client, wid, *_ = estate
    first = await grant(estate)
    payload = body(plan_id='max', cycle='yearly')
    path = f'/api/admin/billing/workspaces/{wid}/subscriptions'
    second = (await client.post(path, json=payload)).json()
    retried = (await client.post(path, json=payload)).json()
    assert second['subscription']['starts_at'] == first['subscription']['ends_at']
    assert first['subscription']['order_id'] is None and second['subscription']['source'] == 'admin'
    assert retried['replayed']
    assert Decimal(first['balance']) == Decimal(second['balance']) == 280
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(PaymentOrder).where(PaymentOrder.workspace_id == wid)) == 0
        assert await db.scalar(select(func.count()).select_from(BillingSubscription).where(BillingSubscription.workspace_id == wid)) == 2
        assert (await active_subscription(db, wid, now())).id == first['subscription']['id']


async def test_edit_checks_revision_overlap_and_workspace(estate):
    client, wid, *_ = estate
    first = (await grant(estate))['subscription']
    second = (await grant(estate))['subscription']
    root = f'/api/admin/billing/workspaces/{wid}/subscriptions/{first["id"]}'
    overlap = body(plan_id='max', ends_at=second['ends_at'], expected_revision=first['revision'])
    assert (await client.patch(root, json=overlap)).status_code == 409
    valid = body(plan_id='max', ends_at=first['ends_at'], expected_revision=first['revision'])
    changed = await client.patch(root, json=valid)
    assert changed.status_code == 200, changed.text
    assert changed.json()['subscription']['plan_id'] == 'max'
    assert (await client.patch(root, json={**valid, 'request_key': uuid4().hex})).status_code == 409
    # The same accepted operation remains retryable even though the old revision is stale.
    assert (await client.patch(root, json=valid)).json()['replayed']
    other = await PgUserRepo().create(id=uuid4().hex, username=uuid4().hex, password_hash='unused')
    wrong = root.replace(wid, other['default_workspace_id'])
    assert (await client.patch(wrong, json=valid)).status_code == 404


async def test_cancel_preserves_terms_credits_and_other_renewals(estate):
    client, wid, *_ = estate
    first = (await grant(estate))['subscription']
    second = (await grant(estate))['subscription']
    payload = body(expected_revision=first['revision'])
    path = f'/api/admin/billing/workspaces/{wid}/subscriptions/{first["id"]}/cancel'
    result = await client.post(path, json=payload)
    assert result.status_code == 200 and result.json()['subscription']['cancelled_at']
    assert (await client.post(path, json=payload)).json()['replayed']
    async with get_db_session() as db:
        assert await active_subscription(db, wid, now()) is None
        assert (await db.get(CreditBalance, wid)).balance == 280
        assert utc((await db.get(BillingSubscription, first['id'])).ends_at).isoformat() == first['ends_at']
        assert (await db.get(BillingSubscription, second['id'])).cancelled_at is None
    detail = (await client.get(f'/api/admin/billing/workspaces/{wid}')).json()
    assert detail['plan_id'] == 'free' and len(detail['queued']) == 1 and len(detail['history']) == 2


async def test_audit_failure_rolls_back_the_credit(estate):
    client, wid, *_ = estate
    def reject_audit(session, *_):
        if any(isinstance(row, AuditLog) and row.action == 'admin.grant_credits' for row in session.new):
            raise RuntimeError('audit store unavailable')
    event.listen(SyncSession, 'before_flush', reject_audit)
    try:
        with pytest.raises(RuntimeError, match='audit store unavailable'):
            await client.post(f'/api/admin/billing/workspaces/{wid}/credits', json=body(credits='100'))
    finally:
        event.remove(SyncSession, 'before_flush', reject_audit)
    async with get_db_session() as db:
        account = await db.get(CreditBalance, wid)
        assert account is None or account.balance == 0
        assert not list(await db.scalars(select(CreditLedger).where(CreditLedger.workspace_id == wid)))


async def test_admin_grant_queues_desktop_entitlement_without_network(estate, monkeypatch):
    monkeypatch.setattr('sandbox.desktop_activation.subscription_sandbox_enabled', lambda: True)
    _, wid, _, owner, _ = estate
    first = (await grant(estate))['subscription']
    await grant(estate)
    async with get_db_session() as db:
        activation = await db.get(DesktopActivation, wid)
        assert activation.user_id == owner['id'] and activation.request_id == first['id']
        assert utc(activation.next_run_at) < now() + timedelta(minutes=1)
