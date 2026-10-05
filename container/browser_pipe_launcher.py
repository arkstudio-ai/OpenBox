"""Fresh-process launcher for a single private Chromium pipe.

The supervisor never forks Python inside a threaded server or exports its
environment/service key. No caller-controlled flags or arbitrary executable
are accepted over the browser resource API.
"""
import argparse
import ctypes
import fcntl
import os
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--read-fd", required=True, type=int)
    parser.add_argument("--write-fd", required=True, type=int)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    parser.add_argument("--disable-inner-sandbox", action="store_true")
    args = parser.parse_args()
    # Duplicate both ends first: neither original descriptor may be clobbered
    # when Chromium's fixed descriptor 3/4 pair is installed.
    read_fd = fcntl.fcntl(args.read_fd, fcntl.F_DUPFD_CLOEXEC, 10)
    write_fd = fcntl.fcntl(args.write_fd, fcntl.F_DUPFD_CLOEXEC, 10)
    os.dup2(read_fd, 3, inheritable=True)
    os.dup2(write_fd, 4, inheritable=True)
    os.set_inheritable(3, True)
    os.set_inheritable(4, True)
    os.closerange(5, min(os.sysconf("SC_OPEN_MAX"), 1048576))
    if args.uid is not None:
        if args.gid is None or args.uid == 0:
            raise ValueError("A distinct unprivileged browser identity is required")
        os.setgroups([])
        os.setgid(args.gid)
        os.setuid(args.uid)
    profile = Path(args.profile)
    if profile.is_symlink():
        raise ValueError("Symlinked browser profiles are forbidden")
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.platform.startswith("linux"):
        if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "no_new_privs failed")
    home = os.path.dirname(args.profile)
    # Chromium's SingletonSocket uses TMPDIR and must fit Unix sockaddr_un.
    # This fresh 0700 directory contains no shared workspace/profile data.
    temporary = tempfile.mkdtemp(prefix="obx-browser-", dir="/tmp")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": home,
           "XDG_CONFIG_HOME": home, "XDG_CACHE_HOME": home, "TMPDIR": temporary}
    argv = [args.binary, "--headless=new", "--remote-debugging-pipe",
            "--user-data-dir=" + args.profile, "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-component-update", "--disable-sync",
            "--disable-extensions", "--disable-default-apps", "--disable-breakpad",
            "--metrics-recording-only", "--password-store=basic", "--window-size=1024,768",
            "about:blank"]
    if args.disable_inner_sandbox:
        # Only explicit container_uid (with startup proof), or a diagnostic
        # fixture, requests this. The HTTP service reports the exact mode.
        argv.append("--no-sandbox")
    os.execve(args.binary, argv, env)


if __name__ == "__main__":
    main()
