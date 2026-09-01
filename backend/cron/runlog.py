"""Per-day markdown run log in the project workspace.

Every run — loud, silent, or failed — appends an entry to
<project>/cron/<YYYY-MM-DD>.md. The chat stays quiet for silent runs; this
file is the complete audit trail, visible in the Files panel, committable,
and readable by the next run's agent (cheap cross-run memory).

Entry assembly is pure so it unit-tests without a sandbox; delivery is
best-effort — a run never fails because its log line could not be written.
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.log import create_logger
from cron.i18n import text

log = create_logger("cron.runlog")

RUNLOG_DIR = "cron"


def _job_timezone(job: dict):
    """The tz the user thinks in for this job: cron tz, else UTC."""
    import zoneinfo

    schedule = job.get("schedule") or {}
    tz_name = schedule.get("tz") if isinstance(schedule, dict) else None
    if tz_name:
        try:
            return zoneinfo.ZoneInfo(tz_name)
        except Exception:
            pass
    return timezone.utc


def log_filename(job: dict, now: datetime | None = None) -> str:
    """cron/<YYYY-MM-DD>.md, dated in the job's own timezone."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_job_timezone(job))
    return f"{RUNLOG_DIR}/{local.strftime('%Y-%m-%d')}.md"


def build_log_entry(
    job: dict,
    status: str,
    result_text: str | None,
    duration_ms: int,
    total_tokens: int,
    silent: bool,
    locale: str = "zh-CN",
    now: datetime | None = None,
) -> str:
    """One markdown section for this run. Pure function."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_job_timezone(job))
    stamp = local.strftime("%H:%M")

    if status == "ok" and silent:
        mark = "✓"
        body = text(locale, "runlog_silent")
    elif status == "ok":
        mark = "✓"
        body = (result_text or "").strip()
    else:
        mark = "✗"
        body = (result_text or "").strip()

    seconds = duration_ms / 1000
    meta_bits = [f"{seconds:.0f}s"]
    if total_tokens:
        meta_bits.append(f"{total_tokens} tokens")
    meta = " · ".join(meta_bits)

    lines = [f"## {stamp} {job.get('name', 'unnamed')} {mark}", f"> {meta}", ""]
    if body:
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def file_header(job: dict, filename: str, locale: str = "zh-CN") -> str:
    """First line of a fresh daily file."""
    date_part = filename.rsplit("/", 1)[-1].removesuffix(".md")
    return f"# {text(locale, 'runlog_title', date=date_part)}\n\n"


async def append_run_log(
    temp_session_id: str,
    user_id: str,
    job: dict,
    entry: str,
    locale: str = "zh-CN",
    *,
    delivery_id: str | None = None,
    filename_override: str | None = None,
) -> bool:
    """Append the entry to today's log in the job's project workspace.

    Best-effort: returns False (and logs) on any failure.
    """
    try:
        from sandbox import sandbox_manager

        client = await sandbox_manager.get_client(temp_session_id, user_id=user_id)
        workdir = await sandbox_manager.get_session_workdir(temp_session_id)
        filename = filename_override or log_filename(job)
        path = f"{workdir}/{filename}"

        if delivery_id:
            # One remote process performs marker check + append while holding
            # an advisory file lock.  This closes both the retry-after-crash
            # duplicate and the multi-replica read/append race.
            import base64
            import shlex

            marker = f"<!-- openbox-cron-delivery:{delivery_id} -->"
            block = f"{marker}\n{entry}"
            header = file_header(job, filename, locale)
            script = (
                "import base64,fcntl,os,sys;"
                "p=base64.b64decode(sys.argv[1]).decode();"
                "m=base64.b64decode(sys.argv[2]).decode();"
                "b=base64.b64decode(sys.argv[3]).decode();"
                "h=base64.b64decode(sys.argv[4]).decode();"
                "os.makedirs(os.path.dirname(p),exist_ok=True);"
                "f=open(p,'a+',encoding='utf-8');"
                "fcntl.flock(f.fileno(),fcntl.LOCK_EX);"
                "f.seek(0);e=f.read();"
                "f.write(('' if e else h)+('' if m in e else b));"
                "f.flush();os.fsync(f.fileno());f.close()"
            )
            encoded = [
                base64.b64encode(value.encode("utf-8")).decode("ascii")
                for value in (path, marker, block, header)
            ]
            command = "python3 -c {} {}".format(
                shlex.quote(script),
                " ".join(shlex.quote(value) for value in encoded),
            )
            result = await client.execute(command, timeout=30, workdir=workdir)
            if result.exit_code != 0:
                raise RuntimeError(result.stderr or "runlog append command failed")
            return True

        # write_file may not create parent directories; the first entry of a
        # project must not fail on a missing cron/ dir.
        try:
            await client.execute(f"mkdir -p '{workdir}/{RUNLOG_DIR}'", timeout=15, workdir=workdir)
        except Exception:
            pass

        existing = ""
        try:
            existing = await client.read_file(path) or ""
        except Exception:
            existing = ""  # first entry of the day

        if not existing:
            existing = file_header(job, filename, locale)

        await client.write_file(path, existing + entry)
        return True
    except Exception as e:
        log.warning(f"Run log append failed for job {job.get('id')}: {e}")
        return False
