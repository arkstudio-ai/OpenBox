"""Fresh-process privilege boundary for untrusted Action Server children.

Use a separate isolated Python interpreter rather than preexec_fn in a
multithreaded ASGI server. Target-supplied loader/Python environment variables
are installed only after supplementary groups and privileges are dropped.
This does not certify filesystem API isolation or descendant quiescence.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys


PROTOCOL = "unprivileged_child_v1"
_PAYLOAD_ENV = "OPENBOX_CHILD_ENV_PAYLOAD"
_CONFIG_ENV = "OPENBOX_EXECUTOR_USER"


class IsolationError(RuntimeError):
    pass


def configured_user():
    return os.environ.get(_CONFIG_ENV, "")


def identity(name):
    if sys.platform != "linux":
        raise IsolationError("Configured child isolation requires Linux")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", name):
        raise IsolationError("Invalid execution user")
    try:
        account = pwd.getpwnam(name)
    except KeyError as exc:
        raise IsolationError("Configured execution user does not exist") from exc
    if account.pw_uid == 0 or account.pw_gid == 0:
        raise IsolationError("Execution identity must not be privileged")
    if os.geteuid() not in (0, account.pw_uid):
        raise IsolationError("Service cannot assume the configured execution identity")
    return account


def validate_configuration():
    name = configured_user()
    if name:
        # Prove the real kernel transition at startup as well as on every
        # dispatch. User lookup alone would advertise an unusable boundary.
        command, env = prepare_child([sys.executable, "-I", "-S", "-c", "pass"], {})
        try:
            result = subprocess.run(command, env=env, capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IsolationError("Execution identity probe failed") from exc
        if result.returncode != 0:
            raise IsolationError("Execution identity probe refused")


def prepare_child(argv, env, *, terminal=False):
    """Return argv/environment for a child; legacy images remain explicit."""
    name = configured_user()
    if not name:
        if terminal:
            raise IsolationError("Isolated terminal requires an execution identity")
        return list(argv), dict(env)
    identity(name)  # Invalid configuration never falls back to root execution.
    target = dict(env)
    if any(not isinstance(k, str) or not isinstance(v, str) or "\0" in k + v or "=" in k
           for k, v in target.items()):
        raise IsolationError("Invalid child environment")
    payload = json.dumps(target, ensure_ascii=True, separators=(",", ":"))
    if len(payload.encode()) > 64 * 1024:
        raise IsolationError("Child environment exceeds the isolation envelope")
    # Nothing selected by a plugin may affect the privileged interpreter's
    # loader, module path, site initialization or working-directory imports.
    launcher_env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "LANG": "C.UTF-8", _PAYLOAD_ENV: payload}
    command = [sys.executable, "-I", "-S", str(Path(__file__).resolve()), "--user", name]
    if terminal:
        command.append("--terminal")
    return [*command, "--", *argv], launcher_env


def assume_identity(account):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    # PR_SET_NO_NEW_PRIVS is irreversible and inherited through exec/fork.
    # sudo, setuid executables and file capabilities cannot regain authority.
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise IsolationError("Kernel refused no_new_privs")
    if os.geteuid() == 0:
        os.setgroups([])
        os.setresgid(account.pw_gid, account.pw_gid, account.pw_gid)
        os.setresuid(account.pw_uid, account.pw_uid, account.pw_uid)
    elif os.getgroups():
        # A non-root supervisor cannot remove inherited privileged groups.
        raise IsolationError("Cannot prove supplementary groups were dropped")
    if os.getresuid() != (account.pw_uid,) * 3 or os.getresgid() != (account.pw_gid,) * 3:
        raise IsolationError("Execution identity did not become permanent")
    if os.getgroups() or libc.prctl(39, 0, 0, 0, 0) != 1:
        raise IsolationError("Execution privilege boundary is unavailable")
    # Supervisors with unusual securebits/keepcaps settings must fail closed,
    # rather than leaving inheritable, permitted or ambient capabilities live.
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    if any(int(status[name].strip(), 16) for name in ("CapInh", "CapPrm", "CapEff", "CapAmb")):
        raise IsolationError("Execution capabilities were not cleared")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--terminal", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise IsolationError("An execution command is required")
    account = identity(args.user)
    target = json.loads(os.environ.get(_PAYLOAD_ENV, "{}"))
    if not isinstance(target, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in target.items()):
        raise IsolationError("Invalid child environment")
    for name in (_PAYLOAD_ENV, _CONFIG_ENV, "OPENBOX_RESOURCE_CONTROL_DB"):
        target.pop(name, None)
    target.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name)
    if args.terminal:
        # Popen(start_new_session=True) created a session without running
        # Python after fork in the threaded ASGI process. Attach its terminal
        # here, in the fresh worker, before permanently dropping privileges.
        import fcntl
        import termios
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    # Empty the launcher's copy before executing any target code. A loader
    # variable such as LD_PRELOAD is safe only after this privilege transition.
    assume_identity(account)
    os.environ.clear()
    os.environ.update(target)
    os.execvpe(command[0], command, target)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # The environment may contain caller-owned credentials. Report the
        # failure category, never its payload or arbitrary exception text.
        print(f"isolated executor refused: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(126)
