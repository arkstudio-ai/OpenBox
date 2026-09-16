"""Run against a migrated, disposable PostgreSQL via PUSH_TEST_DATABASE_URL.

Independent Python processes deliberately bypass asyncio's in-process lock.
"""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select

from auth.mobile import begin_login, mobile_transaction, now, validate_claims
from db.base import close_engine, get_db_session, init_engine
from db.models.push import MobilePresence, MobileSession, PushDelivery, PushDevice, PushMessage
from db.models.user import User
from notifications.store import enqueue_notification

CHILD = '''
import asyncio, json, sys
from fastapi import HTTPException
from db.base import init_engine, close_engine
from auth.mobile import begin_login
from notifications.schema import DeviceRegistration
from notifications.store import register_device
from notifications.runtime import claim_delivery
from notifications.presence import report
from notifications.schema import PresenceReport
async def run():
    init_engine(sys.argv[1])
    print("ready", flush=True)
    await asyncio.to_thread(sys.stdin.readline)
    if sys.argv[2] == "presence":
        payload = json.loads(sys.argv[3])
        result = await report(payload["user"], payload["sid"], PresenceReport(
            state=payload["state"], sequence=payload["sequence"]))
        print(json.dumps(result), flush=True)
    elif sys.argv[2] == "claim":
        claim = await claim_delivery({"jpush"})
        print(json.dumps({"claim": claim.id if claim else None}), flush=True)
    else:
        sid = await begin_login(sys.argv[2], "installation-" + sys.argv[3])
        try:
            await register_device(sys.argv[2], sid, DeviceRegistration(platform="android", provider="jpush", token="registration" + sys.argv[3]))
            bound = True
        except HTTPException as error:
            assert error.status_code == 401
            bound = False
        print(json.dumps({"sid": sid, "bound": bound}), flush=True)
    await close_engine()
asyncio.run(run())
'''


async def processes(database, user, count, values=None):
    values = values or [uuid4().hex for _ in range(count)]
    children = [await asyncio.create_subprocess_exec(sys.executable, "-c", CHILD, database, user, value,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE) for value in values]
    try:
        for child in children:
            assert await asyncio.wait_for(child.stdout.readline(), 30) == b"ready\n"
        for child in children:
            child.stdin.write(b"go\n")
            await child.stdin.drain()
        output = await asyncio.wait_for(asyncio.gather(*(child.communicate() for child in children)), 45)
        for child, (_, error) in zip(children, output):
            assert child.returncode == 0, error.decode()
        return [json.loads(stdout) for stdout, _ in output]
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()


@pytest.mark.skipif(not os.getenv("PUSH_TEST_DATABASE_URL"), reason="Requires an explicitly supplied disposable PostgreSQL")
async def test_cross_process_login_binding_and_delivery_exclusivity():
    database = os.environ["PUSH_TEST_DATABASE_URL"]
    assert database.startswith("postgresql+asyncpg://")
    await close_engine()
    init_engine(database)
    user = "push-concurrency-" + uuid4().hex
    try:
        async with get_db_session() as db:
            db.add(User(id=user, username=user, created_at=now(), updated_at=now()))
        outcomes = await processes(database, user, 5)
        valid = []
        for outcome in outcomes:
            try:
                await validate_claims({"sub": user, "client": "mobile", "sid": outcome["sid"]})
                valid.append(outcome)
            except HTTPException as error:
                assert error.status_code == 401
        assert len(valid) == 1 and valid[0]["bound"]
        async with mobile_transaction() as db:
            device = await db.get(PushDevice, user)
            assert device.enabled and device.mobile_session_id == valid[0]["sid"]
            presence = await db.get(MobilePresence, user)
            presence.state, presence.reported_at = "paused", now() - timedelta(seconds=10)
            await enqueue_notification(db, user_id=user, event_key="concurrent-claim", kind="system_test", title="Test", body="Test", delay_seconds=0)
        claims = await processes(database, "claim", 3)
        assert sum(result["claim"] is not None for result in claims) == 1
        reports = [json.dumps({"user": user, "sid": valid[0]["sid"], "state": state, "sequence": seq})
                   for state, seq in (("paused", 2), ("resumed", 20), ("hidden", 3), ("inactive", 10))]
        await processes(database, "presence", len(reports), reports)
        async with get_db_session() as db:
            presence = await db.get(MobilePresence, user)
            assert presence.sequence == 20 and presence.state == "resumed"
            from sqlalchemy import select
            delivery = await db.scalar(select(PushDelivery).where(PushDelivery.user_id == user))
            assert delivery.status == "cancelled" and delivery.error == "app_foreground"
    finally:
        async with get_db_session() as db:
            for model in (PushDelivery, PushMessage, PushDevice, MobilePresence, MobileSession):
                await db.execute(delete(model).where(model.user_id == user))
            await db.execute(delete(User).where(User.id == user))
        await close_engine()


@asynccontextmanager
async def notification_users():
    """Own only these fresh identities in the explicitly supplied test database."""
    from db.models.notification import Notification
    await close_engine()
    init_engine(os.environ["PUSH_TEST_DATABASE_URL"])
    users = ["push-lock-" + uuid4().hex for _ in range(2)]
    try:
        async with get_db_session() as db:
            db.add_all(User(id=user, username=user, created_at=now(), updated_at=now()) for user in users)
        yield users
    finally:
        async with get_db_session() as db:
            for model in (PushDelivery, PushMessage, PushDevice, MobilePresence, MobileSession, Notification):
                await db.execute(delete(model).where(model.user_id.in_(users)))
            await db.execute(delete(User).where(User.id.in_(users)))
        await close_engine()


async def emit_note(user, key):
    from notifications.events import emit
    async with get_db_session() as db:
        note = await emit(db, user_id=user, workspace_id=None, kind="task_completed", event_key=key)
        return note.id


@pytest.mark.skipif(not os.getenv("PUSH_TEST_DATABASE_URL"), reason="Requires an explicitly supplied disposable PostgreSQL")
async def test_notifications_progress_across_users_and_serialize_with_duplicate_and_login():
    from db.models.notification import Notification
    from notifications.events import emit
    from notifications.schema import DeviceRegistration
    from notifications.store import register_device
    async with notification_users() as (first, second):
        old_sid = await begin_login(first, "installation-" + uuid4().hex)
        await register_device(first, old_sid, DeviceRegistration(
            platform="android", provider="jpush", token="registration" + uuid4().hex))
        pending = []
        try:
            async with get_db_session() as holder:
                note = await emit(holder, user_id=first, workspace_id=None,
                                  kind="task_completed", event_key="same-result")
                # A different user's real inbox + outbox commit must finish
                # while this transaction deliberately keeps its locks.
                await asyncio.wait_for(emit_note(second, "other-result"), 2)
                duplicate = asyncio.create_task(emit_note(first, "same-result"))
                pending.append(duplicate)
                await asyncio.sleep(.1)
                assert not duplicate.done()
                login = asyncio.create_task(begin_login(first, "installation-" + uuid4().hex))
                pending.append(login)
                await asyncio.sleep(.1)
                assert not login.done()
            duplicate_id, new_sid = await asyncio.wait_for(asyncio.gather(*pending), 3)
            assert duplicate_id == note.id and new_sid != old_sid
            async with get_db_session() as db:
                assert await db.scalar(select(func.count()).select_from(PushMessage).where(PushMessage.user_id == first)) == 1
                assert await db.scalar(select(func.count()).select_from(Notification).where(Notification.user_id == first)) == 1
                deliveries = list((await db.scalars(select(PushDelivery).where(PushDelivery.user_id == first))).all())
                assert len(deliveries) == 1
                assert deliveries[0].status == "cancelled" and deliveries[0].mobile_session_id == old_sid
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.skipif(not os.getenv("PUSH_TEST_DATABASE_URL"), reason="Requires an explicitly supplied disposable PostgreSQL")
async def test_notifications_wait_for_binding_mutations_and_cancel_the_committed_result():
    from notifications.events import cancel_event
    from notifications.schema import DeviceRegistration
    from notifications.store import register_device
    async with notification_users() as (user, _):
        sid = await begin_login(user, "installation-" + uuid4().hex)
        await register_device(user, sid, DeviceRegistration(
            platform="android", provider="jpush", token="registration" + uuid4().hex))
        writer = None
        try:
            async with mobile_transaction() as db:
                # The exclusive login/binding protocol also blocks the new
                # shared notification guard until this mutation commits.
                writer = asyncio.create_task(emit_note(user, "resolved-result"))
                await asyncio.sleep(.1)
                assert not writer.done()
            message_id = await asyncio.wait_for(writer, 2)
            async with get_db_session() as db:
                await cancel_event(db, user, "resolved-result")
            async with get_db_session() as db:
                delivery = await db.scalar(select(PushDelivery).where(PushDelivery.message_id == message_id))
                assert (delivery.status, delivery.error) == ("cancelled", "action_resolved")
        finally:
            if writer and not writer.done():
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
