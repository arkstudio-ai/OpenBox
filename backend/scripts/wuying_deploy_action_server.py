#!/usr/bin/env python3
"""Push the current action server and video skill to a WUYING desktop.

The full bootstrap installs a runtime, dev-browser and systemd units; when the
action server or video-production skill changes, re-running all
of that is minutes of unnecessary work. This deploys those small system-owned
artifacts plus the restart, reusing the bootstrap's Desktop primitives so the
upload path stays identical. The skill contains instructions only; provider
credentials remain in the backend environment.

    python backend/scripts/wuying_deploy_action_server.py   # process env / OPENBOX_ENV_FILE / dev profile
    python backend/scripts/wuying_deploy_action_server.py --desktop-id ecd-xxx

Verifies the service came back up and reports the version /alive returns, so a
deploy that silently left the old code running is visible here rather than a
puzzle later.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import pathlib
import re
import shlex
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "backend"))

from sandbox.browser import browser_policy_provision_script  # noqa: E402
from sandbox.assets import asset_cli_provision_script  # noqa: E402
from sandbox.desktop import desktop_provision_script  # noqa: E402
from sandbox.protocol import REQUIRED_ACTION_SERVER_CAPABILITIES  # noqa: E402
from wuying_bootstrap import Desktop  # noqa: E402  (path set above)
from wuying_env import environment_value  # noqa: E402

ACTION_SERVER = REPO / "container" / "action_server.py"
VIDEO_PRODUCTION_SKILL_DIR = (
    REPO / "backend" / ".openbox" / "skills" / "video-production"
)
REMOTE_PATH = "/opt/action_server/action_server.py"
REMOTE_VIDEO_PRODUCTION_SKILL_DIR = "/opt/openbox/skills/video-production"
SERVICE = "openbox-action-server"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--desktop-id", default="", help="ecd-… (default: WUYING_DESKTOP_ID from backend env profile)")
    ap.add_argument("--region", default="", help="default: WUYING_REGION_ID from backend env profile, else cn-hangzhou")
    ap.add_argument("--no-restart", action="store_true", help="upload only, leave the running service alone")
    ap.add_argument("--skip-media-tools", action="store_true", help="do not install/check ffmpeg and CJK fonts")
    args = ap.parse_args()

    configured_desktop_id = environment_value("WUYING_DESKTOP_ID")
    if (
        args.desktop_id
        and configured_desktop_id
        and args.desktop_id != configured_desktop_id
    ):
        print(
            "error: --desktop-id does not match WUYING_DESKTOP_ID in the "
            "selected backend env profile; set OPENBOX_ENV_FILE to the "
            "target desktop profile",
            file=sys.stderr,
        )
        return 2
    desktop_id = args.desktop_id or configured_desktop_id
    region = args.region or environment_value("WUYING_REGION_ID") or "cn-hangzhou"
    api_key = environment_value("WUYING_API_KEY")
    if not desktop_id:
        print("error: no desktop id (pass --desktop-id or set WUYING_DESKTOP_ID in a backend env profile)", file=sys.stderr)
        return 2
    if not api_key:
        print("error: WUYING_API_KEY is missing from the selected backend env profile", file=sys.stderr)
        return 2
    if any(character in api_key for character in "\r\n\0"):
        print("error: WUYING_API_KEY contains an invalid control character", file=sys.stderr)
        return 2
    if not ACTION_SERVER.exists():
        print(f"error: {ACTION_SERVER} not found", file=sys.stderr)
        return 2
    if not (VIDEO_PRODUCTION_SKILL_DIR / "SKILL.md").exists():
        print(f"error: {VIDEO_PRODUCTION_SKILL_DIR / 'SKILL.md'} not found", file=sys.stderr)
        return 2

    d = Desktop(desktop_id, region)
    print(f"deploying {ACTION_SERVER.name} -> {desktop_id} ({region})")

    # Syntax-check before the restart rather than after: a SyntaxError here
    # leaves the desktop with a service that will not come back up.
    d.put(ACTION_SERVER, REMOTE_PATH)
    skill_files = sorted(path for path in VIDEO_PRODUCTION_SKILL_DIR.rglob("*") if path.is_file())
    remote_dirs = sorted(
        {
            str(pathlib.PurePosixPath(REMOTE_VIDEO_PRODUCTION_SKILL_DIR) / path.relative_to(VIDEO_PRODUCTION_SKILL_DIR).parent)
            for path in skill_files
        }
    )
    d.run("mkdir -p " + " ".join(shlex.quote(path) for path in remote_dirs), timeout=120)
    for local_path in skill_files:
        relative = local_path.relative_to(VIDEO_PRODUCTION_SKILL_DIR)
        remote_path = str(pathlib.PurePosixPath(REMOTE_VIDEO_PRODUCTION_SKILL_DIR) / relative)
        d.put(local_path, remote_path)
    d.run(
        f"""
set -e
python3 -m py_compile {REMOTE_PATH}

echo 'compile ok'
""",
        timeout=120,
    )
    print("  remote syntax check passed")

    # User-controlled commands intentionally cannot apt-install packages or
    # write system policy. Repair the desktop/browser component layer through
    # ECD RunCommand (root) as part of every incremental deploy instead.
    d.run(desktop_provision_script(), timeout=900)
    d.run(asset_cli_provision_script(), timeout=120)
    d.run(browser_policy_provision_script(), timeout=120)
    print("  desktop tools and browser policy provisioned")

    # The API-facing service keeps its key as root, while every user-controlled
    # command, PTY, dev-browser relay and stdio MCP process runs as this account.
    # Re-applying the drop-in makes an incremental deploy as safe as a fresh
    # bootstrap and upgrades older desktops in place.
    credential_file = base64.b64encode(
        f"SESSION_API_KEY={shlex.quote(api_key)}\n".encode()
    ).decode()
    d.run(r"""
set -e
command -v setpriv >/dev/null 2>&1 || {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends util-linux
  rm -rf /var/lib/apt/lists/*
}
if ! id -u sandbox >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /workspace/.openbox-home --shell /bin/bash sandbox
fi
mkdir -p /workspace /data/skills /data/mcp /workspace/.openbox-home
chown -R sandbox:sandbox /workspace
chown root:root /data/skills
chmod 0711 /data/skills
chmod 0700 /data/mcp
mkdir -p /etc/systemd/system/openbox-action-server.service.d
cat > /etc/systemd/system/openbox-action-server.service.d/runner-isolation.conf <<'EOF'
[Service]
Environment=OPENBOX_RUNNER_USER=sandbox
Environment=OPENBOX_REQUIRE_RUNNER=1
Environment=OPENBOX_REQUIRE_USER_SCOPE=1
Environment=OPENBOX_WORKSPACE_ROOT=/workspace
Environment=OPENBOX_RUNNER_HOME=/workspace/.openbox-home
UMask=0027
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectHostname=true
ProtectClock=true
ProtectProc=invisible
RestrictSUIDSGID=true
LockPersonality=true
RestrictRealtime=true
CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_CHOWN CAP_KILL CAP_DAC_OVERRIDE
ReadWritePaths=/workspace /data /tmp /var/tmp
EOF
""" + f"""
install -d -m 0755 /etc/openbox
printf '%s' {shlex.quote(credential_file)} | base64 -d > /etc/openbox/action-server.env
chmod 0600 /etc/openbox/action-server.env
chown root:root /etc/openbox/action-server.env
cat > /etc/systemd/system/openbox-action-server.service.d/credentials.conf <<'EOF'
[Service]
Environment=SESSION_API_KEY=
EnvironmentFile=/etc/openbox/action-server.env
EOF
""" + r"""
systemctl daemon-reload
""", timeout=900)
    print("  isolated sandbox runner and protected API credential configured")

    if not args.skip_media_tools:
        print("  checking pinned media runtime")
        d.run(r"""
set -e
export DEBIAN_FRONTEND=noninteractive PATH=/usr/local/bin:$PATH
if ! command -v ffmpeg >/dev/null 2>&1 || ! fc-list :lang=zh | grep -q .; then
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends ffmpeg fonts-noto-cjk fontconfig
  rm -rf /var/lib/apt/lists/*
fi
command -v ffmpeg
command -v ffprobe
fc-list :lang=zh | head -1
echo 'media tools ok'
""", timeout=1800)

    if args.no_restart:
        print("  --no-restart: leaving the running service as-is")
        return 0

    source = ACTION_SERVER.read_bytes()
    version_match = re.search(
        rb'^ACTION_SERVER_VERSION\s*=\s*"([^"]+)"', source, re.MULTILINE
    )
    if not version_match:
        print("error: cannot determine local Action Server version", file=sys.stderr)
        return 2
    expected_version = version_match.group(1).decode()
    expected_sha256 = hashlib.sha256(source).hexdigest()
    required_capabilities = sorted(REQUIRED_ACTION_SERVER_CAPABILITIES)
    verify_program = (
        "import json,sys; d=json.load(sys.stdin); "
        f"assert d.get('version') == {expected_version!r}; "
        f"assert set({required_capabilities!r}) <= "
        "set(d.get('capabilities', []))"
    )
    out = d.run(
        f"set -e; "
        f"test \"$(sha256sum {REMOTE_PATH} | awk '{{print $1}}')\" = {shlex.quote(expected_sha256)}; "
        f"systemctl restart {SERVICE}; sleep 3; "
        f"systemctl is-active {SERVICE}; "
        f"health=$(curl -fsS --max-time 5 http://127.0.0.1:8000/alive) || "
        f"(journalctl -u {SERVICE} -n 40 --no-pager; exit 1); "
        f"printf '%s' \"$health\" | python3 -c {shlex.quote(verify_program)}; "
        f"printf '%s\\n' \"$health\"",
        timeout=180,
    )
    print("  " + out.strip().replace("\n", "\n  "))
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
