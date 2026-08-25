"""CronService facade: validated writes, stagger on creation, status liveness."""
import uuid

import pytest

import cron.service as service_mod
from cron.service import CronService
from cron.types import CronJobCreate, CronScheduleCron, CronScheduleEvery


class FakeSession:
    def __init__(self, sid):
        self.id = sid
        self.model = "m"


@pytest.fixture
def owned_session(monkeypatch):
    import session.session as sess
    import project.workspace as ws

    async def fake_get_session(sid, user_id=None, **kw):
        return FakeSession(sid)

    async def fake_get_project(pid, user_id):
        return object()

    monkeypatch.setattr(sess, "get_session", fake_get_session)
    monkeypatch.setattr(ws, "get_project", fake_get_project)


@pytest.fixture
def svc(monkeypatch):
    s = CronService()
    # arm_timer touches the event loop scheduling machinery; keep it inert here
    monkeypatch.setattr(service_mod, "arm_timer", lambda state: None)
    return s


def _create(**overrides) -> CronJobCreate:
    base = dict(
        project_id="proj_" + uuid.uuid4().hex[:8],
        session_id="sess_" + uuid.uuid4().hex[:8],
        name="daily",
        schedule=CronScheduleCron(expr="0 9 * * *", tz="UTC"),
        task_prompt="report",
    )
    base.update(overrides)
    return CronJobCreate(**base)


async def test_add_applies_validation(svc, owned_session):
    with pytest.raises(ValueError, match="more often"):
        await svc.add("u_" + uuid.uuid4().hex[:6], _create(schedule=CronScheduleEvery(every_ms=1000)))


async def test_add_staggers_top_of_hour_jobs(svc, owned_session):
    from cron.schedule import stagger_ms_for

    result = await svc.add("u_" + uuid.uuid4().hex[:6], _create())
    assert result["next_run_at"] is not None
    from datetime import datetime

    next_run = datetime.fromisoformat(result["next_run_at"])
    offset_s = (stagger_ms_for(result["id"]) // 1000)
    # Fires at 09:00 UTC plus the deterministic per-job offset (< 5 min)
    assert next_run.hour == 9
    assert next_run.minute == offset_s // 60
    assert 0 <= next_run.minute < 5


async def test_pause_and_resume_roundtrip(svc, owned_session):
    user_id = "u_" + uuid.uuid4().hex[:6]
    created = await svc.add(user_id, _create(schedule=CronScheduleEvery(every_ms=600_000)))

    assert await svc.pause_all(user_id) == 1
    job = await svc.get_job(created["id"], user_id)
    assert job["enabled"] is False and job["next_run_at"] is None

    assert await svc.resume_all(user_id) == 1
    job = await svc.get_job(created["id"], user_id)
    assert job["enabled"] is True and job["next_run_at"] is not None


async def test_status_reports_liveness(svc):
    status = await svc.status()
    assert status["running"] is False
    assert status["healthy"] is False
    assert "last_tick_at" in status and "next_run_at" in status

    svc._started = True
    svc._state.last_tick_at_ms = __import__("time").time() * 1000
    status = await svc.status()
    assert status["healthy"] is True


async def test_runs_include_temp_session_link(svc, owned_session):
    from datetime import datetime, timezone
    from db.base import get_db_session
    from db.models.cron import CronRun

    user_id = "u_" + uuid.uuid4().hex[:6]
    created = await svc.add(user_id, _create(schedule=CronScheduleEvery(every_ms=600_000)))
    async with get_db_session() as db:
        db.add(CronRun(
            id="cron_run_" + uuid.uuid4().hex[:8],
            job_id=created["id"],
            user_id=user_id,
            session_id="s",
            temp_session_id="sess_temp_42",
            status="ok",
            started_at=datetime.now(timezone.utc),
        ))

    runs = await svc.list_runs(created["id"], user_id)
    assert runs and runs[0]["temp_session_id"] == "sess_temp_42"
