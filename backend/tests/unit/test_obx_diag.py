"""The desktop collector must survive any desktop, including one that is not Linux."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "sandbox" / "obx_diag.py"


def _load():
    spec = importlib.util.spec_from_file_location("obx_diag_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runs_end_to_end_with_no_desktop_and_exits_zero():
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--no-journal", "--lines", "5"],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert set(report) >= {"host", "chrome", "relay", "x", "units", "runtime", "logs", "summary", "errors", "timings_ms"}
    # Nothing is reachable here, and that must be a diagnosis, not a crash.
    assert report["summary"]["lights"]["chrome"] == "down"
    assert report["chrome"]["version"]["ok"] is False
    assert report["chrome"]["version"]["stage"] == "transport"
    assert report["logs"]["journal"] == {"skipped": True}


def test_python_310_compatible_syntax():
    """Desktops run Ubuntu 22.04's Python 3.10; the script must not need newer syntax."""
    import ast
    source = SCRIPT.read_text()
    ast.parse(source, filename=str(SCRIPT), feature_version=(3, 10))
    assert "tomllib" not in source and "ExceptionGroup" not in source and "except*" not in source


def test_chrome_args_are_reduced_to_the_allow_list():
    mod = _load()
    argv = [
        "/opt/google/chrome/chrome", "--user-data-dir=/home/u/.config/obx-chrome",
        "--remote-debugging-port=9333", "https://example.com/?token=SECRET",
        "--load-extension=/x", "--proxy-server=http://user:pass@host", "--headless=new",
    ]
    kept = mod._filter_chrome_args(argv)
    assert kept == ["--user-data-dir=/home/u/.config/obx-chrome", "--remote-debugging-port=9333", "--headless=new"]
    assert not any("SECRET" in k or "pass@" in k for k in kept)


def test_summary_flags_the_known_failure_modes():
    mod = _load()
    report = {
        "chrome": {
            "version": {"ok": False, "stage": "transport", "error": "refused"},
            "processes": [{"pid": 1, "uid": 1000}],
            "other_chrome_pids": [7],
            "profiles": [{"path": "/p", "exists": True, "uid": 0,
                          "singleton_lock": "host-4242", "singleton_pid_alive": False}],
            "total_threads": 900,
        },
        "relay": {"status": {"ok": True, "data": {"chromeAvailable": False, "error": "ECONNREFUSED"}},
                  "pid_file": "5", "pid_alive": False},
        "x": {"resolution": "1024x768", "obx_x": {"exit_code": 0}},
        "units": {"openbox-action-server.service": {"ActiveState": "active", "TasksCurrent": "1900",
                                                     "TasksMax": "2048", "NRestarts": "2"},
                  "openbox-tunnel.service": {"ActiveState": "activating", "SubState": "auto-restart"}},
        "runtime": {"installed": True, "check": {"ready": False, "problems": ["missing command: node"]}},
    }
    summary = mod.summarize(report)
    assert summary["lights"] == {"chrome": "down", "relay": "degraded", "x": "degraded",
                                 "unit": "degraded", "runtime": "down"}
    findings = "\n".join(summary["findings"])
    for expected in ("alive but :9333", "without the debug port", "stale SingletonLock",
                     "owned by uid 0 but Chrome runs as uid 1000", "chromeAvailable=false",
                     "1024x768", "1900/2048", "restarted 2", "openbox-tunnel is activating",
                     "runtime: missing command: node"):
        assert expected in findings, expected


def test_summary_is_quiet_on_a_healthy_desktop():
    mod = _load()
    report = {
        "chrome": {"version": {"ok": True}, "processes": [{"pid": 1, "uid": 1000}],
                   "profiles": [{"path": "/p", "exists": True, "uid": 1000, "singleton_lock": "h-1",
                                 "singleton_pid_alive": True}], "total_threads": 100},
        "relay": {"status": {"ok": True, "data": {"chromeAvailable": True}}},
        "x": {"resolution": "1920x1080", "obx_x": {"exit_code": 0}},
        "units": {"openbox-action-server.service": {"ActiveState": "active", "TasksCurrent": "300",
                                                     "TasksMax": "4096", "NRestarts": "0"},
                  "openbox-tunnel.service": {"ActiveState": "active"}},
        "runtime": {"installed": True, "check": {"ready": True}},
    }
    summary = mod.summarize(report)
    assert set(summary["lights"].values()) == {"ok"}
    assert summary["findings"] == []


def test_http_probe_reports_stage():
    mod = _load()
    probe = mod._http_json("http://127.0.0.1:1/", timeout=0.5)
    assert probe["ok"] is False and probe["stage"] == "transport"


def test_cmdline_accepts_chromes_space_joined_argv(tmp_path, monkeypatch):
    """Chrome rewrites its argv (process title), so /proc/<pid>/cmdline comes back
    space-separated; the collector then saw 12 Chrome processes and no browser."""
    mod = _load()
    proc = tmp_path / "proc" / "4242"; proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"/opt/google/chrome/chrome --remote-debugging-port=9333 --type=zygote --user-data-dir=/p")
    monkeypatch.setattr(mod, "_read", lambda path, limit=64 * 1024: (tmp_path / path.lstrip("/")).read_bytes().decode())
    assert mod._cmdline(4242) == ["/opt/google/chrome/chrome", "--remote-debugging-port=9333", "--type=zygote", "--user-data-dir=/p"]
    (proc / "cmdline").write_bytes(b"/usr/bin/node\x00relay.ts\x00")
    assert mod._cmdline(4242) == ["/usr/bin/node", "relay.ts"]
