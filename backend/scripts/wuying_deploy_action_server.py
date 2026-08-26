#!/usr/bin/env python3
"""Push the current action_server.py to a WUYING desktop and restart it.

The full bootstrap installs a runtime, dev-browser and systemd units; when only
container/action_server.py has changed, re-running all of that is minutes of
work to replace one file. This does just that file plus the restart, reusing
the bootstrap's Desktop primitives so the upload path stays identical.

    python backend/scripts/wuying_deploy_action_server.py            # reads backend/.env
    python backend/scripts/wuying_deploy_action_server.py --desktop-id ecd-xxx

Verifies the service came back up and reports the version /alive returns, so a
deploy that silently left the old code running is visible here rather than a
puzzle later.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from wuying_bootstrap import Desktop  # noqa: E402  (path set above)

ACTION_SERVER = REPO / "container" / "action_server.py"
REMOTE_PATH = "/opt/action_server/action_server.py"
SERVICE = "openbox-action-server"


def read_env(key: str) -> str:
    """Read one key from backend/.env without importing the app config."""
    env_file = REPO / "backend" / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--desktop-id", default="", help="ecd-… (default: WUYING_DESKTOP_ID from backend/.env)")
    ap.add_argument("--region", default="", help="default: WUYING_REGION_ID from backend/.env, else cn-hangzhou")
    ap.add_argument("--no-restart", action="store_true", help="upload only, leave the running service alone")
    args = ap.parse_args()

    desktop_id = args.desktop_id or read_env("WUYING_DESKTOP_ID")
    region = args.region or read_env("WUYING_REGION_ID") or "cn-hangzhou"
    if not desktop_id:
        print("error: no desktop id (pass --desktop-id or set WUYING_DESKTOP_ID in backend/.env)", file=sys.stderr)
        return 2
    if not ACTION_SERVER.exists():
        print(f"error: {ACTION_SERVER} not found", file=sys.stderr)
        return 2

    d = Desktop(desktop_id, region)
    print(f"deploying {ACTION_SERVER.name} -> {desktop_id} ({region})")

    # Syntax-check before the restart rather than after: a SyntaxError here
    # leaves the desktop with a service that will not come back up.
    d.put(ACTION_SERVER, REMOTE_PATH)
    d.run(f"python3 -m py_compile {REMOTE_PATH} && echo 'compile ok'", timeout=120)
    print("  remote syntax check passed")

    if args.no_restart:
        print("  --no-restart: leaving the running service as-is")
        return 0

    out = d.run(
        f"systemctl restart {SERVICE} && sleep 3 && "
        f"systemctl is-active {SERVICE} && "
        f"curl -s --max-time 5 http://127.0.0.1:8000/alive || "
        f"(journalctl -u {SERVICE} -n 40 --no-pager; exit 1)",
        timeout=180,
    )
    print("  " + out.strip().replace("\n", "\n  "))
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
