"""Run against a migrated, disposable PostgreSQL via PUSH_TEST_DATABASE_URL.

Independent Python processes deliberately bypass asyncio's in-process lock.
"""
import asyncio
import json
import os
import sys
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from auth.mobile import mobile_transaction, now, validate_claims
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
