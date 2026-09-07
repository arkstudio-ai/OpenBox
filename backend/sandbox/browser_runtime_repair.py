#!/usr/bin/env python3
"""Repair the OpenBox browser runtime on reused Wuying images, without restarting.

Run as root on the desktop. Existing Chrome profiles and IBus settings are never
modified. Backups stay on that desktop; no browser data is sent to the API host.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


RUNTIME_VERSION = "20260907.2"
SKILL_DIR = Path("/opt/openbox/skills/dev-browser")
LOCK_FILE = Path("/opt/openbox/tools/dev-browser-package-lock.json")
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
ACTION_SERVICE = "openbox-action-server.service"
ACTION_TASKS_MIN = 2048
SYSTEMD_ROOT = Path("/etc/systemd/system")
SYSTEMD_CONTROL_ROOT = Path("/etc/systemd/system.control")
ACTION_CGROUP = Path("/sys/fs/cgroup/system.slice/openbox-action-server.service")
SERVICE = """[Unit]
Description=Verify and repair the OpenBox browser runtime
After=network-online.target
Wants=network-online.target
Before=openbox-action-server.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/openbox/tools/repair_browser_runtime.py --install-deps
TimeoutStartSec=360
RemainAfterExit=yes
UMask=0022

[Install]
WantedBy=multi-user.target
"""


GATE_MARKER = "# OpenBox: allow only the current user's dedicated automation profile."
GATE_ANCHOR = '[[ "${BOSSIP_CHROME_ALLOW_PROFILE:-}" == "1" ]] && bossip_allow=1'
GATE_ALLOW = r'''
# OpenBox: allow only the current user's dedicated automation profile.
# Keep the legacy BossIP gate for every other caller/profile. Never grant
# access to a worker's profile or make another user's directory writable.
if [[ "${bossip_requested}" == "${HOME}/.config/obx-chrome" ]] \
   && [[ "$(stat -c %u "$HOME")" == "$(id -u)" ]] \
   && [[ ! -L "${bossip_requested}" ]] \
   && { [[ ! -e "${bossip_requested}" ]] \
        || [[ "$(stat -c %u "${bossip_requested}")" == "$(id -u)" ]]; }; then
  bossip_allow=1
fi
'''


def patched_chrome_gate(source: str) -> str:
    if GATE_MARKER in source:
        return source
    if "BOSSIP_CHROME_GATE" not in source:
        return source  # Stock Chrome needs no wrapper modification.
    if source.count(GATE_ANCHOR) != 1:
        raise RuntimeError("Unrecognized BossIP Chrome gate; refusing to rewrite it")
    return source.replace(GATE_ANCHOR, GATE_ANCHOR + "\n" + GATE_ALLOW, 1)


def ensure_node_commands(bin_dir: Path, runtime_dir: Path) -> list[Path]:
    """Expose an image's existing private Node runtime; never replace commands."""
    missing = [name for name in ("node", "npm", "npx") if not shutil.which(name)]
    # Validate every source and destination before creating any links.
    for name in missing:
        source, destination = runtime_dir / name, bin_dir / name
        if not source.is_file() or not os.access(source, os.X_OK):
            raise RuntimeError(f"Missing executable {source}; install Node LTS first")
        if destination.exists() or destination.is_symlink():
            raise RuntimeError(f"Refusing to replace existing command {destination}")
    created = []
    for name in missing:
        destination = bin_dir / name
        destination.symlink_to(runtime_dir / name)
        created.append(destination)
    return created


def repair_gate(path: Path, backup: Path) -> bool:
    path = path.resolve(strict=True)
    source = path.read_text()
    updated = patched_chrome_gate(source)
    if source == updated:
        return False
    if path.stat().st_uid != 0:
        raise RuntimeError("Chrome launcher is not root-owned")
    saved = backup / "google-chrome.before"
    shutil.copy2(path, saved)
    descriptor, staging_name = tempfile.mkstemp(prefix=".openbox-chrome-", dir=path.parent)
    staging = Path(staging_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(updated)
        os.chmod(staging, path.stat().st_mode & 0o777)
        subprocess.run(["bash", "-n", str(staging)], check=True)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    return True


def dependency_problems(skill_dir: Path, lock_file: Path) -> list[str]:
    """Check executable code, not a stale success marker or just node_modules."""
    try:
        locked = json.loads(lock_file.read_text())["packages"]
        package = json.loads((skill_dir / "package.json").read_text())
        if package.get("dependencies") != locked[""].get("dependencies"):
            return ["dev-browser package does not match the pinned dependency manifest"]
        for name in locked[""].get("dependencies", {}):
            installed = json.loads((skill_dir / "node_modules" / name / "package.json").read_text())
            if installed["version"] != locked[f"node_modules/{name}"]["version"]:
                return [f"dev-browser dependency version mismatch: {name}"]
        env = {"PATH": SAFE_PATH, "HOME": "/tmp", "CI": "true"}
        for command in (
            ["node", "node_modules/tsx/dist/cli.mjs", "--eval",
             "const value: number = 1; if (value !== 1) throw new Error('tsx');"],
            ["node", "--input-type=module", "-e",
             "await import('playwright'); await import('hono'); await import('@hono/node-ws');"],
        ):
            subprocess.run(command, cwd=skill_dir, env=env, capture_output=True,
                           check=True, timeout=15)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return [f"dev-browser dependencies unavailable ({type(exc).__name__})"]
    return []


def sufficient_task_limit(value: str) -> bool:
    """TasksMax counts Chrome/IBus/Node threads as well as processes."""
    return value in ("max", "infinity") or (value.isdigit() and int(value) >= ACTION_TASKS_MIN)


def task_budget_problems(systemd_root: Path, cgroup: Path) -> list[str]:
    """Read-only check also works in legacy containers without systemd D-Bus."""
    resource_file = systemd_root / (ACTION_SERVICE + ".d/browser-resources.conf")
    try:
        limits = [line.split("=", 1)[1].strip() for line in resource_file.read_text().splitlines()
                  if line.startswith("TasksMax=")]
        if not limits or not sufficient_task_limit(limits[-1]):
            return ["browser service task budget is below the supported minimum"]
        # Before first boot the action cgroup does not exist yet. In a legacy
        # container it may not be visible; verify the persistent config there.
        live_limit = cgroup / "pids.max"
        if live_limit.exists() and not sufficient_task_limit(live_limit.read_text().strip()):
            return ["live browser service task budget is below the supported minimum"]
    except OSError:
        return ["browser service task budget is not configured"]
    return []


def runtime_problems(skill_dir: Path, lock_file: Path, launcher: Path) -> list[str]:
    problems = []
    for name in ("node", "npm", "npx"):
        if not shutil.which(name):
            problems.append(f"missing command: {name}")
    if not problems:
        try:
            version = subprocess.run(["node", "--version"], capture_output=True,
                                     text=True, check=True, timeout=10).stdout.strip()
            if int(version.lstrip("v").split(".")[0]) < 20:
                problems.append("Node.js 20 or newer is required")
        except (OSError, ValueError, subprocess.SubprocessError):
            problems.append("Node.js cannot execute")
    try:
        source = launcher.read_text()
        if patched_chrome_gate(source) != source:
            problems.append("legacy Chrome gate blocks the OpenBox profile")
        if not os.access(launcher, os.X_OK):
            problems.append("Chrome launcher is not executable")
    except (OSError, UnicodeError, RuntimeError):
        problems.append("Chrome launcher is missing or its profile gate is unrecognized")
    for relative in ("scripts/start-relay.ts", "src/client.ts", "src/relay.ts", "tsconfig.json"):
        if not (skill_dir / relative).is_file():
            problems.append(f"missing dev-browser source: {relative}")
    return (problems + dependency_problems(skill_dir, lock_file)
            + task_budget_problems(SYSTEMD_ROOT, ACTION_CGROUP))


def install_dependencies(skill_dir: Path, backup: Path, registry: str,
                         lock_file: Path | None = None) -> None:
    """Install in staging and promote only after the local TS runtime works."""
    staging = Path(tempfile.mkdtemp(prefix=".dev-browser-deps-", dir=skill_dir.parent))
    print(f"dependency_staging={staging}", flush=True)
    shutil.copy2(skill_dir / "package.json", staging / "package.json")
    lock_file = lock_file or skill_dir / "package-lock.json"
    locked = lock_file.exists()
    if not locked:
        raise RuntimeError("Pinned package-lock.json is required; refusing an unpinned install")
    manifest = json.loads(lock_file.read_text())["packages"][""]
    if json.loads((skill_dir / "package.json").read_text()).get("dependencies") != manifest.get("dependencies"):
        raise RuntimeError("dev-browser package does not match the pinned dependency manifest")
    if locked:
        shutil.copy2(lock_file, staging / "package-lock.json")
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(staging),
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1", "CI": "true",
        "npm_config_cache": str(staging / ".npm-cache"),
    }
    with (backup / "npm-install.log").open("w") as log:
        subprocess.run(
            ["npm", "ci", "--omit=dev", "--ignore-scripts",
             "--no-audit", "--no-fund", "--userconfig=/dev/null",
             f"--globalconfig={staging / 'empty-global-npmrc'}", f"--registry={registry}"],
            cwd=staging, env=env, stdout=log, stderr=subprocess.STDOUT,
            check=True, timeout=240,
        )
    problems = dependency_problems(staging, lock_file)
    if problems:
        raise RuntimeError("; ".join(problems))
    destination = skill_dir / "node_modules"
    if destination.is_symlink():
        raise RuntimeError("Refusing to replace symlinked node_modules")
    lock = skill_dir / "package-lock.json"
    if lock.exists():
        shutil.copy2(lock, backup / "package-lock.before.json")
    old_modules = backup / "node_modules.before"
    if destination.exists():
        destination.rename(old_modules)
    try:
        (staging / "node_modules").rename(destination)
        shutil.copy2(staging / "package-lock.json", lock)
    except BaseException:
        if destination.exists():
            destination.rename(staging / "node_modules.failed")
        if old_modules.exists():
            old_modules.rename(destination)
        if (backup / "package-lock.before.json").exists():
            shutil.copy2(backup / "package-lock.before.json", lock)
        raise
    # Only this newly-created staging directory is removed. Old modules/logs
    # remain in the private rollback directory; never touch browser profiles.
    shutil.rmtree(staging)


@contextmanager
def runtime_lock(path: Path, timeout: float = 290):
    """Serialize repairs across API workers, boot services, and maintenance."""
    with path.open("a") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another browser runtime repair is still running")
                time.sleep(0.25)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def register_boot_service() -> bool:
    """Enable future-boot verification without restarting a live user session."""
    result = subprocess.run(
        ["systemctl", "show", ACTION_SERVICE, "-p", "LoadState", "-p", "MainPID", "-p", "TasksMax"],
        capture_output=True, text=True, timeout=20,
    )
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if properties.get("LoadState") not in ("loaded", "not-found"):
        raise RuntimeError("Cannot inspect the action service task budget")
    current = properties.get("TasksMax", "0") if properties["LoadState"] == "loaded" else "0"
    # Keep an operator's higher/unlimited setting; never lower an existing cap.
    target = current if sufficient_task_limit(current) else str(ACTION_TASKS_MIN)
    unit = SYSTEMD_ROOT / "openbox-browser-runtime.service"
    dropin = SYSTEMD_ROOT / (ACTION_SERVICE + ".d/browser-runtime.conf")
    resources = SYSTEMD_ROOT / (ACTION_SERVICE + ".d/browser-resources.conf")
    desired = {
        unit: SERVICE,
        dropin: "[Unit]\nRequires=openbox-browser-runtime.service\nAfter=openbox-browser-runtime.service\n",
        resources: f"[Service]\nTasksMax={target}\n",
    }
    changed = False
    backup = None
    for path, source in desired.items():
        if path.is_symlink():
            raise RuntimeError(f"Refusing to overwrite symlink {path}")
        if not path.exists() or path.read_text() != source:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if backup is None:
                    root = Path('/opt/openbox/backups')
                    root.mkdir(mode=0o700, parents=True, exist_ok=True)
                    backup = Path(tempfile.mkdtemp(prefix='browser-service-', dir=root))
                shutil.copy2(path, backup / path.name)
            fd, stage = tempfile.mkstemp(prefix='.browser-service-', dir=path.parent)
            try:
                with os.fdopen(fd, 'w') as stream:
                    stream.write(source)
                os.chmod(stage, 0o644)
                os.replace(stage, path)
            finally:
                Path(stage).unlink(missing_ok=True)
            changed = True
    if changed:
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)
    if int(properties.get("MainPID", "0")) > 0 and (
        not sufficient_task_limit(current) or task_budget_problems(SYSTEMD_ROOT, ACTION_CGROUP)
    ):
        # Persistent system.control settings outrank /run/system.control.
        # --runtime would be undone by the daemon-reload implicit in `enable`
        # on legacy guests with a pinned 512 cap. Back up that one property
        # and persist the live update; never change MemoryMax or restart.
        control = SYSTEMD_CONTROL_ROOT / (ACTION_SERVICE + ".d/50-TasksMax.conf")
        if control.is_symlink():
            raise RuntimeError("Refusing to overwrite a symlinked task-budget property")
        if control.exists():
            if backup is None:
                root = Path('/opt/openbox/backups')
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
                backup = Path(tempfile.mkdtemp(prefix='browser-service-', dir=root))
            shutil.copy2(control, backup / control.name)
        subprocess.run(["systemctl", "set-property", ACTION_SERVICE,
                        f"TasksMax={target}"], check=True, timeout=20)
        changed = True
    subprocess.run(["systemctl", "enable", "openbox-browser-runtime.service"],
                   capture_output=True, check=True, timeout=30)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-deps", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--register-service", action="store_true")
    parser.add_argument("--lock-file", type=Path, default=LOCK_FILE)
    parser.add_argument("--registry", default="https://registry.npmjs.org")
    args = parser.parse_args()
    os.environ["PATH"] = SAFE_PATH
    if args.check:
        problems = runtime_problems(SKILL_DIR, args.lock_file, Path('/opt/google/chrome/google-chrome'))
        print(json.dumps({"version": RUNTIME_VERSION, "ready": not problems, "problems": problems}))
        raise SystemExit(1 if problems else 0)
    if os.geteuid() != 0:
        raise SystemExit("Browser runtime needs administrator repair; run as root")
    os.umask(0o022)
    with runtime_lock(Path("/var/lock/openbox-browser-runtime.lock")):
        repair_runtime(args)


def repair_runtime(args) -> None:
    launcher = Path('/opt/google/chrome/google-chrome')
    service_changed = register_boot_service() if args.register_service else False
    if not runtime_problems(SKILL_DIR, args.lock_file, launcher):
        print(json.dumps({"version": RUNTIME_VERSION, "ready": True, "changed": service_changed}))
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = Path("/opt/openbox/backups")
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=f"browser-runtime-{stamp}-", dir=backup_root))
    os.chmod(backup, 0o700)
    print(f"backup={backup}", flush=True)
    for destination in ensure_node_commands(Path("/usr/local/bin"), Path("/opt/bossip/runtime/node/bin")):
        print(f"added_command={destination}", flush=True)
    print(f"chrome_gate_updated={repair_gate(launcher, backup)}", flush=True)
    if args.install_deps and dependency_problems(SKILL_DIR, args.lock_file):
        install_dependencies(SKILL_DIR, backup, args.registry, args.lock_file)
    problems = runtime_problems(SKILL_DIR, args.lock_file, launcher)
    if problems:
        raise RuntimeError("Browser runtime verification failed: " + "; ".join(problems))
    print(json.dumps({"version": RUNTIME_VERSION, "ready": True, "changed": True}))


if __name__ == "__main__":
    main()
