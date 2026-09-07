#!/usr/bin/env python3
"""OpenBox desktop browser diagnostics: one read-only JSON snapshot.

Runs on the Wuying desktop (Python 3.10, stdlib only) either through the
action server, through ECD Cloud Assistant when the tunnel is down, or by an
operator with ``python3 /opt/openbox/tools/obx_diag.py``. Every section is
collected independently and reports its own failure in ``errors``; the script
itself never exits non-zero because of a broken desktop, only because of a
broken invocation.

It never writes under /opt, never restarts anything, never touches a profile,
and never prints command lines or page titles. Chrome argv is reduced to a
fixed allow-list, page URLs are truncated, and journald lines are only the
structured ``execute_trace``/``desktop_lease`` records the action server
already emits without command text.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

DIAG_VERSION = "20260907.1"
CHROME_PORT = 9333
RELAY_PORT = 9222
CHROME_LOG = "/tmp/obx-chrome.log"
RELAY_LOG = "/tmp/obx-relay.log"
IBUS_LOG = "/tmp/obx-ibus.log"
RELAY_PID = "/tmp/obx-relay.pid"
BROWSER_OPS_LOG = "/tmp/obx-browser-ops.jsonl"
REPAIR_TOOL = "/opt/openbox/tools/repair_browser_runtime.py"
STREAMING_LOG_GLOB = "/var/log/wuying/asp/linux-streaming-manager-*"
UNITS = (
    "openbox-action-server.service",
    "openbox-tunnel.service",
    "openbox-browser-runtime.service",
    "obx-display-guard.service",
)
ACTION_CGROUP = "/sys/fs/cgroup/system.slice/openbox-action-server.service"
TOOL_PATH = "/usr/local/bin:/opt/bossip/runtime/node/bin:/usr/bin:/bin"
#: Chrome switches worth seeing. Anything else (URLs, tokens, extension ids)
#: is dropped rather than risk leaking into a log.
CHROME_ARG_ALLOW = (
    "--user-data-dir", "--remote-debugging-port", "--remote-debugging-address",
    "--headless", "--display", "--profile-directory", "--no-sandbox",
    "--disable-gpu", "--window-size", "--window-position", "--type",
    "--remote-allow-origins", "--no-first-run", "--disable-dev-shm-usage",
)
TAIL_BYTES = 16 * 1024
URL_LIMIT = 120


# --- helpers ---------------------------------------------------------------

def _run(argv, timeout=5, env=None):
    """Run a command; return (exit_code, stdout, stderr). Never raises."""
    merged = dict(os.environ)
    merged["PATH"] = TOOL_PATH
    merged["LC_ALL"] = "C"
    if env:
        merged.update(env)
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=merged,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover - defensive
        return -2, "", f"{type(exc).__name__}: {exc}"
    return done.returncode, done.stdout, done.stderr


def _tail(path, lines):
    """Last `lines` lines of a file, bounded by TAIL_BYTES. Never raises."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as stream:
            if size > TAIL_BYTES:
                stream.seek(size - TAIL_BYTES)
            data = stream.read().decode("utf-8", "replace")
        chunk = data.splitlines()[-lines:]
        return {"path": path, "size": size, "mtime": _iso(os.path.getmtime(path)), "lines": chunk}
    except FileNotFoundError:
        return {"path": path, "missing": True}
    except Exception as exc:
        return {"path": path, "error": f"{type(exc).__name__}: {exc}"}


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + "Z"


def _http_json(url, timeout=2.0):
    """GET a loopback JSON endpoint. Returns {"ok", "status"|"error", "data"}."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        return {"ok": False, "stage": "http", "status": exc.code, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "stage": "transport", "error": f"{type(exc).__name__}: {exc}"}
    try:
        return {"ok": True, "status": status, "data": json.loads(body)}
    except ValueError:
        return {"ok": False, "stage": "parse", "status": status, "error": body[:200]}


def _read(path, limit=64 * 1024):
    with open(path, "rb") as stream:
        return stream.read(limit).decode("utf-8", "replace")


def _proc_status(pid):
    """Selected /proc/<pid>/status fields as ints/strings."""
    fields = {}
    for line in _read(f"/proc/{pid}/status").splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if key in ("Uid", "Gid"):
            fields[key.lower()] = int(value.split()[0])
        elif key == "Threads":
            fields["threads"] = int(value)
        elif key == "VmRSS":
            fields["rss_kb"] = int(value.split()[0])
        elif key == "State":
            fields["state"] = value.split()[0]
    return fields


def _proc_start(pid):
    """Process start time as ISO, from /proc/<pid>/stat start ticks."""
    stat = _read(f"/proc/{pid}/stat")
    after = stat.rsplit(")", 1)[1].split()
    ticks = int(after[19])
    hertz = os.sysconf("SC_CLK_TCK")
    with open("/proc/uptime") as stream:
        uptime = float(stream.read().split()[0])
    started = time.time() - uptime + ticks / hertz
    return {"started_at": _iso(started), "age_s": round(time.time() - started)}


def _cmdline(pid):
    raw = _read(f"/proc/{pid}/cmdline", 256 * 1024)
    parts = [part for part in raw.split("\0") if part]
    # Chrome rewrites its argv in place (process title), after which the
    # kernel hands back one space-separated blob instead of NUL-separated
    # arguments. Seen on every Chrome process of a Wuying desktop.
    if len(parts) == 1 and " --" in parts[0]:
        parts = parts[0].split()
    return parts


def _filter_chrome_args(argv):
    kept = []
    for arg in argv[1:]:
        key = arg.split("=", 1)[0]
        if key in CHROME_ARG_ALLOW:
            kept.append(arg[:200])
    return kept


def _pids():
    try:
        names = os.listdir("/proc")
    except FileNotFoundError:  # not Linux: the HTTP probes still run
        return
    for name in names:
        if name.isdigit():
            yield int(name)


def _listening_ports(ports):
    """Which of `ports` have a LISTEN socket, from /proc/net/tcp(6)."""
    wanted = {port: False for port in ports}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = _read(table, 1024 * 1024).splitlines()[1:]
        except Exception:
            continue
        for row in rows:
            parts = row.split()
            if len(parts) < 4 or parts[3] != "0A":  # 0A == LISTEN
                continue
            port = int(parts[1].rsplit(":", 1)[1], 16)
            if port in wanted:
                wanted[port] = True
    return wanted


def _systemctl_show(unit, props):
    code, out, err = _run(["systemctl", "show", "-p", ",".join(props), "--", unit], timeout=5)
    if code != 0:
        return {"error": (err or out).strip()[:200] or f"exit {code}"}
    values = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key in props:
            values[key] = value
    return values


# --- sections --------------------------------------------------------------

def section_chrome(lines):
    out = {"processes": [], "renderers": 0, "total_processes": 0, "total_threads": 0}
    for pid in _pids():
        try:
            argv = _cmdline(pid)
        except Exception:
            continue
        if not argv or "chrome" not in os.path.basename(argv[0]).lower():
            continue
        if not any(a.startswith(f"--remote-debugging-port={CHROME_PORT}") for a in argv) \
                and not any(a.startswith("--type=") for a in argv):
            # Some other Chrome (a user-launched one without our port): count, don't detail.
            out.setdefault("other_chrome_pids", []).append(pid)
            continue
        try:
            status = _proc_status(pid)
        except Exception:
            continue
        out["total_processes"] += 1
        out["total_threads"] += status.get("threads", 0)
        kind = next((a.split("=", 1)[1] for a in argv if a.startswith("--type=")), "browser")
        if kind == "renderer":
            out["renderers"] += 1
        if kind == "browser":
            entry = {"pid": pid, "exe": argv[0][:200], "args": _filter_chrome_args(argv)}
            entry.update(status)
            try:
                entry.update(_proc_start(pid))
            except Exception as exc:
                entry["start_error"] = str(exc)[:100]
            try:
                env = _read(f"/proc/{pid}/environ", 256 * 1024).split("\0")
                entry["env"] = {k: v[:120] for k, _, v in (e.partition("=") for e in env)
                                if k in ("DISPLAY", "XAUTHORITY", "HOME", "USER")}
            except Exception:
                entry["env"] = None  # not root, or process gone
            out["processes"].append(entry)

    out["listening"] = _listening_ports([CHROME_PORT])[CHROME_PORT]
    out["version"] = _http_json(f"http://127.0.0.1:{CHROME_PORT}/json/version")
    targets = _http_json(f"http://127.0.0.1:{CHROME_PORT}/json/list")
    if targets.get("ok") and isinstance(targets.get("data"), list):
        by_type = {}
        pages = []
        for target in targets["data"]:
            kind = target.get("type", "?")
            by_type[kind] = by_type.get(kind, 0) + 1
            if kind == "page":
                pages.append({"url": (target.get("url") or "")[:URL_LIMIT],
                              "attached": target.get("attached")})
        out["targets"] = {"count": len(targets["data"]), "by_type": by_type, "pages": pages[:20]}
    else:
        out["targets"] = targets

    profiles = set()
    for proc in out["processes"]:
        for arg in proc["args"]:
            if arg.startswith("--user-data-dir="):
                profiles.add(arg.split("=", 1)[1])
    out["profiles"] = [_profile_info(path) for path in sorted(profiles)]
    out["log"] = _tail(CHROME_LOG, lines)
    return out


def _profile_info(path):
    info = {"path": path}
    try:
        st = os.stat(path)
        info.update({"exists": True, "uid": st.st_uid, "mode": oct(st.st_mode & 0o777)})
    except FileNotFoundError:
        info["exists"] = False
        return info
    port_file = os.path.join(path, "DevToolsActivePort")
    try:
        info["devtools_active_port"] = _read(port_file).splitlines()[0].strip()
    except Exception:
        info["devtools_active_port"] = None
    lock = os.path.join(path, "SingletonLock")
    try:
        target = os.readlink(lock)  # "<hostname>-<pid>"
        info["singleton_lock"] = target
        pid = target.rsplit("-", 1)[-1]
        info["singleton_pid_alive"] = pid.isdigit() and os.path.exists(f"/proc/{pid}")
    except FileNotFoundError:
        info["singleton_lock"] = None
    except Exception as exc:
        info["singleton_lock_error"] = str(exc)[:100]
    return info


def section_relay(lines):
    out = {"listening": _listening_ports([RELAY_PORT])[RELAY_PORT]}
    out["status"] = _http_json(f"http://127.0.0.1:{RELAY_PORT}/")
    try:
        pid = _read(RELAY_PID).strip()
        out["pid_file"] = pid
        out["pid_alive"] = pid.isdigit() and os.path.exists(f"/proc/{pid}")
    except FileNotFoundError:
        out["pid_file"] = None
    node = []
    for pid in _pids():
        try:
            argv = _cmdline(pid)
        except Exception:
            continue
        if argv and "node" in os.path.basename(argv[0]) and any("relay" in a for a in argv[1:4]):
            try:
                status = _proc_status(pid)
            except Exception:
                status = {}
            node.append({"pid": pid, "threads": status.get("threads"), "rss_kb": status.get("rss_kb")})
    out["node_processes"] = node
    out["log"] = _tail(RELAY_LOG, lines)
    return out


def section_x():
    out = {}
    code, out_text, err = _run(["obx-x", "sh", "-c", 'echo "$DISPLAY"; xrandr --current 2>&1 | head -3'], timeout=8)
    out["obx_x"] = {"exit_code": code, "stdout": out_text.strip()[:600], "stderr": err.strip()[:300]}
    match = re.search(r"current (\d+) x (\d+)", out_text)
    out["resolution"] = f"{match.group(1)}x{match.group(2)}" if match else None
    out["display_guard"] = _systemctl_show("obx-display-guard.service", ["ActiveState", "SubState", "NRestarts"])
    logs = sorted(glob.glob(STREAMING_LOG_GLOB), key=lambda p: os.path.getmtime(p))
    if logs:
        newest = logs[-1]
        tail = _tail(newest, 400)
        clients = [l for l in tail.get("lines", []) if "channel clients" in l]
        out["streaming"] = {"log": newest, "mtime": tail.get("mtime"),
                            "last_clients_line": clients[-1][-200:] if clients else None}
    else:
        out["streaming"] = None
    return out


def section_units():
    out = {}
    props = ["ActiveState", "SubState", "Result", "NRestarts", "TasksCurrent", "TasksMax",
             "MemoryCurrent", "ExecMainStartTimestamp", "ExecMainStatus", "FragmentPath"]
    for unit in UNITS:
        info = _systemctl_show(unit, props)
        dropins = []
        for root in ("/etc/systemd/system", "/etc/systemd/system.control"):
            dropins.extend(sorted(glob.glob(f"{root}/{unit}.d/*.conf")))
        info["dropins"] = dropins
        out[unit] = info
    cgroup = {}
    for name in ("pids.current", "pids.max", "memory.current"):
        try:
            cgroup[name] = _read(os.path.join(ACTION_CGROUP, name)).strip()
        except Exception:
            cgroup[name] = None
    out["action_cgroup"] = cgroup
    return out


def section_runtime():
    out = {"tool": REPAIR_TOOL, "installed": os.path.exists(REPAIR_TOOL)}
    if not out["installed"]:
        return out
    try:
        match = re.search(r'RUNTIME_VERSION\s*=\s*"([^"]+)"', _read(REPAIR_TOOL, 256 * 1024))
        out["installed_version"] = match.group(1) if match else None
    except Exception as exc:
        out["installed_version_error"] = str(exc)[:100]
    code, text, err = _run([sys.executable, REPAIR_TOOL, "--check"], timeout=25)
    out["check_exit_code"] = code
    for line in reversed(text.splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            out["check"] = data
            break
    else:
        out["check_error"] = (err or text).strip()[-400:]
    return out


def section_logs(lines, session, journal):
    out = {"ibus": _tail(IBUS_LOG, min(lines, 20))}
    if os.path.exists(BROWSER_OPS_LOG):
        out["browser_ops"] = _tail(BROWSER_OPS_LOG, lines)
    if not journal:
        out["journal"] = {"skipped": True}
        return out
    code, text, err = _run(
        ["journalctl", "-u", "openbox-action-server.service", "-n", str(max(lines * 6, 200)),
         "--no-pager", "-o", "short-iso"], timeout=8)
    if code != 0:
        out["journal"] = {"error": (err or text).strip()[:200] or f"exit {code}"}
    else:
        wanted = [l for l in text.splitlines()
                  if ("execute_trace" in l or "desktop_lease" in l or "diag_" in l)
                  and (not session or session in l)]
        out["journal"] = {"unit": "openbox-action-server.service", "filter_session": session or None,
                          "lines": [l[-600:] for l in wanted[-lines:]]}
    code, text, err = _run(["journalctl", "-u", "openbox-browser-runtime.service", "-n", "20",
                            "--no-pager", "-o", "short-iso"], timeout=8)
    out["runtime_journal"] = text.splitlines()[-20:] if code == 0 else {"error": (err or text)[:200]}
    return out


def section_host():
    out = {"hostname": socket.gethostname(), "uid": os.getuid(), "euid": os.geteuid(),
           "python": sys.version.split()[0]}
    try:
        out["loadavg"] = [round(v, 2) for v in os.getloadavg()]
    except Exception:
        pass
    try:
        with open("/proc/uptime") as stream:
            out["uptime_s"] = round(float(stream.read().split()[0]))
    except Exception:
        pass
    try:
        mem = {}
        for line in _read("/proc/meminfo").splitlines():
            key, _, value = line.partition(":")
            if key in ("MemTotal", "MemAvailable", "SwapFree"):
                mem[key] = int(value.split()[0])
        out["mem_kb"] = mem
    except Exception:
        pass
    disks = {}
    for path in ("/workspace", "/tmp", "/opt/openbox", "/"):
        try:
            vfs = os.statvfs(path)
            total = vfs.f_blocks * vfs.f_frsize
            free = vfs.f_bavail * vfs.f_frsize
            disks[path] = {"total_mb": total // 2**20, "free_mb": free // 2**20,
                           "used_pct": round(100 * (1 - free / total), 1) if total else None}
        except Exception:
            pass
    out["disk"] = disks
    code, text, _ = _run(["node", "--version"], timeout=5)
    out["node"] = text.strip() if code == 0 else None
    code, text, _ = _run(["npm", "--version"], timeout=8)
    out["npm"] = text.strip() if code == 0 else None
    out["backups"] = len(glob.glob("/opt/openbox/backups/*")) if os.path.isdir("/opt/openbox/backups") else 0
    return out


# --- summary ---------------------------------------------------------------

def summarize(report):
    """Five lights plus one-line findings an operator can act on."""
    lights = {}
    findings = []
    chrome = report.get("chrome") or {}
    relay = report.get("relay") or {}
    x = report.get("x") or {}
    units = report.get("units") or {}
    runtime = report.get("runtime") or {}

    version = chrome.get("version") or {}
    if version.get("ok") and chrome.get("processes"):
        lights["chrome"] = "ok"
    elif version.get("ok"):
        lights["chrome"] = "ok"
        findings.append("Chrome answers on :9333 but no browser process with that port was found (container view?)")
    elif chrome.get("processes"):
        lights["chrome"] = "down"
        findings.append(f"Chrome process alive but :9333 not answering ({version.get('stage')}: {str(version.get('error'))[:80]})")
    else:
        lights["chrome"] = "down"
        findings.append("no OpenBox Chrome process and :9333 not answering")
    if chrome.get("other_chrome_pids"):
        findings.append(f"{len(chrome['other_chrome_pids'])} Chrome process(es) without the debug port (icon-launched?)")
    for profile in chrome.get("profiles") or []:
        lock = profile.get("singleton_lock")
        if lock and profile.get("singleton_pid_alive") is False:
            findings.append(f"stale SingletonLock {lock} in {profile['path']}")
        for proc in chrome.get("processes") or []:
            if profile.get("exists") and "uid" in proc and profile.get("uid") not in (None, proc["uid"]):
                findings.append(f"profile {profile['path']} owned by uid {profile['uid']} but Chrome runs as uid {proc['uid']}")
    threads = chrome.get("total_threads") or 0

    status = relay.get("status") or {}
    if status.get("ok"):
        lights["relay"] = "ok"
        data = status.get("data") or {}
        if data.get("chromeAvailable") is False:
            lights["relay"] = "degraded"
            findings.append(f"relay up but chromeAvailable=false ({str(data.get('error'))[:80]})")
    else:
        lights["relay"] = "down"
        if relay.get("pid_file") and relay.get("pid_alive") is False:
            findings.append(f"relay pid file {relay['pid_file']} points to a dead process")

    resolution = x.get("resolution")
    if resolution == "1920x1080":
        lights["x"] = "ok"
    elif resolution:
        lights["x"] = "degraded"
        findings.append(f"desktop resolution is {resolution}, expected 1920x1080")
    else:
        lights["x"] = "down" if (x.get("obx_x") or {}).get("exit_code") not in (0, None) else "unknown"
        if lights["x"] == "down":
            findings.append(f"obx-x cannot reach an X session: {(x.get('obx_x') or {}).get('stderr') or (x.get('obx_x') or {}).get('stdout')}"[:160])

    action = units.get("openbox-action-server.service") or {}
    lights["unit"] = "ok" if action.get("ActiveState") == "active" else ("unknown" if "error" in action else "down")
    try:
        current, maximum = int(action.get("TasksCurrent", "")), int(action.get("TasksMax", ""))
        ratio = current / maximum if maximum else 0
        if ratio >= 0.8:
            lights["unit"] = "degraded"
            findings.append(f"action server tasks {current}/{maximum} ({ratio:.0%}); Chrome threads alone: {threads}")
        if maximum < 2048:
            findings.append(f"TasksMax={maximum} is below the required 2048")
    except (TypeError, ValueError):
        pass
    try:
        if int(action.get("NRestarts", "0")) > 0:
            findings.append(f"action server restarted {action['NRestarts']} time(s) since boot")
    except ValueError:
        pass
    tunnel = units.get("openbox-tunnel.service") or {}
    if tunnel.get("ActiveState") not in (None, "active") and "error" not in tunnel:
        findings.append(f"openbox-tunnel is {tunnel.get('ActiveState')}/{tunnel.get('SubState')}")

    check = runtime.get("check") or {}
    if not runtime.get("installed"):
        lights["runtime"] = "unknown"
    elif check.get("ready") is True:
        lights["runtime"] = "ok"
    else:
        lights["runtime"] = "down"
        for problem in check.get("problems") or ([runtime.get("check_error")] if runtime.get("check_error") else []):
            findings.append(f"runtime: {problem}")

    return {"lights": lights, "findings": findings}


# --- main ------------------------------------------------------------------

def collect(lines=60, session="", journal=True):
    started = time.monotonic()
    report = {"diag_version": DIAG_VERSION, "collected_at": _iso(time.time()), "errors": []}

    def section(name, fn, *args):
        t0 = time.monotonic()
        try:
            report[name] = fn(*args)
        except Exception as exc:
            report[name] = None
            report["errors"].append({"section": name, "error": f"{type(exc).__name__}: {exc}"[:300]})
        report.setdefault("timings_ms", {})[name] = round((time.monotonic() - t0) * 1000)

    section("host", section_host)
    section("chrome", section_chrome, lines)
    section("relay", section_relay, lines)
    section("x", section_x)
    section("units", section_units)
    section("runtime", section_runtime)
    section("logs", section_logs, lines, session, journal)
    try:
        report["summary"] = summarize(report)
    except Exception as exc:
        report["summary"] = None
        report["errors"].append({"section": "summary", "error": f"{type(exc).__name__}: {exc}"[:300]})
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lines", type=int, default=60, help="log tail length per file")
    parser.add_argument("--session", default="", help="only keep journald traces mentioning this session id")
    parser.add_argument("--no-journal", action="store_true", help="skip journalctl (faster, no root needed)")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    report = collect(lines=max(1, min(args.lines, 400)), session=args.session[:80], journal=not args.no_journal)
    print(json.dumps(report, indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":"),
                     sort_keys=True))


if __name__ == "__main__":
    main()
