"""The agent driver is single-flight and stale generations are fenced out."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent import driver
from agent.driver import DriverBusyError, LeaseLostError
from db.base import Base, get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.project import Project
from db.models.session import Session
from db.models.user import User


async def _seed() -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"drv-user-{suffix}"
    project_id = f"drv-project-{suffix}"
    session_id = f"drv-session-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            User(
                id=user_id,
                username=f"driver-{suffix}",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Project(
                id=project_id,
                user_id=user_id,
                name="Driver test",
                slug=f"driver-{suffix}",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Session(
                id=session_id,
                user_id=user_id,
                project_id=project_id,
                status="idle",
                created_at=now,
                updated_at=now,
            )
        )
    return user_id, session_id


async def _seed_sessions(count: int) -> tuple[str, list[str]]:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"quota-user-{suffix}"
    project_id = f"quota-project-{suffix}"
    session_ids = [f"quota-session-{suffix}-{index}" for index in range(count)]
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(
            User(
                id=user_id,
                username=user_id,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Project(
                id=project_id,
                user_id=user_id,
                name="Driver quota",
                slug=f"driver-quota-{suffix}",
                created_at=now,
                updated_at=now,
            )
        )
        for session_id in session_ids:
            db.add(
                Session(
                    id=session_id,
                    user_id=user_id,
                    project_id=project_id,
                    status="idle",
                    created_at=now,
                    updated_at=now,
                )
            )
    return user_id, session_ids


@pytest.mark.asyncio
async def test_concurrent_reservations_have_one_winner():
    user_id, session_id = await _seed()

    async def attempt():
        try:
            return await driver.reserve_run(session_id, user_id)
        except DriverBusyError:
            return None

    results = await asyncio.gather(*(attempt() for _ in range(50)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    state = await driver.get_driver_state(session_id)
    assert state is not None
    assert state.run_id == winners[0].run_id
    assert state.generation == 1
    await winners[0].release(session_status="idle")


@pytest.mark.asyncio
async def test_reservation_publishes_busy_generation_before_return(monkeypatch):
    user_id, session_id = await _seed()
    published: list[dict] = []
    monkeypatch.setattr(
        driver.bus,
        "publish",
        lambda event, payload: published.append({"event": event, **payload}),
    )

    lease = await driver.reserve_run(session_id, user_id)

    assert published == [
        {
            "event": "session.status",
            "userId": user_id,
            "sessionId": session_id,
            "status": "busy",
            "generation": lease.generation,
        }
    ]
    await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_monitor_uses_initial_full_ttl_before_first_renewal(monkeypatch):
    user_id, session_id = await _seed()
    renewals: list[int] = []

    async def fake_renew(self):
        renewals.append(self.generation)
        return True

    monkeypatch.setattr(driver, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(driver, "ABORT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(driver.RunLease, "renew", fake_renew)
    lease = await driver.reserve_run(session_id, user_id)
    try:
        await asyncio.sleep(0)
        assert renewals == []
        await asyncio.sleep(0.07)
        assert renewals == [lease.generation]
        assert lease._lost is False
    finally:
        await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_concurrent_sibling_reservations_enforce_durable_user_quota(
    monkeypatch,
):
    user_id, session_ids = await _seed_sessions(3)
    import core.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: type("Quota", (), {"max_concurrent_agents": 1})(),
    )

    async def attempt(session_id):
        try:
            return await driver.reserve_run(session_id, user_id)
        except driver.DriverQuotaExceededError as exc:
            return exc

    results = await asyncio.gather(*(attempt(item) for item in session_ids))
    winners = [item for item in results if isinstance(item, driver.RunLease)]
    rejected = [
        item for item in results if isinstance(item, driver.DriverQuotaExceededError)
    ]
    assert len(winners) == 1
    assert len(rejected) == 2
    assert {(item.used, item.limit) for item in rejected} == {(1, 1)}

    winner = winners[0]
    assert await winner.release(session_status="idle") is True
    next_session = next(
        session_id for session_id in session_ids if session_id != winner.session_id
    )
    replacement = await driver.reserve_run(next_session, user_id)
    assert await replacement.release(session_status="idle") is True


@pytest.mark.asyncio
async def test_expired_recovery_respects_hard_quota_and_retries_after_release(
    monkeypatch,
):
    user_id, session_ids = await _seed_sessions(3)
    import core.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: type("Quota", (), {"max_concurrent_agents": 1})(),
    )
    expired = await driver.reserve_run(session_ids[0], user_id)
    await expired.stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(
                AgentDriverState.session_id == session_ids[0],
            )
            .values(
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
    record = next(
        item
        for item in await driver.recover_expired_driver_records()
        if item.session_id == session_ids[0]
    )

    ordinary = await driver.reserve_run(session_ids[1], user_id)
    try:
        with pytest.raises(driver.DriverQuotaExceededError) as caught:
            await driver.reserve_recovered_run(
                record,
                initial_phase="reserved",
            )
        assert (caught.value.used, caught.value.limit) == (1, 1)
        unchanged = await driver.get_driver_state(session_ids[0])
        assert unchanged is not None
        assert unchanged.run_id == record.run_id
        assert unchanged.generation == record.generation
        assert unchanged.phase == record.phase
        assert unchanged.trigger_message_id == record.trigger_message_id
    finally:
        assert await ordinary.release(session_status="idle") is True

    continuation = await driver.reserve_recovered_run(
        record,
        initial_phase="reserved",
    )
    try:
        assert continuation.generation == record.generation + 1
    finally:
        assert await continuation.release(session_status="idle") is True
        assert await expired.release(session_status="error") is False


@pytest.mark.asyncio
async def test_concurrent_recovery_and_sibling_reserve_have_one_quota_winner(
    monkeypatch,
):
    user_id, session_ids = await _seed_sessions(2)
    import core.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: type("Quota", (), {"max_concurrent_agents": 1})(),
    )
    expired = await driver.reserve_run(session_ids[0], user_id)
    await expired.stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(AgentDriverState.session_id == session_ids[0])
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    record = next(
        item
        for item in await driver.recover_expired_driver_records()
        if item.session_id == session_ids[0]
    )

    async def recover_attempt():
        try:
            return await driver.reserve_recovered_run(
                record,
                initial_phase="reserved",
            )
        except driver.DriverQuotaExceededError as exc:
            return exc

    async def sibling_attempt():
        try:
            return await driver.reserve_run(session_ids[1], user_id)
        except driver.DriverQuotaExceededError as exc:
            return exc

    results = await asyncio.gather(recover_attempt(), sibling_attempt())
    winners = [item for item in results if isinstance(item, driver.RunLease)]
    rejected = [
        item for item in results if isinstance(item, driver.DriverQuotaExceededError)
    ]
    assert len(winners) == 1
    assert len(rejected) == 1
    assert (rejected[0].used, rejected[0].limit) == (1, 1)
    winner = winners[0]
    assert await winner.release(session_status="idle") is True

    if winner.session_id == session_ids[0]:
        retry = await driver.reserve_run(session_ids[1], user_id)
    else:
        retry = await driver.reserve_recovered_run(
            record,
            initial_phase="reserved",
        )
    assert await retry.release(session_status="idle") is True
    assert await expired.release(session_status="error") is False


@pytest.mark.asyncio
async def test_concurrent_release_and_next_reserve_converge_without_deadlock():
    user_id, session_id = await _seed()
    first = await driver.reserve_run(session_id, user_id)

    async def reserve_after_release():
        while True:
            try:
                return await driver.reserve_run(session_id, user_id)
            except DriverBusyError:
                await asyncio.sleep(0)

    released, second = await asyncio.wait_for(
        asyncio.gather(
            first.release(session_status="idle"),
            reserve_after_release(),
        ),
        timeout=2,
    )
    assert released is True
    assert second.generation == first.generation + 1
    async with get_db_session() as db:
        status = (
            await db.execute(select(Session.status).where(Session.id == session_id))
        ).scalar_one()
    assert status == "busy"
    assert await second.release(session_status="idle") is True
    state = await driver.get_driver_state(session_id)
    assert state is not None
    assert state.phase == "idle"
    async with get_db_session() as db:
        status = (
            await db.execute(select(Session.status).where(Session.id == session_id))
        ).scalar_one()
    assert status == "idle"


@pytest.mark.asyncio
async def test_abort_request_is_durable_and_reaches_local_owner():
    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)
    try:
        assert await driver.request_abort(session_id, user_id) is True
        assert lease.abort.is_set()
        state = await driver.get_driver_state(session_id)
        assert state is not None
        assert state.abort_requested_at is not None
    finally:
        await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_stale_release_cannot_clear_takeover_generation():
    user_id, session_id = await _seed()
    first = await driver.reserve_run(session_id, user_id)
    # Quiesce renewal and expire the durable lease to model a dead worker.
    await first.stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(AgentDriverState.session_id == session_id)
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )

    with pytest.raises(driver.DriverRecoveryRequiredError) as blocked:
        await driver.reserve_run(session_id, user_id)
    second = await driver.reserve_recovered_run(
        blocked.value.record,
        initial_phase="reserved",
    )
    assert second.generation == first.generation + 1
    assert await first.release(session_status="error") is False

    state = await driver.get_driver_state(session_id)
    assert state is not None
    assert state.run_id == second.run_id
    assert state.phase == "reserved"
    async with get_db_session() as db:
        status = (
            await db.execute(select(Session.status).where(Session.id == session_id))
        ).scalar_one()
    assert status == "busy"
    await second.release(session_status="idle")


@pytest.mark.asyncio
async def test_release_commit_failure_keeps_exact_marker_available_for_fallback(
    monkeypatch,
):
    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)
    await lease.set_phase("finalizing")
    await lease.stop_monitor()
    original_get_db_session = driver.get_db_session

    @asynccontextmanager
    async def fail_before_commit():
        # Raising while the real context is still active makes it roll the
        # Driver+Session transition back together, exactly like a commit
        # failpoint. The RunLease must remain usable for the error fallback.
        async with original_get_db_session() as db:
            yield db
            raise RuntimeError("release commit failpoint")

    monkeypatch.setattr(driver, "get_db_session", fail_before_commit)
    with pytest.raises(RuntimeError, match="release commit failpoint"):
        await lease.release(session_status="idle")

    assert lease._closed is False
    assert lease._transport_revoked is True
    async with get_db_session() as db:
        state = (
            await db.execute(
                select(AgentDriverState).where(
                    AgentDriverState.session_id == session_id
                )
            )
        ).scalar_one()
        status = (
            await db.execute(select(Session.status).where(Session.id == session_id))
        ).scalar_one()
    assert state.run_id == lease.run_id
    assert state.generation == lease.generation
    assert state.phase == "finalizing"
    assert status == "busy"

    monkeypatch.setattr(driver, "get_db_session", original_get_db_session)
    assert await lease.preserve_for_recovery(session_status="error") is True
    state = await driver.get_driver_state(session_id)
    assert state is not None
    expiry = driver._aware(state.lease_expires_at)
    assert expiry is not None
    assert expiry <= datetime.now(timezone.utc)
    async with get_db_session() as db:
        status = (
            await db.execute(select(Session.status).where(Session.id == session_id))
        ).scalar_one()
    assert status == "error"


@pytest.mark.asyncio
async def test_expired_generation_cannot_renew_or_advance_phase():
    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)
    await lease.stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(AgentDriverState.session_id == session_id)
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )

    assert await lease.renew() is False
    with pytest.raises(LeaseLostError):
        await lease.set_phase("running")
    assert lease.abort.is_set()

    state = await driver.get_driver_state(session_id)
    assert state is not None
    assert state.phase == "reserved"
    assert driver._aware(state.lease_expires_at) < datetime.now(timezone.utc)
    await lease.release(session_status="error")


@pytest.mark.asyncio
async def test_recovery_uses_database_clock_not_a_skewed_worker_clock(monkeypatch):
    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)
    await lease.stop_monitor()
    monkeypatch.setattr(
        driver,
        "_utcnow",
        lambda: datetime.now(timezone.utc) + timedelta(days=30),
    )

    recovered = await driver.recover_expired_driver_records()

    assert all(record.session_id != session_id for record in recovered)
    state = await driver.get_driver_state(session_id)
    assert state is not None
    assert state.run_id == lease.run_id
    assert state.phase == "reserved"
    await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_expired_marker_survives_snapshot_and_old_owner_release():
    user_id, session_id = await _seed()
    lease = await driver.reserve_run(
        session_id,
        user_id,
        trigger_message_id="accepted-message",
    )
    await lease.stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(AgentDriverState.session_id == session_id)
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )

    records = await driver.recover_expired_driver_records()
    record = next(item for item in records if item.session_id == session_id)
    state = await driver.get_driver_state(session_id)
    assert state is not None
    assert state.run_id == lease.run_id == record.run_id
    assert state.generation == lease.generation == record.generation
    assert state.phase == record.phase == "reserved"
    assert state.trigger_message_id == record.trigger_message_id == "accepted-message"
    async with get_db_session() as db:
        status = (
            await db.execute(select(Session.status).where(Session.id == session_id))
        ).scalar_one()
    assert status == "error"

    # Expiry turns the identity into a recovery marker. The former owner can
    # no longer erase it from a late finally block.
    assert await lease.release(session_status="idle") is False
    state = await driver.get_driver_state(session_id)
    assert state is not None
    assert state.run_id == record.run_id
    assert state.phase == "reserved"

    repair_lease = await driver.reserve_recovered_run(
        record,
        initial_phase="finalizing",
    )
    assert repair_lease.generation == record.generation + 1
    state = await driver.get_driver_state(session_id)
    assert state is not None
    assert state.phase == "finalizing"
    assert state.trigger_message_id == "accepted-message"
    with pytest.raises(driver.StaleRecoveryError):
        await driver.reserve_recovered_run(record, initial_phase="reserved")
    assert await repair_lease.release(session_status="idle") is True


@pytest.mark.asyncio
async def test_expired_marker_must_be_recovered_before_a_new_prompt():
    user_id, session_id = await _seed()
    old = await driver.reserve_run(
        session_id,
        user_id,
        trigger_message_id="old-message",
    )
    await old.stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(AgentDriverState.session_id == session_id)
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    record = next(
        item
        for item in await driver.recover_expired_driver_records()
        if item.session_id == session_id
    )

    with pytest.raises(driver.DriverRecoveryRequiredError) as blocked:
        await driver.reserve_run(
            session_id,
            user_id,
            trigger_message_id="new-message",
        )
    assert blocked.value.record == record

    repair = await driver.reserve_recovered_run(
        record,
        initial_phase="finalizing",
    )
    assert await repair.release(session_status="error") is True
    newer = await driver.reserve_run(
        session_id,
        user_id,
        trigger_message_id="new-message",
    )
    with pytest.raises(driver.StaleRecoveryError):
        await driver.reserve_recovered_run(record, initial_phase="reserved")
    state = await driver.get_driver_state(session_id)
    assert state is not None
    assert state.run_id == newer.run_id
    assert state.generation == newer.generation
    assert state.trigger_message_id == "new-message"
    assert await old.release(session_status="error") is False
    assert await newer.release(session_status="idle") is True


@pytest.mark.asyncio
async def test_concurrent_recovery_snapshots_and_user_reserve_do_not_deadlock():
    user_id, session_id = await _seed()
    old = await driver.reserve_run(
        session_id,
        user_id,
        trigger_message_id="expired-message",
    )
    await old.stop_monitor()
    async with get_db_session() as db:
        await db.execute(
            update(AgentDriverState)
            .where(AgentDriverState.session_id == session_id)
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )

    sweep_tasks = [
        asyncio.create_task(driver.recover_expired_driver_records()) for _ in range(12)
    ]
    prompt_task = asyncio.create_task(
        driver.reserve_run(
            session_id,
            user_id,
            trigger_message_id="newer-message",
        )
    )
    results = await asyncio.wait_for(
        asyncio.gather(*sweep_tasks, prompt_task, return_exceptions=True),
        timeout=2,
    )
    blocked = results[-1]
    assert isinstance(blocked, driver.DriverRecoveryRequiredError)
    stale_records = [
        record
        for sweep in results[:-1]
        for record in sweep
        if record.session_id == session_id
    ]
    assert stale_records
    repair = await driver.reserve_recovered_run(
        blocked.record,
        initial_phase="finalizing",
    )
    for record in stale_records:
        with pytest.raises(driver.StaleRecoveryError):
            await driver.reserve_recovered_run(record, initial_phase="finalizing")

    state = await driver.get_driver_state(session_id)
    assert state is not None
    assert state.run_id == repair.run_id
    assert state.trigger_message_id == "expired-message"
    assert await old.release(session_status="error") is False
    assert await repair.release(session_status="error") is True


@pytest.mark.asyncio
async def test_wait_for_idle_tracks_the_active_generation():
    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)
    waiter = asyncio.create_task(driver.wait_for_idle(session_id, timeout=2))
    await asyncio.sleep(0)
    assert not waiter.done()
    await lease.release(session_status="idle")
    assert await waiter is True


@pytest.mark.asyncio
async def test_wait_for_idle_uses_database_clock_for_remote_owner(monkeypatch):
    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)
    await lease.stop_monitor()
    # Model a request handled by a different worker: there is no local
    # activity event, so the durable row and the database clock are authority.
    driver._activities.pop(session_id, None)
    monkeypatch.setattr(
        driver,
        "_utcnow",
        lambda: datetime.now(timezone.utc) + timedelta(days=30),
    )

    assert await driver.wait_for_idle(session_id, timeout=0.05) is False

    await lease.release(session_status="idle")
    assert await driver.wait_for_idle(session_id, timeout=0.2) is True


@pytest.mark.asyncio
async def test_trigger_message_binding_is_fenced():
    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)
    try:
        await lease.bind_trigger_message("message-accepted")
        state = await driver.get_driver_state(session_id)
        assert state is not None
        assert state.trigger_message_id == "message-accepted"
    finally:
        await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_user_message_and_trigger_binding_commit_atomically():
    from session.session import create_user_message

    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)
    try:
        message = await create_user_message(
            session_id,
            "accepted atomically",
            user_id=user_id,
            run_fence=(session_id, lease.run_id, lease.generation),
            bind_trigger=True,
        )
        state = await driver.get_driver_state(session_id)
        assert state is not None
        assert state.run_id == lease.run_id
        assert state.generation == lease.generation
        assert state.trigger_message_id == message.id
    finally:
        await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_trigger_bind_failure_rolls_back_the_user_message(monkeypatch):
    from db.models.message import Message
    from session.session import create_user_message

    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)

    async def fail_bind(*_args, **_kwargs):
        raise LeaseLostError("simulated bind failure")

    monkeypatch.setattr(driver, "bind_trigger_message_locked", fail_bind)
    try:
        with pytest.raises(LeaseLostError):
            await create_user_message(
                session_id,
                "must roll back",
                user_id=user_id,
                run_fence=(session_id, lease.run_id, lease.generation),
                bind_trigger=True,
            )
        async with get_db_session() as db:
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.session_id == session_id)
                )
            ).scalar_one()
        assert count == 0
    finally:
        await lease.release(session_status="idle")


@pytest.mark.asyncio
@pytest.mark.parametrize("settlement", ["release", "preserve"])
async def test_file_sqlite_session_lock_precedes_quota_transaction(
    tmp_path,
    monkeypatch,
    settlement,
):
    """Release/preserve cannot deadlock with a replacement reservation.

    The holder pauses after acquiring the process-local Session lock but before
    opening its database transaction. A replacement must not check out its own
    connection or execute BEGIN IMMEDIATE until that holder has committed.
    This is the exact two-connection inversion that quota admission used to
    create when it opened the SQLite writer transaction before Session lock.
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / f'driver-{settlement}.db'}",
        pool_size=4,
        connect_args={"timeout": 0.25},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    holder_entered = asyncio.Event()
    allow_holder_db = asyncio.Event()
    contender_entered_db = asyncio.Event()
    holder_task_name = f"sqlite-{settlement}-holder"
    contender_task_name = f"sqlite-{settlement}-contender"

    @asynccontextmanager
    async def isolated_db():
        task = asyncio.current_task()
        task_name = task.get_name() if task is not None else ""
        async with factory() as db:
            if task_name == holder_task_name:
                holder_entered.set()
                await asyncio.wait_for(allow_holder_db.wait(), timeout=2)
            elif task_name == contender_task_name:
                contender_entered_db.set()
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    # Keep two physical DBAPI connections checked out simultaneously. The test
    # must exercise a real multi-connection file database, not StaticPool.
    async with (
        engine.connect() as first_connection,
        engine.connect() as second_connection,
    ):
        first_dbapi = first_connection.sync_connection.connection.dbapi_connection
        second_dbapi = second_connection.sync_connection.connection.dbapi_connection
        assert first_dbapi is not second_dbapi

    monkeypatch.setattr(driver, "get_db_session", isolated_db)
    import core.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: type("Quota", (), {"max_concurrent_agents": 1})(),
    )
    suffix = uuid.uuid4().hex[:12]
    user_id = f"sqlite-order-user-{suffix}"
    project_id = f"sqlite-order-project-{suffix}"
    session_id = f"sqlite-order-session-{suffix}"
    sibling_id = f"sqlite-order-sibling-{suffix}"
    now = datetime.now(timezone.utc)
    async with isolated_db() as db:
        db.add(
            User(
                id=user_id,
                username=user_id,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Project(
                id=project_id,
                user_id=user_id,
                name="SQLite order",
                slug=project_id,
                created_at=now,
                updated_at=now,
            )
        )
        for item_id in (session_id, sibling_id):
            db.add(
                Session(
                    id=item_id,
                    user_id=user_id,
                    project_id=project_id,
                    status="idle",
                    created_at=now,
                    updated_at=now,
                )
            )

    first = await driver.reserve_run(session_id, user_id)
    await first.stop_monitor()
    old_record = driver.RecoveredDriver(
        session_id=session_id,
        user_id=user_id,
        run_id=first.run_id,
        generation=first.generation,
        phase="reserved",
        trigger_message_id=None,
    )
    if settlement == "release":
        holder = asyncio.create_task(
            first.release(session_status="idle"),
            name=holder_task_name,
        )
    else:
        holder = asyncio.create_task(
            first.preserve_for_recovery(session_status="error"),
            name=holder_task_name,
        )
    await asyncio.wait_for(holder_entered.wait(), timeout=1)

    if settlement == "release":
        contender = asyncio.create_task(
            driver.reserve_run(session_id, user_id),
            name=contender_task_name,
        )
    else:
        contender = asyncio.create_task(
            driver.reserve_recovered_run(
                old_record,
                initial_phase="reserved",
            ),
            name=contender_task_name,
        )
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                contender_entered_db.wait(),
                timeout=0.05,
            )
        assert contender.done() is False
    finally:
        allow_holder_db.set()

    settled, replacement = await asyncio.wait_for(
        asyncio.gather(holder, contender),
        timeout=2,
    )
    assert settled is True
    assert replacement.generation == first.generation + 1
    state = await driver.get_driver_state(session_id)
    assert state is not None
    assert state.run_id == replacement.run_id
    assert state.generation == replacement.generation
    assert await first.release(session_status="error") is False

    # The replacement consumes the sole user slot; a sibling reservation is
    # atomically refused, then succeeds immediately after exact release.
    with pytest.raises(driver.DriverQuotaExceededError) as quota:
        await driver.reserve_run(sibling_id, user_id)
    assert (quota.value.used, quota.value.limit) == (1, 1)
    assert await replacement.release(session_status="idle") is True
    sibling = await driver.reserve_run(sibling_id, user_id)
    assert await sibling.release(session_status="idle") is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_fenced_sqlite_write_blocks_takeover_until_commit(
    tmp_path,
    monkeypatch,
):
    """SQLite's no-op UPDATE is a real cross-connection transaction fence."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'driver-fence.db'}",
        connect_args={"timeout": 2},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def isolated_db():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(driver, "get_db_session", isolated_db)

    suffix = uuid.uuid4().hex[:12]
    user_id = f"sqlite-user-{suffix}"
    project_id = f"sqlite-project-{suffix}"
    session_id = f"sqlite-session-{suffix}"
    now = datetime.now(timezone.utc)
    async with isolated_db() as db:
        db.add(User(id=user_id, username=user_id, created_at=now, updated_at=now))
        db.add(
            Project(
                id=project_id,
                user_id=user_id,
                name="SQLite fence",
                slug=project_id,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            Session(
                id=session_id,
                user_id=user_id,
                project_id=project_id,
                status="idle",
                created_at=now,
                updated_at=now,
            )
        )

    first = await driver.reserve_run(session_id, user_id)
    await first.stop_monitor()
    async with isolated_db() as db:
        await db.execute(
            update(AgentDriverState)
            .where(AgentDriverState.session_id == session_id)
            .values(lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=1))
        )

    fence_entered = asyncio.Event()
    allow_commit = asyncio.Event()

    async def old_write():
        async with isolated_db() as db:
            await driver.assert_run_fence_locked(
                db,
                session_id=session_id,
                user_id=user_id,
                run_id=first.run_id,
                generation=first.generation,
            )
            fence_entered.set()
            await allow_commit.wait()
            await db.execute(
                update(Session)
                .where(Session.id == session_id)
                .values(title="old write committed first")
            )

    writer = asyncio.create_task(old_write())
    await asyncio.wait_for(fence_entered.wait(), timeout=1)
    # Wait on SQLite's own second-precision clock until the *committed* lease
    # is definitely eligible for takeover. A fixed sleep around the boundary
    # can still observe it live depending on fractional wall-clock alignment.
    for _ in range(80):
        async with isolated_db() as db:
            expired = (
                await db.execute(
                    select(AgentDriverState.session_id).where(
                        AgentDriverState.session_id == session_id,
                        AgentDriverState.lease_expires_at <= func.current_timestamp(),
                    )
                )
            ).scalar_one_or_none() is not None
        if expired:
            break
        await asyncio.sleep(0.05)
    assert expired
    record = driver.RecoveredDriver(
        session_id=session_id,
        user_id=user_id,
        run_id=first.run_id,
        generation=first.generation,
        phase="reserved",
        trigger_message_id=None,
    )
    takeover = asyncio.create_task(
        driver.reserve_recovered_run(
            record,
            initial_phase="reserved",
        )
    )
    await asyncio.sleep(0.05)
    assert not takeover.done(), "takeover crossed the uncommitted transcript fence"

    allow_commit.set()
    await asyncio.wait_for(writer, timeout=1)
    second = await asyncio.wait_for(takeover, timeout=1)
    assert second.generation == first.generation + 1
    assert await second.release(session_status="idle") is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_fence_uses_session_then_driver_lock_order():
    statements: list[str] = []

    class Dialect:
        name = "postgresql"

    class Bind:
        dialect = Dialect()

    class Result:
        def scalar_one_or_none(self):
            return "matched"

    class FakeDB:
        def get_bind(self):
            return Bind()

        async def execute(self, statement):
            statements.append(str(statement))
            return Result()

    await driver.assert_run_fence_locked(
        FakeDB(),
        session_id="session",
        user_id="user",
        run_id="run",
        generation=1,
    )

    assert "sessions" in statements[0]
    assert "agent_driver_states" in statements[1]


@pytest.mark.asyncio
async def test_postgres_quota_transaction_locks_global_then_user(monkeypatch):
    statements: list[tuple[str, dict | None]] = []

    class Dialect:
        name = "postgresql"

    class Bind:
        dialect = Dialect()

    class Result:
        def close(self):
            return None

    class FakeDB:
        def get_bind(self):
            return Bind()

        async def execute(self, statement, parameters=None):
            statements.append((str(statement), parameters))
            return Result()

    @asynccontextmanager
    async def fake_session():
        yield FakeDB()

    monkeypatch.setattr(driver, "get_db_session", fake_session)
    async with driver._agent_quota_transaction("quota-user"):
        pass

    assert len(statements) == 2
    assert all("pg_advisory_xact_lock" in sql for sql, _ in statements)
    assert statements[0][1] == {"key": driver._AGENT_QUOTA_GLOBAL_LOCK_KEY}
    assert statements[1][1] == {
        "key": driver._agent_quota_user_lock_key("quota-user"),
    }
