"""Desktop browser diagnostics: collection command, ring buffer, error citations."""
import base64
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sandbox import diag


@pytest.fixture(autouse=True)
async def fresh_store():
    from sandbox import events

    diag.clear()
    await events.purge(older_than_days=-1)
    yield
    diag.clear()


def test_command_is_self_contained_and_fits_cloud_assistant():
    command = diag.diag_command(session="sess-1", lines=30)
    assert command.startswith(f": {diag.DIAG_MARKER}; python3 -c ")
    assert "--session sess-1" in command and "--lines 30" in command
    # Cloud Assistant caps the base64-encoded command at 16 KiB.
    assert len(base64.b64encode(command.encode())) < 16_000
    # It carries the collector inline, so it never depends on the installed copy.
    assert diag.DIAG_TOOL not in command
    assert "--no-journal" in diag.diag_command(journal=False)


def test_command_runs_the_real_collector_locally():
    """The inline loader must reproduce `python3 obx_diag.py` exactly."""
    done = subprocess.run(
        ["sh", "-c", diag.diag_command(lines=3, journal=False)],
        capture_output=True, text=True, timeout=60,
    )
    report = diag.parse_report(done.stdout)
    assert report is not None, done.stderr[-500:]
    assert report["diag_version"] and "summary" in report and "errors" in report
    assert report["summary"]["lights"].keys() == {"chrome", "relay", "x", "unit", "runtime"}


def test_parse_report_takes_the_last_json_object_line():
    noise = "warning: something\n{\"not\": \"a report\"}\n"
    body = json.dumps({"diag_version": "x", "summary": {}})
    assert diag.parse_report(noise + body + "\ntrailing") == {"diag_version": "x", "summary": {}}
    assert diag.parse_report("nothing here") is None
    assert diag.parse_report("") is None


async def test_collect_raises_when_the_collector_produced_nothing():
    client = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        exit_code=1, stdout="", stderr="python3: not found")))
    with pytest.raises(diag.DiagUnavailable, match="not found"):
        await diag.collect_browser_diag(client)


async def test_collect_tags_the_transport():
    report = {"diag_version": "x", "summary": {"lights": {}}}
    client = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        exit_code=0, stdout=json.dumps(report), stderr="")))
    assert (await diag.collect_browser_diag(client, session="s"))["via"] == "action_server"
    assert "--session s" in client.execute.await_args.args[0]


async def test_capture_failure_records_report_and_cites_it_on_the_exception():
    report = {"diag_version": "x", "summary": {"lights": {"chrome": "down"}, "findings": ["no Chrome"]}}
    client = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        exit_code=0, stdout=json.dumps(report), stderr="")))
    error = RuntimeError("Chrome did not open its debug port\nchrome log:\n...40 lines...")
    diag_id = await diag.capture_failure(client, container_key="ecd-1", reason="ChromeUnavailable", error=error)
    assert error.diag_id == diag_id
    stored = await diag.get(diag_id)
    assert stored["collected"] is True and stored["report"] == {**report, "via": "action_server"}
    assert stored["error"].startswith("RuntimeError: Chrome did not open")
    assert stored["kind"] == "browser.diag" and stored["lights"] == {"chrome": "down"}
    assert diag.summarize_error(error) == f"Chrome did not open its debug port [diag:{diag_id}]"
    listed = await diag.list_recent()
    assert listed[0]["id"] == diag_id and "report" not in listed[0]
    assert listed[0]["summary"].startswith("ChromeUnavailable | RuntimeError: Chrome did not open")


async def test_repeated_failures_on_one_desktop_do_not_recollect():
    report = {"diag_version": "x", "summary": {}}
    client = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        exit_code=0, stdout=json.dumps(report), stderr="")))
    first = await diag.capture_failure(client, container_key="ecd-1", reason="r", error="boom")
    second = await diag.capture_failure(client, container_key="ecd-1", reason="r", error="boom again")
    other = await diag.capture_failure(client, container_key="ecd-2", reason="r", error="boom")
    assert client.execute.await_count == 2
    assert (await diag.get(first))["collected"] and (await diag.get(other))["collected"]
    skipped = await diag.get(second)
    assert skipped["collected"] is False and "skipped" in skipped["note"]
    assert [row["id"] for row in await diag.list_recent(desktop_id="ecd-2")] == [other]


async def test_capture_failure_never_raises_when_the_desktop_is_gone():
    error = RuntimeError("tunnel down")
    diag_id = await diag.capture_failure(SimpleNamespace(), container_key="ecd-x", reason="r", error=error)
    stored = await diag.get(diag_id)
    assert stored["collected"] is False and "collection failed" in stored["note"]
    assert stored["status"] == "fail"
    assert error.diag_id == diag_id


def test_summarize_error_keeps_the_headline_only():
    error = RuntimeError("x" * 400 + "\nsecond line")
    assert len(diag.summarize_error(error)) == 300
    assert "second" not in diag.summarize_error(error)
    assert diag.summarize_error(RuntimeError("")) == "RuntimeError"


async def test_snapshot_survives_a_database_outage_in_memory(monkeypatch):
    from sandbox import events

    monkeypatch.setattr(events, "emit", AsyncMock(return_value=None))
    diag_id = await diag.remember({"diag_version": "x", "summary": {}}, reason="r")
    assert diag_id.startswith("mem_")
    assert (await diag.get(diag_id))["collected"] is True
    assert (await diag.list_recent())[0]["id"] == diag_id
    assert await diag.get("missing") is None


def test_error_text_includes_causes():
    try:
        try:
            raise ValueError("inner")
        except ValueError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as outer:
        text = diag._error_text(outer)
    assert text == "RuntimeError: outer\ncaused by ValueError: inner"
