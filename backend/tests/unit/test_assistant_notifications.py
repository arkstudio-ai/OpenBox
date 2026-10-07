"""Result/notification atomicity, durable identity and current read/send authority."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from agent import inbox as inputs
from assistant.notifications import read_target
from assistant.policy import AssistantError
from auth.mobile import begin_login, now
from db.base import close_engine, get_db_session, init_engine
from db.models.assistant import AssistantTask, TaskResult
from db.models.notification import Notification
from db.models.part import Part
from db.models.push import MobilePresence, PushDelivery, PushMessage
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from notifications import events, inbox
from notifications.runtime import claim_delivery, still_sendable
from notifications.schema import DeviceRegistration
from notifications.store import can_receive, register_device
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_results import result_ready


async def records(owner):
    async with get_db_session() as db:
        return (list((await db.scalars(select(Notification).where(Notification.user_id == owner))).all()),
            list((await db.scalars(select(PushMessage).where(PushMessage.user_id == owner))).all()))


@pytest.mark.parametrize(('finish', 'kind'), [('stop', 'assistant_result_ready'),
    ('error', 'assistant_result_failed'), ('aborted', 'assistant_result_stopped')])
async def test_terminal_notification_describes_execution_without_claiming_report_or_read(finish, kind):
    owner, workspace, main, _, lease, _ = await result_ready(finish=finish)
    await lease.release(session_status='idle')
    notes, pushes = await records(owner)
    assert len(notes) == len(pushes) == 1 and notes[0].kind == kind
    target = await read_target(user_id=owner, workspace_id=workspace, main_id=main.id, result_id=notes[0].link['resultId'])
    assert target['result']['delivery_state'] == 'pending'
    assert target['result']['processed_sequence'] is None
    assert target['result']['processed_message_id'] is None


async def test_notification_rolls_back_with_result_and_replay_never_duplicates(monkeypatch):
    from assistant import notifications
    owner, workspace, main, accepted, lease, message = await result_ready(settle=False)
    original = notifications.result_finished
    async def crash(*args):
        await original(*args)
        raise RuntimeError('crash before settlement commit')
    monkeypatch.setattr(notifications, 'result_finished', crash)
    try:
        with pytest.raises(RuntimeError, match='crash'):
            await inputs.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome='succeeded')
        assert await records(owner) == ([], [])
        async with get_db_session() as db:
            assert not await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted['task_id']))
        monkeypatch.setattr(notifications, 'result_finished', original)
        await asyncio.gather(*(inputs.settle_claimed_inbox_items(lease,
            result_message_id=message.id, outcome='succeeded') for _ in range(2)))
        notes, pushes = await records(owner)
        assert len(notes) == len(pushes) == 1
        note, push = notes[0], pushes[0]
        assert note.link['taskId'] == accepted['task_id']
        assert note.link['sessionId'] == main.id and note.link['kind'] == 'assistant_task'
        assert push.event_key == note.source_key and push.payload['notificationId'] == note.id
        assert push.payload['sessionId'] == main.id and push.payload['type'] == 'assistant_result_ready'
        assert 'Browser verification' not in str(push.payload) and 'Create a report' not in str(push.payload)
        async with get_db_session() as db:
            url = db.get_bind().url
            result = await db.get(TaskResult, note.link['resultId'])
            result.report_attempt = 4
            result.delivery_state = 'retry_wait'
        await close_engine()
        init_engine(url.render_as_string(hide_password=False))
        async with get_db_session() as db:
            task = await db.get(AssistantTask, accepted['task_id'])
            result = await db.get(TaskResult, note.link['resultId'])
            await original(db, task, result)
        assert len((await records(owner))[0]) == 1
        target = await read_target(user_id=owner, workspace_id=workspace, main_id=main.id, result_id=result.id)
        assert target['task']['task']['id'] == accepted['task_id']
        assert target['result']['result_id'] == result.id and target['result']['report_attempt'] == 4
    finally:
        await lease.release(session_status='idle')


async def test_source_edit_keeps_the_notification_and_its_target():
    # Revocation is not retroactive (D1): editing an original part is not a
    # permission change, so the notification and its target stay available.
    owner, workspace, main, _, lease, message = await result_ready()
    await lease.release(session_status='idle')
    note, push = (x[0] for x in await records(owner))
    async with get_db_session() as db:
        part = await db.scalar(select(Part).where(Part.message_id == message.id))
        part.data = {**part.data, 'text': 'altered original'}
    async with get_db_session() as db:
        assert await events.guard_valid(db, push)
        assert [row.id for row in (await inbox.list_inbox(db, owner, workspace))[0]] == [note.id]
        assert (await inbox.unread_counts(db, owner, workspace))['total'] == 1
    target = await read_target(user_id=owner, workspace_id=workspace, main_id=main.id,
                               result_id=note.link['resultId'])
    assert target['result']['result_id'] == note.link['resultId']


@pytest.mark.parametrize('change', ['member', 'execution', 'project', 'main'])
async def test_revocation_hides_list_counts_mark_read_legacy_api_and_target(change):
    from api import notifications as legacy
    from db.models.project import Project
    owner, workspace, main, accepted, lease, message = await result_ready()
    await lease.release(session_status='idle')
    note, push = (x[0] for x in await records(owner))
    async with get_db_session() as db:
        assert await events.guard_valid(db, push)
        assert (await inbox.unread_counts(db, owner, workspace))['total'] == 1
        if change == 'member':
            (await db.get(WorkspaceMember, (workspace, owner))).status = 'removed'
        elif change == 'project':
            (await db.get(Project, main.project_id)).is_deleted = True
        else:
            (await db.get(Session, main.id if change == 'main' else lease.session_id)).is_deleted = True
    async with get_db_session() as db:
        assert not await events.guard_valid(db, push)
        assert await inbox.list_inbox(db, owner, workspace) == ([], None)
        assert (await inbox.unread_counts(db, owner, workspace))['total'] == 0
        assert await inbox.mark_read(db, owner, workspace, note.id) is None
        assert await inbox.mark_all_read(db, owner, workspace) == 0
    actor = {'user_id': owner, 'workspace_id': workspace}
    assert await legacy.list_notifications(unread=False, limit=30, current_user=actor) == {'items': [], 'unread': 0}
    with pytest.raises(AssistantError):
        await read_target(**{'user_id':owner, 'workspace_id':workspace, 'main_id':main.id}, result_id=note.link['resultId'])


async def test_main_and_linked_legacy_completion_do_not_emit_duplicate_task_finished():
    owner, workspace, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status='idle')
    async with get_db_session() as db:
        for sid in (main.id, accepted['execution_session_id']):
            await events.task_finished(db, await db.get(Session, sid),
                SimpleNamespace(user_id=owner, run_id='another-report-attempt', generation=9))
        other = await db.scalar(select(WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == workspace, WorkspaceMember.user_id != owner))
        assert not await can_receive(db, other, workspace, main.id)
        assert await inbox.list_inbox(db, other, workspace) == ([], None)
    assert len((await records(owner))[0]) == 1


async def test_listing_can_be_limited_to_kinds_with_their_own_unread_count():
    from api import notifications as legacy
    owner, workspace, main, _, lease, _ = await result_ready()
    await lease.release(session_status='idle')
    async with get_db_session() as db:
        for index in range(3):
            await inbox.add_inbox(db, user_id=owner, workspace_id=workspace, kind='system_test',
                title=f'other {index}')
        await inbox.add_inbox(db, user_id=owner, workspace_id=workspace, kind='platform_auth_expired',
            title='sign in again', link=inbox.link_for('platform_auth_expired', workspace_id=workspace))
    actor = {'user_id': owner, 'workspace_id': workspace}
    everything = await legacy.list_notifications(unread=True, limit=20, current_user=actor)
    assert everything['unread'] == 5 and len(everything['items']) == 5
    # The auth center asks only for what concerns sign-ins; its count follows.
    auth = await legacy.list_notifications(unread=True, limit=20, current_user=actor,
        kind=['platform_auth_expired', 'desktop_login_expired'])
    assert [item['title'] for item in auth['items']] == ['sign in again'] and auth['unread'] == 1
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        await legacy.list_notifications(unread=True, limit=20, current_user=actor, kind=['x' * 65])


async def test_filtered_pagination_crosses_revoked_batches_without_counting_them():
    owner, workspace, main, _, lease, _ = await result_ready()
    await lease.release(session_status='idle')
    async with get_db_session() as db:
        # A stale private session link may have survived in legacy notifications.
        for index in range(105):
            await inbox.add_inbox(db, user_id=owner, workspace_id=workspace, kind='task_completed',
                title='stale private title', link={'kind':'session', 'workspaceId':workspace, 'sessionId':'missing'})
        for index in range(3):
            row = await inbox.add_inbox(db, user_id=owner, workspace_id=workspace,
                kind='system_test', title=f'valid {index}')
            row.created_at = now() - timedelta(minutes=1, seconds=index)
    seen, cursor = [], None
    while True:
        async with get_db_session() as db:
            page, cursor = await inbox.list_inbox(db, owner, workspace, limit=2, cursor=cursor)
        seen.extend(row.id for row in page)
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 4
    async with get_db_session() as db:
        assert (await inbox.unread_counts(db, owner, workspace))['total'] == 4


async def test_presend_revalidation_ignores_source_edits_but_cancels_after_main_is_deleted():
    owner, workspace, main, _, lease, message = await result_ready(settle=False)
    sid = await begin_login(owner, uuid4().hex)
    await register_device(owner, sid, DeviceRegistration(platform='ios', provider='apns', token=uuid4().hex))
    try:
        await inputs.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome='succeeded')
    finally:
        await lease.release(session_status='idle')
    async with get_db_session() as db:
        presence = await db.get(MobilePresence, owner)
        presence.state, presence.sequence = 'paused', 1
        presence.reported_at = now()-timedelta(seconds=10)
        row = await db.scalar(select(PushDelivery).where(PushDelivery.user_id == owner))
        row.available_at = now()-timedelta(seconds=1)
    claim = await claim_delivery({'apns'})
    assert claim is not None and 'guard' not in claim.payload
    async with get_db_session() as db:
        part = await db.scalar(select(Part).where(Part.message_id == message.id))
        part.data = {**part.data, 'text':'edited original'}
    assert await still_sendable(claim)  # D1: an edited source is not a revocation.
    async with get_db_session() as db:
        (await db.get(Session, main.id)).is_deleted = True
    assert not await still_sendable(claim)
    async with get_db_session() as db:
        row = await db.get(PushDelivery, claim.id)
        assert row.status == 'cancelled' and row.error == 'state_changed_before_send'
