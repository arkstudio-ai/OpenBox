"""Pull a browser diagnostic snapshot off a desktop, and keep the recent ones.

The collector itself is ``sandbox/obx_diag.py``. It is shipped to every
desktop with the browser runtime, but the backend never depends on the copy
that happens to be installed there: the command built here carries the
backend's own copy inline, so a desktop whose runtime repair has not landed
yet — or whose filesystem is read-only — still answers with the current
format. When the application tunnel is down the same command goes through ECD
Cloud Assistant instead.

Every snapshot is stored as a ``browser.diag`` row in ``desktop_events`` so a
tool error can cite ``[diag:<id>]`` and an admin can pull the full report from
``/api/admin/fleet/diag/<id>`` without touching the desktop again. When the
database is unavailable the snapshot falls back to a small process-local ring
under the same id, so the citation still resolves for as long as the process
lives.
"""
from __future__ import annotations

import base64
import gzip
import json
import secrets
import shlex
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from core.log import create_logger
from sandbox import events

log = create_logger("sandbox.diag")

# Lives beside this module so the backend image carries it; container/obx_diag.py
# is only a repository CLI forwarding here.
DIAG_SCRIPT_PATH = Path(__file__).resolve().parent / "obx_diag.py"
#: Where the browser runtime repair installs the collector on the desktop.
DIAG_TOOL = "/opt/openbox/tools/obx_diag.py"
#: Marker the action server uses to classify the command without seeing it.
DIAG_MARKER = "obx-diag"

#: How long a full collection may take on the desktop. The runtime check and
#: journald reads dominate; the script bounds every subprocess itself.
COLLECT_TIMEOUT = 75
#: Automatic captures for one desktop are spaced out so a flapping browser
#: does not turn every failed turn into a 30s diagnostic run.
CAPTURE_INTERVAL = 300
RECENT_LIMIT = 50


class DiagUnavailable(RuntimeError):
    """The collector ran but produced no report."""


def diag_script() -> str:
    return DIAG_SCRIPT_PATH.read_text()


def diag_command(*, session: str = "", lines: int = 60, journal: bool = True) -> str:
    """A shell command that runs the backend's collector on the desktop.

    The script travels gzip+base85 inside argv, so it is independent of what is
    installed on the desktop and still fits Cloud Assistant's 16 KiB cap.
    """
    payload = base64.b85encode(gzip.compress(diag_script().encode(), mtime=0)).decode()
    loader = (
        "import base64,gzip,sys;"
        "src=gzip.decompress(base64.b85decode(sys.argv[1])).decode();"
        'sys.argv=["obx_diag"]+sys.argv[2:];'
        'exec(compile(src,"obx_diag.py","exec"),{"__name__":"__main__"})'
    )
    args = ["--lines", str(max(1, min(int(lines), 400)))]
    if session:
        args += ["--session", session[:80]]
    if not journal:
        args.append("--no-journal")
    return (
        f": {DIAG_MARKER}; python3 -c {shlex.quote(loader)} {shlex.quote(payload)} "
        + " ".join(shlex.quote(a) for a in args)
    )


def parse_report(output: str) -> dict | None:
    """The report is the last JSON object line; anything before it is noise."""
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "diag_version" in data:
            return data
    return None


async def collect_browser_diag(client, *, session: str = "", lines: int = 60) -> dict:
    """Run the collector through the action server and return its report."""
    result = await client.execute(
        diag_command(session=session, lines=lines), timeout=COLLECT_TIMEOUT
    )
    report = parse_report(result.stdout)
    if report is None:
        raise DiagUnavailable(
            f"collector exited {result.exit_code} without a report: "
            f"{(result.stderr or result.stdout or '')[-400:]}"
        )
    report["via"] = "action_server"
    return report


async def collect_desktop_diag(desktop_id: str, *, session: str = "", lines: int = 60) -> dict:
    """Run the collector through ECD Cloud Assistant (no tunnel required)."""
    from sandbox.channel import run_desktop_command

    output = await run_desktop_command(
        desktop_id, diag_command(session=session, lines=lines), timeout=COLLECT_TIMEOUT + 30
    )
    report = parse_report(output)
    if report is None:
        raise DiagUnavailable(f"cloud assistant returned no report: {output[-400:]}")
    report["via"] = "cloud_assistant"
    return report


# --- stored snapshots -------------------------------------------------------

#: Fallback for snapshots the database refused; same ids, process lifetime only.
_fallback: "OrderedDict[str, dict]" = OrderedDict()
_last_capture: dict[str, float] = {}


def _record(diag_id: str, ts: str, report: dict | None, *, desktop_id, container_key,
            session_id, reason, error, note) -> dict:
    summary = (report or {}).get("summary") or {}
    return {
        "id": diag_id,
        "ts": ts,
        "desktop_id": desktop_id,
        "container_key": container_key or desktop_id,
        "session_id": session_id,
        "kind": "browser.diag",
        "status": "ok" if report is not None else "fail",
        "reason": reason,
        "error": (error or "")[:8000],
        "note": note,
        "collected": report is not None,
        "via": (report or {}).get("via"),
        "lights": summary.get("lights"),
        "findings": summary.get("findings"),
        "report": report,
    }


async def remember(
    report: dict | None,
    *,
    desktop_id: str = "",
    container_key: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    reason: str = "",
    error: str = "",
    note: str = "",
) -> str:
    """Store a snapshot (or just the failure text) and return its id."""
    record = _record(
        "", datetime.now(timezone.utc).isoformat(), report, desktop_id=desktop_id,
        container_key=container_key, session_id=session_id, reason=reason, error=error, note=note,
    )
    lights = record["lights"] or {}
    headline = record["error"].splitlines()[0][:160] if record["error"] else ""
    summary = " | ".join(part for part in (
        reason,
        headline,
        " ".join(f"{k}={v}" for k, v in lights.items()) if lights else ("" if report else note),
    ) if part)
    detail = {k: record[k] for k in ("reason", "error", "note", "collected", "via", "lights", "findings", "report")}
    event_id = await events.emit(
        "browser.diag", status=record["status"], desktop_id=desktop_id, container_key=container_key,
        session_id=session_id, tool_call_id=tool_call_id, summary=summary, detail=detail,
    )
    if event_id:
        return event_id
    diag_id = "mem_" + secrets.token_hex(4)
    record["id"] = diag_id
    _fallback[diag_id] = record
    while len(_fallback) > RECENT_LIMIT:
        _fallback.popitem(last=False)
    return diag_id


def _from_event(event: dict) -> dict:
    detail = event.get("detail") or {}
    return {**{k: v for k, v in event.items() if k != "detail"}, **detail}


async def get(diag_id: str) -> dict | None:
    if diag_id in _fallback:
        return _fallback[diag_id]
    try:
        event = await events.get(diag_id)
    except Exception as exc:
        log.warning("diag lookup failed for %s: %s", diag_id, exc)
        return None
    if not event or event.get("kind") != "browser.diag":
        return None
    return _from_event(event)


async def list_recent(limit: int = RECENT_LIMIT, *, desktop_id: str = "") -> list[dict]:
    """Newest first, without the report bodies."""
    rows: list[dict] = []
    try:
        for event in await events.list_events(
            kind="browser.diag", desktop_id=desktop_id, limit=limit, with_detail=True
        ):
            row = _from_event(event)
            row.pop("report", None)
            rows.append(row)
    except Exception as exc:
        log.warning("diag listing failed: %s", exc)
    if not desktop_id:
        rows.extend({k: v for k, v in r.items() if k != "report"} for r in _fallback.values())
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return rows[:limit]


def clear() -> None:
    _fallback.clear()
    _last_capture.clear()


async def capture_failure(
    client,
    *,
    container_key: str,
    reason: str,
    error: BaseException | str,
    session_id: str = "",
    desktop_id: str = "",
) -> str:
    """Snapshot the desktop after a browser failure. Best-effort, never raises.

    Returns the diag id, which is also attached to `error` as ``diag_id`` when
    it is an exception, so the tool layer can cite it.
    """
    text = _error_text(error)
    key = container_key or desktop_id or "unknown"
    now = time.monotonic()
    report = None
    note = ""
    if now - _last_capture.get(key, -CAPTURE_INTERVAL) < CAPTURE_INTERVAL:
        note = f"collection skipped: last capture for {key} under {CAPTURE_INTERVAL}s ago"
    else:
        _last_capture[key] = now
        try:
            report = await collect_browser_diag(client, session=session_id)
        except Exception as exc:  # the desktop may be exactly what is broken
            note = f"collection failed: {type(exc).__name__}: {exc}"[:400]
    trace = events.trace_of(client)
    diag_id = await remember(
        report,
        desktop_id=desktop_id or getattr(client, "desktop_id", "") or "",
        container_key=container_key,
        session_id=session_id or trace.get("session_id", ""),
        tool_call_id=trace.get("tool_call_id", ""),
        reason=reason,
        error=text,
        note=note,
    )
    if isinstance(error, BaseException):
        try:
            error.diag_id = diag_id  # type: ignore[attr-defined]
        except Exception:
            pass
    summary = (report or {}).get("summary") or {}
    log.warning(
        "browser failure diag=%s key=%s reason=%s lights=%s findings=%s note=%s error=%s",
        diag_id, key, reason, summary.get("lights"), summary.get("findings"), note,
        text.splitlines()[0][:300] if text else "",
    )
    return diag_id


def _error_text(error: BaseException | str) -> str:
    if isinstance(error, BaseException):
        parts = [f"{type(error).__name__}: {error}"]
        cause = error.__cause__ or error.__context__
        depth = 0
        while cause is not None and depth < 3:
            parts.append(f"caused by {type(cause).__name__}: {cause}")
            cause = cause.__cause__ or cause.__context__
            depth += 1
        return "\n".join(parts)
    return str(error)


def summarize_error(error: BaseException | str, limit: int = 300) -> str:
    """First line of an error plus its diag reference, for a tool message.

    Log tails and problem lists stay in the diag record; the model only needs
    the headline and a handle an admin can look up.
    """
    text = str(error).strip()
    head = text.splitlines()[0] if text else type(error).__name__ if isinstance(error, BaseException) else ""
    if len(head) > limit:
        head = head[: limit - 1] + "…"
    diag_id = getattr(error, "diag_id", "")
    return f"{head} [diag:{diag_id}]" if diag_id else head
