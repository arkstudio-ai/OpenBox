"""Real auth, binding fences, concise templates and device receipts."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from auth.mobile import now
from db.base import get_db_session
from db.models.push import PushDelivery
from db.models.user import User
from notifications.runtime import PushWorker
from notifications.templates import TEMPLATES, render_template, test_template as render_test
from tests.integration.test_mobile_push_api import setup, login, bind, make_due_in_background  # noqa: F401


async def change_role(user, role):
    async with get_db_session() as db:
        row = await db.get(User, user)
        row.role = role


async def send(client, binding, **extra):
    return await client.post('/api/admin/push/test', json={
        'bindingId': binding['bindingId'], 'requestId': str(uuid4()), **extra})


@pytest.mark.parametrize('platform', ['ios', 'android'])
async def test_admin_web_and_mobile_share_only_the_current_phone(setup, platform):
    web, ios, android, credentials, user, fake = setup
    client = ios if platform == 'ios' else android
    await login(client, credentials)
    binding = await bind(client, platform)
    before = await web.get('/api/admin/push')
    assert before.status_code == 200, before.text
    data = before.json()
    assert data['device']['ready'] and data['device']['platform'] == platform
    assert len(data['templates']) == 10
    assert 'token' not in data['device'] and 'tokenHash' not in data['device']
    assert data['presence']['pushAllowed'] is False
    request_id = str(uuid4())
    response = await send(web, binding, template='task_completed', requestId=request_id)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body['title'] == '测试 · 任务完成' and body['status'] == 'pending'
    duplicate = await send(web, binding, template='task_completed', requestId=request_id)
    assert duplicate.status_code == 202 and duplicate.json()['id'] == body['id']
    assert (await send(web, binding)).status_code == 429
    assert (await send(web, binding, template='task_failed', requestId=request_id)).status_code == 409
    assert (await send(web, binding, userId='someone-else')).status_code == 422
    assert (await send(web, binding, template='anything')).status_code == 422
    receipt_path = f"/api/admin/push/messages/{body['id']}/receipt"
    assert (await client.post(receipt_path, json={'kind': 'received'})).status_code == 409
    await make_due_in_background(user)
    await PushWorker(fake).tick()
    assert len(fake.sent) == 1 and fake.sent[0][3]['recipientId'] == user
    assert fake.sent[0][3]['type'] == 'system_test'
    assert (await web.post(receipt_path, json={'kind': 'received'})).status_code == 401
    assert (await client.post(receipt_path, json={'kind': 'received'})).status_code == 200
    assert (await client.post(receipt_path, json={'kind': 'opened'})).status_code == 200
    await client.post(receipt_path, json={'kind': 'received'})
    record = (await web.get('/api/admin/push')).json()['tests'][0]
    assert record['status'] == 'accepted' and record['receipt'] == 'opened'
    assert record['receiptAt'] and record['attempts'] == 1


async def test_ordinary_and_demoted_admin_are_denied_including_legacy_route(setup):
    web, ios, _, credentials, user, fake = setup
    await login(ios, credentials)
    binding = await bind(ios, 'ios')
    queued = await send(web, binding)
    assert queued.status_code == 202
    await change_role(user, 'user')
    for client in [web, ios]:
        assert (await client.get('/api/admin/push')).status_code == 403
        assert (await send(client, binding)).status_code == 403
    assert (await ios.post('/api/push/test')).status_code == 403
    await make_due_in_background(user)
    await PushWorker(fake).tick()
    assert fake.sent == []
    await login(ios, credentials)
    assert (await ios.get('/api/admin/push')).status_code == 403
    assert (await ios.post('/api/push/test')).status_code == 403
    assert (await ios.get('/api/push/status')).status_code == 200
    assert (await bind(ios, 'ios'))['deliveryEnabled']


async def test_phone_change_rejects_stale_target_and_receipts(setup):
    web, ios, android, credentials, user, fake = setup
    await login(ios, credentials)
    old = await bind(ios, 'ios')
    queued = await send(web, old)
    await make_due_in_background(user)
    await PushWorker(fake).tick()
    await login(android, credentials)
    current = await bind(android, 'android')
    assert (await send(web, old)).status_code == 409
    path = f"/api/admin/push/messages/{queued.json()['id']}/receipt"
    assert (await android.post(path, json={'kind': 'opened'})).status_code == 409
    assert (await ios.post(path, json={'kind': 'opened'})).status_code == 401
    assert (await web.get('/api/admin/push')).json()['device']['bindingId'] == current['bindingId']


async def test_test_uses_normal_foreground_suppression(setup):
    web, ios, _, credentials, user, fake = setup
    await login(ios, credentials)
    binding = await bind(ios, 'ios')
    assert (await ios.put('/api/push/presence', json={'state': 'resumed', 'sequence': 1})).status_code == 200
    queued = await send(web, binding)
    async with get_db_session() as db:
        delivery = await db.scalar(select(PushDelivery).where(PushDelivery.message_id == queued.json()['id']))
        delivery.available_at = now() - timedelta(seconds=1)
    await PushWorker(fake).tick()
    record = (await web.get('/api/admin/push')).json()['tests'][0]
    assert record['status'] == 'cancelled' and record['error'] == 'app_foreground'
    assert fake.sent == []


@pytest.mark.parametrize('locale', ['zh-CN', 'en-US'])
def test_all_copy_is_short_bounded_and_has_distinct_business_meaning(locale):
    for kind in TEMPLATES:
        title, body = render_template(kind, '超长任务名称\n\t\x00\u202e' * 100, locale)
        assert len(title) <= 24 and len(body) <= (36 if locale == 'zh-CN' else 80)
        assert '\n' not in body and '\x00' not in body and '\u202e' not in body
        if kind != 'system_test': assert '…' in body
        test_title, test_body = render_test(kind, locale)
        assert len(test_title) <= 32 and len(test_body) <= 80
    assert render_template('input_required', '任务', locale) != render_template('approval_required', '任务', locale)
    assert render_template('platform_auth_expired', 'douyin', 'zh-CN')[1].startswith('抖音')
