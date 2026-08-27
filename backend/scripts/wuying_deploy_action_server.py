#!/usr/bin/env python3
"""Push the current action server and video skill to a WUYING desktop.

The full bootstrap installs a runtime, dev-browser and systemd units; when the
action server, media queue, or video-production skill changes, re-running all
of that is minutes of unnecessary work. This deploys those small system-owned
artifacts plus the restart, reusing the bootstrap's Desktop primitives so the
upload path stays identical. The skill contains instructions only; provider
credentials remain in the backend environment.

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
from wuying_media_runtime import ensure_local_media_runtime  # noqa: E402

ACTION_SERVER = REPO / "container" / "action_server.py"
MEDIA_JOBS = REPO / "container" / "media_jobs.py"
MEDIA_CONFIG = REPO / "container" / "media-jobs.json"
MEDIA_PACKAGE = REPO / "container" / "media-runtime" / "package.json"
MEDIA_LOCK = REPO / "container" / "media-runtime" / "package-lock.json"
VIDEO_PRODUCTION_SKILL = (
    REPO / "backend" / ".openbox" / "skills" / "video-production" / "SKILL.md"
)
REMOTE_PATH = "/opt/action_server/action_server.py"
REMOTE_VIDEO_PRODUCTION_SKILL = "/opt/openbox/skills/video-production/SKILL.md"
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
    ap.add_argument("--skip-media-runtime", action="store_true", help="do not install/check FFmpeg, fonts, HyperFrames or GSAP")
    ap.add_argument(
        "--force-media-bundle",
        action="store_true",
        help="rebuild the pinned linux/amd64 runtime locally and replace the remote copy",
    )
    args = ap.parse_args()

    desktop_id = args.desktop_id or read_env("WUYING_DESKTOP_ID")
    region = args.region or read_env("WUYING_REGION_ID") or "cn-hangzhou"
    if not desktop_id:
        print("error: no desktop id (pass --desktop-id or set WUYING_DESKTOP_ID in backend/.env)", file=sys.stderr)
        return 2
    if not ACTION_SERVER.exists():
        print(f"error: {ACTION_SERVER} not found", file=sys.stderr)
        return 2
    if not VIDEO_PRODUCTION_SKILL.exists():
        print(f"error: {VIDEO_PRODUCTION_SKILL} not found", file=sys.stderr)
        return 2

    d = Desktop(desktop_id, region)
    print(f"deploying {ACTION_SERVER.name} -> {desktop_id} ({region})")

    # Syntax-check before the restart rather than after: a SyntaxError here
    # leaves the desktop with a service that will not come back up.
    d.put(ACTION_SERVER, REMOTE_PATH)
    d.put(MEDIA_JOBS, "/opt/action_server/media_jobs.py")
    d.put(MEDIA_CONFIG, "/opt/openbox/media/media-jobs.json")
    d.put(MEDIA_PACKAGE, "/opt/openbox/media/package.json")
    d.put(VIDEO_PRODUCTION_SKILL, REMOTE_VIDEO_PRODUCTION_SKILL)
    if MEDIA_LOCK.exists():
        d.put(MEDIA_LOCK, "/opt/openbox/media/package-lock.json")
    d.run(
        f"python3 -m py_compile {REMOTE_PATH} /opt/action_server/media_jobs.py && echo 'compile ok'",
        timeout=120,
    )
    print("  remote syntax check passed")

    if not args.skip_media_runtime:
        print("  checking pinned media runtime")
        d.run(r"""
set -e
export DEBIAN_FRONTEND=noninteractive PATH=/usr/local/bin:$PATH
if ! command -v ffmpeg >/dev/null 2>&1 || ! fc-list :lang=zh | grep -q .; then
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends ffmpeg fonts-noto-cjk fontconfig
  rm -rf /var/lib/apt/lists/*
fi
mkdir -p /opt/openbox/media /data/openbox-media /tmp/openbox-media/jobs /tmp/openbox-media/cache
""", timeout=1800)
        ensure_local_media_runtime(d, force=args.force_media_bundle)
        d.run(r"""
set -e
cd /opt/openbox/media
node_modules/.bin/hyperframes telemetry disable >/dev/null 2>&1 || true
mkdir -p /etc/systemd/system/openbox-action-server.service.d
cat > /etc/systemd/system/openbox-action-server.service.d/media.conf <<'EOF'
[Service]
Environment=MEDIA_JOBS_CONFIG=/opt/openbox/media/media-jobs.json
Environment=HYPERFRAMES_BROWSER_PATH=/usr/bin/google-chrome
MemoryHigh=5G
MemoryMax=6G
TasksMax=512
EOF
systemctl daemon-reload
test -x node_modules/.bin/hyperframes
test -f node_modules/gsap/dist/gsap.min.js
command -v ffmpeg
command -v ffprobe
test -x /usr/bin/google-chrome
echo 'media runtime ok'
""", timeout=1800)

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
