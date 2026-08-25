"""Daily markdown run log: filename tz, entry assembly, best-effort append."""
from datetime import datetime, timezone

from cron.runlog import append_run_log, build_log_entry, file_header, log_filename

NOW = datetime(2026, 8, 25, 23, 30, tzinfo=timezone.utc)


def _job(**over):
    base = {"id": "cron_x", "name": "夜巡", "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"}}
    base.update(over)
    return base


def test_filename_uses_the_jobs_timezone():
    # 23:30 UTC on the 25th is already the 26th in Asia/Shanghai
    assert log_filename(_job(), NOW) == "cron/2026-08-26.md"
    utc_job = _job(schedule={"kind": "every", "every_ms": 600_000})
    assert log_filename(utc_job, NOW) == "cron/2026-08-25.md"


def test_entry_variants():
    ok = build_log_entry(_job(), "ok", "构建全部通过", 36_000, 12800, False, "zh-CN", NOW)
    assert "## 07:30 夜巡 ✓" in ok          # 23:30 UTC = 07:30 +08:00
    assert "36s · 12800 tokens" in ok
    assert "构建全部通过" in ok

    silent = build_log_entry(_job(), "ok", "NO_REPLY", 5_000, 900, True, "zh-CN", NOW)
    assert "✓" in silent and "无事可报" in silent and "NO_REPLY" not in silent

    err = build_log_entry(_job(), "error", "sandbox unreachable", 8_000, 0, False, "en-US", NOW)
    assert "✗" in err and "sandbox unreachable" in err
    assert "tokens" not in err               # zero tokens omitted


def test_file_header_localized():
    assert file_header(_job(), "cron/2026-08-26.md", "zh-CN").startswith("# 定时任务日志 · 2026-08-26")
    assert "Scheduled task log" in file_header(_job(), "cron/2026-08-26.md", "en-US")


async def test_append_creates_then_appends(monkeypatch):
    files: dict[str, str] = {}

    class FakeClient:
        async def read_file(self, path):
            if path not in files:
                raise FileNotFoundError(path)
            return files[path]

        async def write_file(self, path, content):
            files[path] = content

    class FakeManager:
        async def get_client(self, sid, *, user_id):
            return FakeClient()

        async def get_session_workdir(self, sid):
            return "/workspace/demo"

    import sandbox
    monkeypatch.setattr(sandbox, "sandbox_manager", FakeManager())

    job = _job()
    assert await append_run_log("sess_t", "u1", job, "entry-one\n", "zh-CN") is True
    assert await append_run_log("sess_t", "u1", job, "entry-two\n", "zh-CN") is True

    content = next(iter(files.values()))
    assert content.startswith("# 定时任务日志")
    assert content.index("entry-one") < content.index("entry-two")
    assert content.count("# 定时任务日志") == 1  # header written once


async def test_append_failure_is_swallowed(monkeypatch):
    class BrokenManager:
        async def get_client(self, sid, *, user_id):
            raise RuntimeError("sandbox offline")

        async def get_session_workdir(self, sid):
            return "/workspace/demo"

    import sandbox
    monkeypatch.setattr(sandbox, "sandbox_manager", BrokenManager())
    assert await append_run_log("sess_t", "u1", _job(), "entry\n") is False
