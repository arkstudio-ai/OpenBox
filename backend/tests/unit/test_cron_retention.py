"""Transcript retention: per-job keep window, 30-day cap, quota exclusion."""
import uuid
from datetime import datetime, timedelta, timezone

from core.config import get_config
from cron.reaper import RETENTION_DAYS, _sweep_old_runs, _sweep_temp_sessions

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


async def _insert_session(session_id: str, user_id: str, kind: str = "cron"):
    from db.base import get_db_session
    from db.models.session import Session as SessionORM

    now = NOW()
    async with get_db_session() as db:
        db.add(SessionORM(
            id=session_id,
            user_id=user_id,
            project_id="default",
            title="[定时] t",
            kind=kind,
            status="idle",
            token_usage={},
            created_at=now,
            updated_at=now,
        ))


async def _insert_run(job_id: str, user_id: str, started_at, temp_session_id: str | None):
    from db.base import get_db_session
    from db.models.cron import CronRun

    run_id = "cron_run_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(CronRun(
            id=run_id,
            job_id=job_id,
            user_id=user_id,
            session_id="sess_main",
            temp_session_id=temp_session_id,
            status="ok",
            started_at=started_at,
        ))
    return run_id


async def _project_row(project_id: str = "default"):
    """tests share the sqlite db; make sure the default project exists once."""
    from db.base import get_db_session
    from db.models.project import Project
    from sqlalchemy import select

    async with get_db_session() as db:
        exists = (await db.execute(select(Project.id).where(Project.id == project_id))).scalar_one_or_none()
        if not exists:
            now = NOW()
            db.add(Project(id=project_id, user_id="u", name="d", slug="default", created_at=now, updated_at=now))


async def test_keep_window_trims_oldest_transcripts(monkeypatch):
    await _project_row()
    config = get_config()
    original = config.cron_transcript_keep_per_job
    config.cron_transcript_keep_per_job = 3
    try:
        user = "u_" + uuid.uuid4().hex[:8]
        job_id = "cron_" + uuid.uuid4().hex[:10]
        run_sessions = []
        for i in range(5):
            sid = f"sess_ret_{uuid.uuid4().hex[:8]}"
            await _insert_session(sid, user)
            await _insert_run(job_id, user, NOW() - timedelta(hours=5 - i), sid)
            run_sessions.append(sid)

        await _sweep_temp_sessions()

        from db.base import get_db_session
        from db.models.cron import CronRun
        from db.models.session import Session as SessionORM
        from sqlalchemy import select

        async with get_db_session() as db:
            kept = (await db.execute(
                select(CronRun.temp_session_id).where(
                    CronRun.job_id == job_id, CronRun.temp_session_id.isnot(None))
            )).scalars().all()
            # newest 3 keep their transcripts; oldest 2 are trimmed
            assert set(kept) == set(run_sessions[2:])

            trimmed = (await db.execute(
                select(SessionORM.is_deleted).where(SessionORM.id.in_(run_sessions[:2]))
            )).scalars().all()
            assert all(trimmed)
    finally:
        config.cron_transcript_keep_per_job = original


async def test_thirty_day_cap_deletes_runs_and_transcripts():
    await _project_row()
    user = "u_" + uuid.uuid4().hex[:8]
    job_id = "cron_" + uuid.uuid4().hex[:10]
    sid = f"sess_old_{uuid.uuid4().hex[:8]}"
    await _insert_session(sid, user)
    run_id = await _insert_run(job_id, user, NOW() - timedelta(days=RETENTION_DAYS + 1), sid)

    await _sweep_old_runs()

    from db.base import get_db_session
    from db.models.cron import CronRun
    from db.models.session import Session as SessionORM
    from sqlalchemy import select

    async with get_db_session() as db:
        assert (await db.execute(select(CronRun.id).where(CronRun.id == run_id))).scalar_one_or_none() is None
        deleted = (await db.execute(select(SessionORM.is_deleted).where(SessionORM.id == sid))).scalar_one()
        assert deleted is True


async def test_pending_outbox_protects_run_and_transcript_from_retention():
    from cron.outbox import stable_delivery_id
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox, CronRun
    from db.models.session import Session as SessionORM
    from sqlalchemy import select

    await _project_row()
    user = "u_" + uuid.uuid4().hex[:8]
    job_id = "cron_" + uuid.uuid4().hex[:10]
    sid = f"sess_pending_{uuid.uuid4().hex[:8]}"
    await _insert_session(sid, user)
    started = NOW() - timedelta(days=RETENTION_DAYS + 1)
    run_id = await _insert_run(job_id, user, started, sid)
    delivery_id = stable_delivery_id(run_id, "runlog")
    async with get_db_session() as db:
        db.add(CronDeliveryOutbox(
            id=delivery_id,
            run_id=run_id,
            job_id=job_id,
            user_id=user,
            session_id="sess_main",
            kind="runlog",
            payload={},
            state="pending",
            attempts=0,
            available_at=NOW(),
            created_at=NOW(),
            updated_at=NOW(),
        ))

    await _sweep_old_runs()

    async with get_db_session() as db:
        assert (await db.execute(
            select(CronRun.id).where(CronRun.id == run_id)
        )).scalar_one() == run_id
        assert (await db.execute(
            select(SessionORM.is_deleted).where(SessionORM.id == sid)
        )).scalar_one() is False


async def test_dead_letter_keeps_audit_until_normal_retention_then_reaps():
    from cron.outbox import stable_delivery_id
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox, CronRun
    from db.models.session import Session as SessionORM
    from sqlalchemy import select

    await _project_row()
    user = "u_" + uuid.uuid4().hex[:8]
    job_id = "cron_" + uuid.uuid4().hex[:10]
    sid = f"sess_dead_{uuid.uuid4().hex[:8]}"
    await _insert_session(sid, user)
    started = NOW() - timedelta(days=RETENTION_DAYS + 1)
    run_id = await _insert_run(job_id, user, started, sid)
    delivery_id = stable_delivery_id(run_id, "runlog")
    async with get_db_session() as db:
        db.add(CronDeliveryOutbox(
            id=delivery_id,
            run_id=run_id,
            job_id=job_id,
            user_id=user,
            session_id="sess_main",
            kind="runlog",
            payload={},
            state="dead_letter",
            attempts=12,
            available_at=started,
            last_error="permanent failure",
            created_at=started,
            updated_at=started,
        ))

    await _sweep_old_runs()

    async with get_db_session() as db:
        assert await db.scalar(
            select(CronRun.id).where(CronRun.id == run_id)
        ) is None
        assert await db.scalar(
            select(CronDeliveryOutbox.id).where(
                CronDeliveryOutbox.id == delivery_id
            )
        ) is None
        assert await db.scalar(
            select(SessionORM.is_deleted).where(SessionORM.id == sid)
        ) is True


async def test_old_run_is_retained_when_transcript_deletion_raises(monkeypatch):
    from db.base import get_db_session
    from db.models.cron import CronRun
    from db.models.session import Session as SessionORM
    from sqlalchemy import select
    import session.session as session_mod

    await _project_row()
    user = "u_" + uuid.uuid4().hex[:8]
    job_id = "cron_" + uuid.uuid4().hex[:10]
    sid = f"sess_delete_fail_{uuid.uuid4().hex[:8]}"
    await _insert_session(sid, user)
    run_id = await _insert_run(
        job_id,
        user,
        NOW() - timedelta(days=RETENTION_DAYS + 1),
        sid,
    )

    async def fail_delete(*_args, **_kwargs):
        raise RuntimeError("transient session delete failure")

    monkeypatch.setattr(session_mod, "delete_session", fail_delete)
    await _sweep_old_runs()

    async with get_db_session() as db:
        run = await db.scalar(select(CronRun).where(CronRun.id == run_id))
        session_deleted = await db.scalar(
            select(SessionORM.is_deleted).where(SessionORM.id == sid)
        )
    assert run is not None and run.temp_session_id == sid
    assert session_deleted is False


async def test_keep_window_does_not_unlink_failed_transcript_delete(monkeypatch):
    from db.base import get_db_session
    from db.models.cron import CronRun
    from sqlalchemy import select
    import session.session as session_mod

    await _project_row()
    config = get_config()
    original = config.cron_transcript_keep_per_job
    config.cron_transcript_keep_per_job = 0
    try:
        user = "u_" + uuid.uuid4().hex[:8]
        job_id = "cron_" + uuid.uuid4().hex[:10]
        sid = f"sess_trim_fail_{uuid.uuid4().hex[:8]}"
        await _insert_session(sid, user)
        run_id = await _insert_run(job_id, user, NOW(), sid)

        async def fail_delete(*_args, **_kwargs):
            raise RuntimeError("transient session delete failure")

        monkeypatch.setattr(session_mod, "delete_session", fail_delete)
        await _sweep_temp_sessions()

        async with get_db_session() as db:
            run = await db.scalar(select(CronRun).where(CronRun.id == run_id))
        assert run is not None and run.temp_session_id == sid
    finally:
        config.cron_transcript_keep_per_job = original


async def test_cron_sessions_do_not_count_against_quota():
    from db.repository.session_repo import PgSessionRepo

    user = "u_" + uuid.uuid4().hex[:8]
    await _insert_session(f"sess_n_{uuid.uuid4().hex[:6]}", user, kind="normal")
    await _insert_session(f"sess_c_{uuid.uuid4().hex[:6]}", user, kind="cron")
    await _insert_session(f"sess_c_{uuid.uuid4().hex[:6]}", user, kind="cron")

    assert await PgSessionRepo().count_by_user(user) == 1


async def test_create_session_kind_flows_through(monkeypatch):
    import session.session as sess

    captured = {}

    async def fake_resolve(project_id, user_id):
        return "default"

    import project.workspace as ws
    monkeypatch.setattr(ws, "resolve_for_session", fake_resolve)

    created = await sess.create_session(user_id="u_kind", kind="cron", parent_id=None)
    captured["id"] = created.id
    assert created.kind == "cron"

    fetched = await sess.get_session(created.id, user_id="u_kind")
    assert fetched is not None and fetched.kind == "cron"


async def test_cron_sessions_appear_in_the_sidebar_with_kind(monkeypatch):
    """Cron run sessions list alongside chats (clock-badged in the UI) whether
    or not they carry a notify-session parent; task-subagent children do not."""
    import session.session as sess

    user = "u_" + uuid.uuid4().hex[:8]

    async def fake_resolve(project_id, user_id):
        return "default"

    import project.workspace as ws
    monkeypatch.setattr(ws, "resolve_for_session", fake_resolve)

    normal = await sess.create_session(user_id=user, kind="normal")
    orphan_cron = await sess.create_session(user_id=user, kind="cron", parent_id=None)
    child_cron = await sess.create_session(user_id=user, kind="cron", parent_id=normal.id)
    subagent = await sess.create_session(user_id=user, kind="normal", parent_id=normal.id)

    listed = await sess.list_sessions(user_id=user)
    by_id = {s.id: s for s in listed}
    assert normal.id in by_id
    assert orphan_cron.id in by_id and by_id[orphan_cron.id].kind == "cron"
    assert child_cron.id in by_id and by_id[child_cron.id].kind == "cron"
    assert subagent.id not in by_id
