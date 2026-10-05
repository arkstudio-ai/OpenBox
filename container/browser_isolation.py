"""Startup evidence for the dedicated browser container's UID boundary.

These checks do not attest Docker host configuration. The backend separately
inspects the exact dedicated container, mounts, namespaces and image before
enrollment. No process or filesystem outside this new resource is modified.
"""
import asyncio
import fcntl
import json
import os
from pathlib import Path
import stat
import sys


CHECKS = (
    "linux", "supervisor_uid_zero", "browser_uid_distinct", "browser_gid_distinct",
    "browser_capabilities_empty", "no_new_privileges", "supervisor_capabilities_bounded",
    "root_filesystem_readonly", "protected_storage", "private_pipe",
    "browser_cannot_read_journal", "browser_cannot_write_journal",
    "browser_cannot_read_supervisor_environment", "browser_cannot_open_control_pipe",
    "browser_cannot_signal_supervisor",
)

# A Wuying guest is a full Linux machine, not a read-only container root.
# Its ordinary and actor executors must instead be distinct unprivileged
# identities, unable to access this root supervisor or this browser's profile.
WUYING_CHECKS = (
    "linux", "supervisor_uid_zero", "browser_uid_distinct", "executor_uid_distinct",
    "legacy_executor_uid_distinct", "browser_capabilities_empty", "no_new_privileges",
    "protected_storage", "protected_source", "protected_profile", "private_pipe",
    "chromium_sandbox_enabled", "browser_cannot_read_journal", "browser_cannot_write_journal",
    "browser_cannot_read_supervisor_environment", "browser_cannot_open_control_pipe",
    "browser_cannot_signal_supervisor", "browser_cannot_read_actor_workspace",
    "executor_cannot_read_profile", "executor_cannot_read_journal",
    "executor_cannot_write_journal", "executor_cannot_read_supervisor_environment",
    "executor_cannot_open_control_pipe", "executor_cannot_signal_supervisor",
    "legacy_executor_cannot_read_profile", "legacy_executor_cannot_read_journal",
    "legacy_executor_cannot_write_journal", "legacy_executor_cannot_read_supervisor_environment",
    "legacy_executor_cannot_open_control_pipe", "legacy_executor_cannot_signal_supervisor",
)


class BrowserIsolationError(Exception):
    pass


def pending(pipe):
    return {"mode": "diagnostic" if pipe.fixture_no_sandbox else pipe.isolation,
            "verification": "pending", "supervisor_uid": os.geteuid(),
            "browser_uid": pipe.uid, "browser_gid": pipe.gid,
            "checks": {name: False for name in (WUYING_CHECKS if pipe.isolation == "wuying_guest_uid" else CHECKS)}}


def _status(pid):
    return dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line)


def _protected_storage(journal):
    path = journal.path
    if path.is_symlink() or path.stat().st_uid != 0 or stat.S_IMODE(path.stat().st_mode) != 0o600:
        return False
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        return False
    return all(not parent.is_symlink() and parent.stat().st_uid == 0
               and not (parent.stat().st_mode & 0o022) for parent in path.parents)


# The child receives paths and PIDs only, never control secrets. It performs
# permission checks without writing bytes, delivering signals or truncating a
# file. A successful open closes its own descriptor and makes startup fail.
_PROBE = """import json,os,sys
os.setgroups([]);os.setgid(int(sys.argv[4]));os.setuid(int(sys.argv[3]))
paths=json.loads(sys.argv[1]);parent=int(sys.argv[2]);result={}
for name,path,flags in paths:
 try:
  fd=os.open(path,flags);os.close(fd);result[name]=False
 except PermissionError:result[name]=True
 except OSError:result[name]=False
try:os.kill(parent,0);result['browser_cannot_signal_supervisor']=False
except PermissionError:result['browser_cannot_signal_supervisor']=True
except OSError:result['browser_cannot_signal_supervisor']=False
result['uid']=os.geteuid();result['gid']=os.getegid()
print(json.dumps(result))
"""


async def verify(journal, pipe):
    if pipe.isolation == "wuying_guest_uid":
        return await _verify_wuying(journal, pipe)
    report = pending(pipe)
    checks = report["checks"]
    if report["mode"] == "diagnostic":
        # The diagnostic flag is never silently promoted into a deployable
        # configuration, even when the surrounding fixture is well isolated.
        report["verification"] = "diagnostic"
        return report
    try:
        checks["linux"] = sys.platform.startswith("linux")
        checks["supervisor_uid_zero"] = os.geteuid() == 0
        own, browser = _status(os.getpid()), _status(pipe.process.pid)
        checks["browser_uid_distinct"] = pipe.uid is not None and pipe.uid > 0 and [int(x) for x in browser["Uid"].split()] == [pipe.uid] * 4
        checks["browser_gid_distinct"] = pipe.gid is not None and pipe.gid > 0 and [int(x) for x in browser["Gid"].split()] == [pipe.gid] * 4
        checks["browser_capabilities_empty"] = all(int(browser[name], 16) == 0 for name in ("CapInh", "CapPrm", "CapEff", "CapAmb"))
        checks["no_new_privileges"] = own["NoNewPrivs"].strip() == browser["NoNewPrivs"].strip() == "1"
        allowed = sum(1 << bit for bit in (0, 5, 6, 7))  # CHOWN, KILL, SETGID, SETUID.
        checks["supervisor_capabilities_bounded"] = all(int(own[name], 16) & ~allowed == 0 for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"))
        checks["root_filesystem_readonly"] = any(line.split()[4] == "/" and "ro" in line.split()[5].split(",")
            for line in Path("/proc/self/mountinfo").read_text().splitlines())
        checks["protected_storage"] = _protected_storage(journal)
        writer = pipe._write.fileno()
        reader = pipe._read_transport.get_extra_info("pipe").fileno()
        argv = Path(f"/proc/{pipe.process.pid}/cmdline").read_bytes().split(b"\0")
        checks["private_pipe"] = (
            all(stat.S_ISFIFO(os.fstat(fd).st_mode) and os.fstat(fd).st_uid == 0
                and stat.S_IMODE(os.fstat(fd).st_mode) == 0o600 for fd in (writer, reader))
            and fcntl.fcntl(writer, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY
            and fcntl.fcntl(reader, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
            and b"--remote-debugging-pipe" in argv
            and not any(arg.startswith(b"--remote-debugging-port") for arg in argv)
            and pipe.live)
        if not all(checks[name] for name in CHECKS[:10]):
            raise BrowserIsolationError("Browser startup boundary verification failed")
        paths = [
            ("browser_cannot_read_journal", str(journal.path), os.O_RDONLY),
            ("browser_cannot_write_journal", str(journal.path), os.O_WRONLY),
            ("browser_cannot_read_supervisor_environment", f"/proc/{os.getpid()}/environ", os.O_RDONLY),
            ("browser_cannot_open_control_pipe", f"/proc/{os.getpid()}/fd/{writer}", os.O_WRONLY),
        ]
        # uvloop does not implement Popen's user/group keywords. A fresh
        # interpreter drops all groups/IDs before the immutable probe instead;
        # no preexec_fn is used in the threaded HTTP supervisor.
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", _PROBE, json.dumps(paths), str(os.getpid()),
            str(pipe.uid), str(pipe.gid), env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 5)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        proof = json.loads(output)
        if process.returncode != 0 or proof["uid"] != pipe.uid or proof["gid"] != pipe.gid:
            raise BrowserIsolationError("Browser adversarial identity verification failed")
        for name in CHECKS[10:]:
            checks[name] = proof.get(name) is True
        if not all(checks.values()):
            raise BrowserIsolationError("Browser startup boundary verification failed")
    except BrowserIsolationError:
        raise
    except (OSError, KeyError, ValueError, TypeError, asyncio.TimeoutError) as error:
        raise BrowserIsolationError("Browser startup boundary verification unavailable") from error
    report["verification"] = "passed"
    return report


def _root_owned(path):
    path = Path(path)
    return all(not item.is_symlink() and item.stat().st_uid == 0
               and not item.stat().st_mode & 0o022 for item in (path, *path.parents))


async def _identity_probe(uid, gid, paths, signal_key):
    process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-S", "-c", _PROBE,
        json.dumps(paths), str(os.getpid()), str(uid), str(gid),
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"}, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 5)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    result = json.loads(output)
    if process.returncode != 0 or result.get("uid") != uid or result.get("gid") != gid:
        raise BrowserIsolationError("Guest identity permission probe failed")
    result[signal_key] = result.pop("browser_cannot_signal_supervisor")
    return result


async def _verify_wuying(journal, pipe):
    import pwd
    from execution_identity import configured_user, identity, validate_configuration

    report = pending(pipe)
    checks = report["checks"]
    try:
        if pipe.fixture_no_sandbox or pipe.guest_binding is None:
            raise BrowserIsolationError("A protected Wuying guest binding is required")
        binding = pipe.guest_binding
        actor = pwd.getpwnam(binding.executor_user)
        browser_account = pwd.getpwnam(binding.browser_user)
        legacy = identity(configured_user())
        # A declared username alone does not prove the legacy launch path can
        # permanently drop privileges. Exercise the same production launcher.
        validate_configuration()
        checks["linux"] = sys.platform.startswith("linux")
        checks["supervisor_uid_zero"] = os.geteuid() == 0
        browser = _status(pipe.process.pid)
        checks["browser_uid_distinct"] = (pipe.uid == browser_account.pw_uid and pipe.uid > 0
            and pipe.gid == browser_account.pw_gid and pipe.gid > 0
            and [int(x) for x in browser["Uid"].split()] == [pipe.uid] * 4
            and [int(x) for x in browser["Gid"].split()] == [pipe.gid] * 4)
        checks["executor_uid_distinct"] = actor.pw_uid > 0 and actor.pw_gid > 0 and actor.pw_uid != pipe.uid
        checks["legacy_executor_uid_distinct"] = legacy.pw_uid > 0 and legacy.pw_uid not in {pipe.uid, actor.pw_uid}
        checks["browser_capabilities_empty"] = all(int(browser[name], 16) == 0
            for name in ("CapInh", "CapPrm", "CapEff", "CapAmb"))
        checks["no_new_privileges"] = browser["NoNewPrivs"].strip() == "1"
        checks["protected_storage"] = _protected_storage(journal)
        checks["protected_source"] = all(_root_owned(Path(__file__).with_name(name)) for name in (
            "action_server.py", "browser_resource.py", "browser_pipe.py", "browser_pipe_launcher.py",
            "browser_isolation.py", "private_actor.py", "execution_identity.py", "file_worker.py"))
        home = Path(binding.browser_home)
        checks["protected_profile"] = (home == pipe.profile.parent and not home.is_symlink()
            and home.stat().st_uid == pipe.uid and stat.S_IMODE(home.stat().st_mode) == 0o700
            and _root_owned(home.parent))
        writer = pipe._write.fileno()
        reader = pipe._read_transport.get_extra_info("pipe").fileno()
        argv = Path(f"/proc/{pipe.process.pid}/cmdline").read_bytes().split(b"\0")
        checks["private_pipe"] = (all(stat.S_ISFIFO(os.fstat(fd).st_mode) and os.fstat(fd).st_uid == 0
                and stat.S_IMODE(os.fstat(fd).st_mode) == 0o600 for fd in (writer, reader))
            and fcntl.fcntl(writer, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY
            and fcntl.fcntl(reader, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
            and b"--remote-debugging-pipe" in argv
            and not any(arg.startswith(b"--remote-debugging-port") for arg in argv)
            and b"--headless=new" in argv and pipe.live)
        checks["chromium_sandbox_enabled"] = b"--no-sandbox" not in argv
        if not all(checks[name] for name in WUYING_CHECKS[:12]):
            raise BrowserIsolationError("Wuying browser startup boundary verification failed")
        for prefix, account in (("browser", browser_account), ("executor", actor), ("legacy_executor", legacy)):
            paths = [(prefix + "_cannot_read_journal", str(journal.path), os.O_RDONLY),
                (prefix + "_cannot_write_journal", str(journal.path), os.O_WRONLY),
                (prefix + "_cannot_read_supervisor_environment", f"/proc/{os.getpid()}/environ", os.O_RDONLY),
                (prefix + "_cannot_open_control_pipe", f"/proc/{os.getpid()}/fd/{writer}", os.O_WRONLY)]
            if prefix == "browser":
                paths.append(("browser_cannot_read_actor_workspace", str(binding.workspace_dir), os.O_RDONLY))
            else:
                paths.append((prefix + "_cannot_read_profile", str(home), os.O_RDONLY))
            proof = await _identity_probe(account.pw_uid, account.pw_gid, paths, prefix + "_cannot_signal_supervisor")
            for name, _, _ in paths:
                checks[name] = proof.get(name) is True
            checks[prefix + "_cannot_signal_supervisor"] = proof.get(prefix + "_cannot_signal_supervisor") is True
        if not all(checks.values()):
            raise BrowserIsolationError("Wuying browser permission boundary verification failed")
    except BrowserIsolationError:
        raise
    except Exception as error:
        raise BrowserIsolationError("Wuying browser startup boundary unavailable") from error
    report["verification"] = "passed"
    report["executor_uid"] = actor.pw_uid
    report["legacy_executor_uid"] = legacy.pw_uid
    return report
