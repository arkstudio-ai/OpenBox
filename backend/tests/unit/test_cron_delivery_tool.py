"""Webhook delivery SSRF hardening + chat-tool recursion guard."""
import uuid
from datetime import datetime, timezone

from cron.delivery import dispatch_delivery


async def test_dispatch_none_succeeds_quietly():
    result = await dispatch_delivery({}, job_name="n", job_id="j", status="ok", summary_text="s", duration_ms=1)
    assert result.success is True


async def test_dispatch_channel_still_unimplemented():
    result = await dispatch_delivery(
        {"mode": "channel", "channel": "slack"},
        job_name="n", job_id="j", status="ok", summary_text="s", duration_ms=1,
    )
    assert result.success is False


async def test_webhook_to_private_address_is_refused_at_send_time():
    """Even if a bad URL slipped past creation-time validation."""
    result = await dispatch_delivery(
        {"mode": "webhook", "webhook_url": "http://127.0.0.1:8080/internal"},
        job_name="n", job_id="j", status="ok", summary_text="s", duration_ms=1,
    )
    assert result.success is False
    assert "private or local" in (result.error or "")


class FakeCtx:
    def __init__(self, session_id):
        self.session_id = session_id
        self.origin_session_id = None
        self.project_id = "proj_" + uuid.uuid4().hex[:6]
        self.user_id = "u_" + uuid.uuid4().hex[:6]


async def test_cron_tool_refuses_to_schedule_from_a_cron_run():
    from db.base import get_db_session
    from db.models.cron import CronRun
    from tool.cron_tool import CronToolArgs, execute

    temp_sid = "sess_tmp_" + uuid.uuid4().hex[:8]
    async with get_db_session() as db:
        db.add(CronRun(
            id="cron_run_" + uuid.uuid4().hex[:10],
            job_id="cron_x",
            user_id="u1",
            session_id="sess_main",
            temp_session_id=temp_sid,
            status="running",
            started_at=datetime.now(timezone.utc),
        ))

    result = await execute(
        CronToolArgs(action="add", name="evil", schedule="0 9 * * *", task="spawn more"),
        FakeCtx(temp_sid),
    )
    assert result.title == "Not allowed"
    assert "cannot create" in result.output


def test_tool_schedule_parsing():
    from tool.cron_tool import _parse_schedule

    every = _parse_schedule("every 30m")
    assert every is not None and every.every_ms == 30 * 60_000
    cron = _parse_schedule("0 9 * * *", tz="Asia/Shanghai")
    assert cron is not None and cron.expr == "0 9 * * *" and cron.tz == "Asia/Shanghai"
    assert _parse_schedule("every 10s") is None       # sub-minute refused at parse
    assert _parse_schedule("gibberish") is None
