"""Injector: busy queueing, silent-flush consumption, localized scaffolding."""
import uuid
from datetime import datetime, timezone

import cron.injector as injector
from cron.i18n import SILENT_SENTINEL


class FakeSession:
    def __init__(self, status="idle"):
        self.status = status
        self.token_usage = None
        self.model = "m"


async def test_busy_session_queues_instead_of_injecting(monkeypatch):
    import session.session as sess

    async def busy(sid, user_id=None, **kw):
        return FakeSession(status="busy")

    monkeypatch.setattr(sess, "get_session", busy)

    job = {"session_id": "s1", "user_id": "u1", "id": "j1", "name": "n", "task_prompt": "p"}
    assert await injector.try_inject_result("run1", job, "result") is False


async def test_missing_session_does_not_inject(monkeypatch):
    import session.session as sess

    async def nobody(sid, user_id=None, **kw):
        return None

    monkeypatch.setattr(sess, "get_session", nobody)
    job = {"session_id": "gone", "user_id": "u1", "id": "j1", "name": "n", "task_prompt": "p"}
    assert await injector.try_inject_result("run1", job, "result") is False


async def _insert_run(session_id: str, summary: str | None) -> str:
    from db.base import get_db_session
    from db.models.cron import CronRun

    run_id = "cron_run_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(CronRun(
            id=run_id,
            job_id="cron_j_" + uuid.uuid4().hex[:6],
            user_id="u1",
            session_id=session_id,
            status="ok",
            summary_text=summary,
            injected=False,
            started_at=datetime.now(timezone.utc),
        ))
    return run_id


async def _run_injected(run_id: str) -> bool:
    from db.base import get_db_session
    from db.models.cron import CronRun
    from sqlalchemy import select

    async with get_db_session() as db:
        row = (await db.execute(select(CronRun).where(CronRun.id == run_id))).scalar_one()
    return row.injected


async def test_overflow_triggers_compaction_before_injection(monkeypatch):
    import agent.compaction as compaction
    import session.session as sess
    from models.message import TokenUsage

    class FullSession:
        model = "m"
        token_usage = TokenUsage(input=0, output=0, total=0, limit=1000, context=990)

    async def full(sid, user_id=None, **kw):
        return FullSession()

    compactions = []

    async def fake_create(sid, auto=False, user_id=None, **kw):
        compactions.append(sid)

    async def fake_process(*a, **kw):
        return None

    async def fake_messages(sid, user_id=None, **kw):
        return []

    monkeypatch.setattr(sess, "get_session", full)
    monkeypatch.setattr(sess, "get_messages", fake_messages)
    monkeypatch.setattr(compaction, "create_compaction", fake_create)
    monkeypatch.setattr(compaction, "process_compaction", fake_process)

    await injector._check_and_compact_if_needed("s1", "u1", "job", "x" * 400)
    assert compactions == ["s1"]

    # Far from the limit: no compaction
    FullSession.token_usage = TokenUsage(input=0, output=0, total=0, limit=100000, context=10)
    await injector._check_and_compact_if_needed("s1", "u1", "job", "tiny")
    assert compactions == ["s1"]


async def test_flush_consumes_silent_results_without_messages(monkeypatch):
    session_id = "sess_" + uuid.uuid4().hex[:8]
    silent_run = await _insert_run(session_id, SILENT_SENTINEL)
    empty_run = await _insert_run(session_id, "")
    loud_run = await _insert_run(session_id, "real result")

    injected_messages = []

    async def fake_inject_messages(sid, uid, job_id, job_name, task_prompt, result_text):
        injected_messages.append(result_text)

    async def no_compact(*a, **kw):
        return None

    monkeypatch.setattr(injector, "_inject_messages", fake_inject_messages)
    monkeypatch.setattr(injector, "_check_and_compact_if_needed", no_compact)

    count = await injector.flush_pending_cron_results(session_id, "u1")

    # Only the loud run creates messages, but every run is consumed.
    assert injected_messages == ["real result"]
    assert count == 1
    assert await _run_injected(silent_run) is True
    assert await _run_injected(empty_run) is True
    assert await _run_injected(loud_run) is True
