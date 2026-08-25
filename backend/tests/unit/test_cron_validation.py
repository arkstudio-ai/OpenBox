"""Service-layer validation: the REST API and the chat tool share one rulebook."""
import uuid
from datetime import datetime, timezone

import pytest

from core.config import get_config
from cron.types import CronDeliveryConfig, CronJobCreate, CronScheduleCron, CronScheduleEvery, CronScheduleAt
from cron.validation import (
    check_webhook_url,
    ensure_not_cron_session,
    validate_create,
    validate_delivery,
)


def _uid() -> str:
    return "u_" + uuid.uuid4().hex[:10]


def _create(**overrides) -> CronJobCreate:
    base = dict(
        project_id="proj_" + uuid.uuid4().hex[:10],
        session_id="sess_" + uuid.uuid4().hex[:10],
        name="job",
        schedule=CronScheduleEvery(every_ms=600_000),
        task_prompt="do the thing",
    )
    base.update(overrides)
    return CronJobCreate(**base)


class FakeSession:
    def __init__(self, sid):
        self.id = sid
        self.model = "m"


@pytest.fixture
def owned_session(monkeypatch):
    """Ownership checks pass for any session and any project."""
    import session.session as sess
    import project.workspace as ws

    async def fake_get_session(sid, user_id=None, **kw):
        return FakeSession(sid)

    async def fake_get_project(pid, user_id):
        return object()

    monkeypatch.setattr(sess, "get_session", fake_get_session)
    monkeypatch.setattr(ws, "get_project", fake_get_project)


async def test_rejects_unknown_project(monkeypatch):
    import project.workspace as ws

    async def nobody(pid, user_id):
        return None

    monkeypatch.setattr(ws, "get_project", nobody)
    with pytest.raises(ValueError, match="not found"):
        await validate_create(_uid(), _create())


async def test_rejects_unknown_notify_session(owned_session, monkeypatch):
    import session.session as sess

    async def nobody(sid, user_id=None, **kw):
        return None

    monkeypatch.setattr(sess, "get_session", nobody)
    with pytest.raises(ValueError, match="not found"):
        await validate_create(_uid(), _create())

    # Without a notify session the same job passes
    await validate_create(_uid(), _create(session_id=None))


async def test_rejects_bad_name_and_prompt(owned_session):
    with pytest.raises(ValueError, match="name"):
        await validate_create(_uid(), _create(name="  "))
    with pytest.raises(ValueError, match="name"):
        await validate_create(_uid(), _create(name="x" * 300))
    with pytest.raises(ValueError, match="[Tt]ask prompt"):
        await validate_create(_uid(), _create(task_prompt=" "))
    limit = get_config().cron_max_task_prompt_length
    with pytest.raises(ValueError, match="characters"):
        await validate_create(_uid(), _create(task_prompt="x" * (limit + 1)))


async def test_rejects_timeout_out_of_bounds(owned_session):
    with pytest.raises(ValueError, match="timeout_seconds"):
        await validate_create(_uid(), _create(timeout_seconds=1))
    with pytest.raises(ValueError, match="timeout_seconds"):
        await validate_create(_uid(), _create(timeout_seconds=10_000_000))


async def test_rejects_sub_minimum_intervals(owned_session):
    # every 60s < 5-minute floor
    with pytest.raises(ValueError, match="more often"):
        await validate_create(_uid(), _create(schedule=CronScheduleEvery(every_ms=60_000)))
    # every-minute cron expression
    with pytest.raises(ValueError, match="more often"):
        await validate_create(_uid(), _create(schedule=CronScheduleCron(expr="* * * * *")))
    # 10 minutes passes
    await validate_create(_uid(), _create(schedule=CronScheduleEvery(every_ms=600_000)))


async def test_rejects_past_one_shot_and_bad_expr(owned_session):
    with pytest.raises(ValueError, match="future"):
        await validate_create(_uid(), _create(schedule=CronScheduleAt(at="2000-01-01T00:00:00Z")))
    with pytest.raises(ValueError, match="[Ii]nvalid cron"):
        await validate_create(_uid(), _create(schedule=CronScheduleCron(expr="not a cron")))


def test_delivery_validation():
    validate_delivery(None)
    validate_delivery({"mode": "none"})
    with pytest.raises(ValueError, match="webhook_url"):
        validate_delivery({"mode": "webhook"})
    with pytest.raises(ValueError, match="not implemented"):
        validate_delivery({"mode": "channel", "channel": "slack"})
    with pytest.raises(ValueError, match="Unknown"):
        validate_delivery({"mode": "carrier-pigeon"})


def test_webhook_ssrf_rules():
    for bad in (
        "http://127.0.0.1/hook",
        "http://localhost:8080/hook",
        "http://10.1.2.3/hook",
        "http://192.168.1.1/hook",
        "http://172.16.0.9/hook",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://[::1]/hook",
    ):
        with pytest.raises(ValueError):
            check_webhook_url(bad)
    with pytest.raises(ValueError, match="http"):
        check_webhook_url("ftp://example.com/x")
    # Public literal IP passes without DNS
    check_webhook_url("https://8.8.8.8/hook")


async def test_per_project_and_per_user_quota(owned_session):
    from db.base import get_db_session
    from db.models.cron import CronJob

    user_id = _uid()
    project_id = "proj_" + uuid.uuid4().hex[:10]
    now = datetime.now(timezone.utc)
    config = get_config()

    per_project = config.cron_max_jobs_per_project
    async with get_db_session() as db:
        for i in range(per_project):
            db.add(CronJob(
                id=f"cron_{uuid.uuid4().hex[:12]}",
                user_id=user_id,
                project_id=project_id,
                session_id=None,
                name=f"j{i}",
                schedule={"kind": "every", "every_ms": 600_000},
                task_prompt="t",
                created_at=now,
                updated_at=now,
            ))

    with pytest.raises(ValueError, match="per project"):
        await validate_create(user_id, _create(project_id=project_id))

    # Same user, other projects, up to the per-user cap
    async with get_db_session() as db:
        for i in range(config.cron_max_jobs_per_user - per_project):
            db.add(CronJob(
                id=f"cron_{uuid.uuid4().hex[:12]}",
                user_id=user_id,
                project_id="proj_" + uuid.uuid4().hex[:10],
                session_id=None,
                name=f"k{i}",
                schedule={"kind": "every", "every_ms": 600_000},
                task_prompt="t",
                created_at=now,
                updated_at=now,
            ))

    with pytest.raises(ValueError, match="per user"):
        await validate_create(user_id, _create())


async def test_recursion_guard_blocks_cron_temp_sessions(owned_session):
    from db.base import get_db_session
    from db.models.cron import CronRun

    temp_sid = "sess_tmp_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(CronRun(
            id="cron_run_" + uuid.uuid4().hex[:10],
            job_id="cron_x",
            user_id=_uid(),
            session_id="sess_main",
            temp_session_id=temp_sid,
            status="ok",
            started_at=datetime.now(timezone.utc),
        ))

    with pytest.raises(ValueError, match="cannot create"):
        await ensure_not_cron_session(temp_sid)

    # A normal session passes
    await ensure_not_cron_session("sess_normal_" + uuid.uuid4().hex[:6])

    # And validate_create refuses to target a temp session
    with pytest.raises(ValueError, match="cannot create"):
        await validate_create(_uid(), _create(session_id=temp_sid))
