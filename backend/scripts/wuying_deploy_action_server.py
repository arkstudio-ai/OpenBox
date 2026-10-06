#!/usr/bin/env python3
"""Push the current action server and video skill to a WUYING desktop.

The full bootstrap installs a runtime, dev-browser and systemd units; when the
action server or video-production skill changes, re-running all
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
import base64
from datetime import datetime, timezone
import gzip
import hashlib
import json
import pathlib
import re
import shlex
import sys
from uuid import uuid4

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from wuying_bootstrap import Desktop  # noqa: E402  (path set above)

ACTION_SERVER = REPO / "container" / "action_server.py"
RESOURCE_GATE = REPO / "container" / "resource_gate.py"
EXECUTION_IDENTITY = REPO / "container" / "execution_identity.py"
FILE_WORKER = REPO / "container" / "file_worker.py"
STORAGE_MIGRATION = REPO / "container" / "storage_migration.py"
VIDEO_PRODUCTION_SKILL_DIR = (
    REPO / "backend" / ".openbox" / "skills" / "video-production"
)
REMOTE_PATH = "/opt/action_server/action_server.py"
REMOTE_VIDEO_PRODUCTION_SKILL_DIR = "/opt/openbox/skills/video-production"
SERVICE = "openbox-action-server"
ACTION_SERVER_MODULES = (
    "action_server.py", "resource_gate.py", "execution_identity.py", "file_worker.py", "storage_migration.py",
    "private_actor.py",
)


def read_env(key: str, env_file=None) -> str:
    """Read one key from backend/.env without importing the app config."""
    env_file = pathlib.Path(env_file) if env_file else REPO / "backend" / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


def action_server_bundle(source_dir=None):
    """Compile locally before creating any remote command; exclude all state."""
    source_dir = pathlib.Path(source_dir or REPO / "container")
    files = {}
    for name in ACTION_SERVER_MODULES:
        content = (source_dir / name).read_bytes()
        compile(content, name, "exec")
        files[name] = {"sha256": hashlib.sha256(content).hexdigest(),
                       "content": base64.b64encode(content).decode()}
    return files


def _python(script):
    return "python3 -I -S -c " + shlex.quote(script)


def retained_deployment_commands(files, release_id, *, remote_root="/opt/action_server"):
    """Idempotent bounded code publication, retaining chunks and old modules.

    Never uses Desktop.put (which removes its staging file), bootstrap, data
    migration, dependency installation, shell cleanup, or user/permission
    changes. It only replaces these exact system-owned Python modules after
    validating the full bundle and saving a copy of every original module.
    The optional root is for an isolated local transport fixture, not CLI input.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", release_id) or set(files) != set(ACTION_SERVER_MODULES):
        raise ValueError("Invalid fixed Action Server release")
    packed = base64.b64encode(gzip.compress(json.dumps(files, sort_keys=True).encode())).decode()
    chunks = [packed[index:index + 9000] for index in range(0, len(packed), 9000)]
    root = pathlib.PurePosixPath(remote_root)
    stage = root / "releases" / release_id
    prelude = f'''import os,pathlib,stat
root=pathlib.Path({str(root)!r})
stage=pathlib.Path({str(stage)!r})
def protected(path):
    for item in (path,*path.parents):
        meta=item.lstat()
        if stat.S_ISLNK(meta.st_mode) or meta.st_uid!=0 or meta.st_mode & 0o022:
            raise RuntimeError('Deployment path is not root protected')
if os.geteuid()!=0: raise RuntimeError('Deployment requires the existing root service authority')
protected(root)
root.joinpath('releases').mkdir(mode=0o700,exist_ok=True)
protected(root/'releases')
stage.mkdir(mode=0o700,exist_ok=True)
protected(stage)
'''
    commands = []
    for index, chunk in enumerate(chunks):
        commands.append(_python(prelude + f'''
path=stage/{('chunk_%04d' % index)!r}
data={chunk!r}.encode()
try:
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
except FileExistsError:
    protected(path)
    if path.read_bytes()!=data: raise RuntimeError('Existing release chunk differs')
else:
    with os.fdopen(fd,'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
print('retained chunk {index + 1}/{len(chunks)}')
'''))
    expected = {name: data["sha256"] for name, data in files.items()}
    commands.append(_python(prelude + f'''
import base64,gzip,hashlib,json,shutil
expected={expected!r}
blob=b''.join((stage/('chunk_%04d'%index)).read_bytes() for index in range({len(chunks)}))
bundle=json.loads(gzip.decompress(base64.b64decode(blob)))
if set(bundle)!=set(expected): raise RuntimeError('Release file set differs')
contents={{}}
for name, entry in bundle.items():
    data=base64.b64decode(entry['content'],validate=True)
    if hashlib.sha256(data).hexdigest()!=expected[name] or entry['sha256']!=expected[name]:
        raise RuntimeError('Release checksum differs')
    compile(data,name,'exec')
    contents[name]=data
backup=stage/'original'
backup.mkdir(mode=0o700,exist_ok=True)
protected(backup)
original_index=stage/'original-files.json'
if not original_index.exists():
    originals={{name:(root/name).exists() for name in contents}}
    with original_index.open('x') as stream:
        json.dump(originals,stream,sort_keys=True); stream.flush(); os.fsync(stream.fileno())
    original_index.chmod(0o600)
originals=json.loads(original_index.read_text())
if set(originals)!=set(contents): raise RuntimeError('Original file set differs')
for name in contents:
    target=root/name
    saved=backup/name
    if target.exists() or target.is_symlink():
        protected(target)
        if not target.is_file(): raise RuntimeError('Target is not a regular module')
        if originals[name] and not saved.exists():
            with saved.open('xb') as stream:
                stream.write(target.read_bytes()); stream.flush(); os.fsync(stream.fileno())
            saved.chmod(0o600)
for name,data in contents.items():
    target=root/name
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==expected[name]: continue
    staged=stage/('published-'+name)
    if staged.exists():
        protected(staged)
        if staged.read_bytes()!=data: raise RuntimeError('Published source differs')
    else:
        with staged.open('xb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        staged.chmod(0o644)
    # Retain the staged source and original copy; this is the authorized code
    # replacement, never a data-directory or permission migration.
    with target.open('wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    target.chmod(0o644)
for name,digest in expected.items():
    if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:
        raise RuntimeError('Installed checksum differs')
manifest=stage/'manifest.json'
data=json.dumps({{'release_id':{release_id!r},'files':expected}},sort_keys=True).encode()
if manifest.exists() and manifest.read_bytes()!=data: raise RuntimeError('Release manifest differs')
if not manifest.exists():
    with manifest.open('xb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    manifest.chmod(0o600)
print(json.dumps({{'release_id':{release_id!r},'verified_modules':len(expected),'retained_originals':str(backup)}}))
'''))
    return commands


def deploy_action_server_only(desktop, *, release_id=None, no_restart=False, source_dir=None):
    files = action_server_bundle(source_dir)
    release_id = release_id or datetime.now(timezone.utc).strftime("as_%Y%m%dT%H%M%SZ_") + uuid4().hex[:10]
    for command in retained_deployment_commands(files, release_id):
        # Each chunk and publication step is exact-content idempotent. Keep
        # any uncertain invocation for read-only inspection, not cleanup.
        desktop.run(command, timeout=120)
    print(json.dumps({"release_id": release_id, "files": {name: value["sha256"] for name, value in files.items()}}))
    if not no_restart:
        print(desktop.run(f"systemctl restart {SERVICE} && systemctl is-active {SERVICE}", timeout=120).strip())
    return release_id


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--desktop-id", default="", help="ecd-… (default: WUYING_DESKTOP_ID from backend/.env)")
    ap.add_argument("--region", default="", help="default: WUYING_REGION_ID from backend/.env, else cn-hangzhou")
    ap.add_argument("--no-restart", action="store_true", help="upload only, leave the running service alone")
    ap.add_argument("--skip-media-tools", action="store_true", help="do not install/check ffmpeg and CJK fonts")
    ap.add_argument("--env-file", default="", help="explicit local configuration file")
    ap.add_argument("--action-server-only", action="store_true", help="retain source/backups; update only the fixed Python service modules")
    ap.add_argument("--release-id", default="", help="reuse an exact retained release after read-only outcome inspection")
    ap.add_argument("--dry-run", action="store_true", help="print only the bounded code plan and hashes; no remote calls")
    args = ap.parse_args()

    desktop_id = args.desktop_id or read_env("WUYING_DESKTOP_ID", args.env_file)
    region = args.region or read_env("WUYING_REGION_ID", args.env_file) or "cn-hangzhou"
    if not desktop_id:
        print("error: no desktop id (pass --desktop-id or set WUYING_DESKTOP_ID in backend/.env)", file=sys.stderr)
        return 2
    if args.action_server_only:
        files = action_server_bundle()
        if args.dry_run:
            print(json.dumps({"desktop_id": desktop_id, "region_id": region, "service": SERVICE,
                "restart": not args.no_restart, "data_paths_modified": [],
                "files": {name: value["sha256"] for name, value in files.items()}}, sort_keys=True))
            return 0
        deploy_action_server_only(Desktop(desktop_id, region), release_id=args.release_id or None,
                                  no_restart=args.no_restart)
        return 0
    if args.dry_run or args.release_id:
        ap.error("--dry-run/--release-id require --action-server-only")
    if not ACTION_SERVER.exists():
        print(f"error: {ACTION_SERVER} not found", file=sys.stderr)
        return 2
    if not RESOURCE_GATE.exists():
        print(f"error: {RESOURCE_GATE} not found", file=sys.stderr)
        return 2
    if not EXECUTION_IDENTITY.exists():
        print(f"error: {EXECUTION_IDENTITY} not found", file=sys.stderr)
        return 2
    if not FILE_WORKER.exists():
        print(f"error: {FILE_WORKER} not found", file=sys.stderr)
        return 2
    if not STORAGE_MIGRATION.exists():
        print(f"error: {STORAGE_MIGRATION} not found", file=sys.stderr)
        return 2
    if not (VIDEO_PRODUCTION_SKILL_DIR / "SKILL.md").exists():
        print(f"error: {VIDEO_PRODUCTION_SKILL_DIR / 'SKILL.md'} not found", file=sys.stderr)
        return 2

    d = Desktop(desktop_id, region)
    print(f"deploying {ACTION_SERVER.name} -> {desktop_id} ({region})")

    # Syntax-check before the restart rather than after: a SyntaxError here
    # leaves the desktop with a service that will not come back up.
    d.put(RESOURCE_GATE, "/opt/action_server/resource_gate.py")
    d.put(EXECUTION_IDENTITY, "/opt/action_server/execution_identity.py")
    d.put(FILE_WORKER, "/opt/action_server/file_worker.py")
    d.put(STORAGE_MIGRATION, "/opt/action_server/storage_migration.py")
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
python3 -m py_compile {REMOTE_PATH} /opt/action_server/resource_gate.py /opt/action_server/execution_identity.py /opt/action_server/file_worker.py /opt/action_server/storage_migration.py

echo 'compile ok'
""",
        timeout=120,
    )
    print("  remote syntax check passed")

    if not args.skip_media_tools:
        # Composition is the agent running ffmpeg now, so the desktop needs
        # the binary and CJK fonts and nothing else. The pinned HyperFrames /
        # GSAP / Chrome bundle went with the media worker.
        print("  checking ffmpeg and CJK fonts")
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
