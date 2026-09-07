"""Versioned, idempotent browser readiness gate for existing and new desktops.

The same standalone installer is shipped by bootstrap, the activation worker,
and lazy browser startup. Nothing here stops Chrome, changes a profile, starts
or purchases a desktop, or grants a subscription entitlement.
"""
from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
import uuid

from sandbox.browser_runtime_repair import RUNTIME_VERSION


class BrowserRuntimeUnavailable(RuntimeError):
    pass


def runtime_files() -> dict[str, str]:
    """Pinned repair code and relay sources shipped to every desktop."""
    root = Path(__file__).resolve().parent
    dev_browser = root.parents[1] / "container" / "dev-browser"
    source_paths = [
        dev_browser / "SKILL.md",
        dev_browser / "package.json",
        dev_browser / "tsconfig.json",
        *sorted((dev_browser / "scripts").glob("*.ts")),
        *sorted((dev_browser / "src").rglob("*.ts")),
    ]
    return {
        "repair_browser_runtime.py": (root / "browser_runtime_repair.py").read_text(),
        "dev-browser-package-lock.json": (root / "assets/dev-browser-package-lock.json").read_text(),
        "dev-browser-sources.json": json.dumps({
            str(path.relative_to(dev_browser)): path.read_text() for path in source_paths
        }, separators=(",", ":")),
    }


def runtime_install_script() -> str:
    files = runtime_files()
    # Base85 leaves room under Cloud Assistant's 16 KiB encoded-command cap.
    payload = base64.b85encode(gzip.compress(json.dumps(files).encode(), mtime=0)).decode()
    # Atomic, fixed-path writes of bundled code, never any home/profile files.
    return f"""set -eu
python3 - <<'OPENBOX_RUNTIME'
import base64,gzip,json,os,pathlib,shutil,subprocess,tempfile
if os.geteuid() != 0:
    raise SystemExit('Browser runtime repair requires a root action server on Wuying')
root=pathlib.Path('/opt/openbox/tools')
root.mkdir(parents=True,exist_ok=True)
if root.is_symlink() or root.stat().st_uid != 0 or root.stat().st_mode & 0o022:
    raise SystemExit('OpenBox tools directory must be root-owned and not a symlink')
files=json.loads(gzip.decompress(base64.b85decode('{payload}')))
backup=None
for name,source in files.items():
    path=root/name
    if path.is_symlink() or (path.exists() and path.stat().st_uid != 0):
        raise SystemExit('Refusing to replace untrusted runtime tool')
    if path.exists() and path.read_text()==source:
        continue
    if path.exists():
        if backup is None:
            backups=pathlib.Path('/opt/openbox/backups')
            backups.mkdir(mode=0o700,exist_ok=True)
            backup=pathlib.Path(tempfile.mkdtemp(prefix='browser-tools-',dir=backups))
        shutil.copy2(path,backup/name)
    fd,stage=tempfile.mkstemp(prefix='.browser-runtime-',dir=root)
    try:
        with os.fdopen(fd,'w') as stream: stream.write(source)
        os.chmod(stage,0o644)
        os.replace(stage,path)
    finally:
        pathlib.Path(stage).unlink(missing_ok=True)
subprocess.run(['python3',str(root/'repair_browser_runtime.py'),'--install-deps','--register-service'],check=True,timeout=330)
# Repair a failed boot gate after the installer has released its lock.
# `start` is a no-op for a live action server; it never restarts a user session.
if subprocess.run(['systemctl','is-failed','--quiet','openbox-browser-runtime.service']).returncode == 0:
    subprocess.run(['systemctl','reset-failed','openbox-browser-runtime.service','openbox-action-server.service'],check=True)
    subprocess.run(['systemctl','start','openbox-browser-runtime.service'],check=True,timeout=60)
    subprocess.run(['systemctl','start','openbox-action-server.service'],check=True,timeout=60)
OPENBOX_RUNTIME
"""


def runtime_cloud_commands() -> list[str]:
    """Bound every Cloud Assistant payload, even as future repair code grows."""
    script = runtime_install_script()
    if len(base64.b64encode(script.encode())) < 16_000:
        return [script]
    payload = base64.b85encode(gzip.compress(script.encode(), mtime=0)).decode()
    directory = f"/var/tmp/openbox-browser-runtime-{uuid.uuid4().hex}"
    stage = directory + "/payload.b85"
    commands = []
    for offset in range(0, len(payload), 7500):
        chunk = payload[offset:offset + 7500]
        first = offset == 0
        guard = f"mkdir -m 700 {directory}" if first else f"test -f {stage}"
        commands.append(f"set -eu\numask 077\n{guard}\nprintf '%s' '{chunk}' {'>' if first else '>>'} {stage}\n")
    commands.append(f"""set -eu
python3 - <<'OPENBOX_RUN'
import base64,gzip,pathlib,subprocess
stage=pathlib.Path('{stage}')
try:
    subprocess.run(['bash'],input=gzip.decompress(base64.b85decode(stage.read_bytes())),check=True)
finally:
    stage.unlink(missing_ok=True)
    stage.parent.rmdir()
OPENBOX_RUN
""")
    return commands


def verified_result(output: str) -> dict:
    for line in reversed(output.splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("version") == RUNTIME_VERSION and data.get("ready") is True:
            return data
    raise BrowserRuntimeUnavailable("Browser runtime did not pass the current version's verification")


async def ensure_browser_runtime(client) -> dict:
    # Some legacy action containers see the guest filesystem read-only. A
    # healthy runtime must not need a write lock, code upload or systemctl.
    # Bootstrap/Cloud Assistant still handle repairs before channel activation.
    checked = await client.execute(
        "python3 /opt/openbox/tools/repair_browser_runtime.py --check", timeout=45
    )
    if checked.exit_code == 0:
        try:
            return verified_result(checked.stdout or "")
        except BrowserRuntimeUnavailable:
            pass  # An older verifier cannot certify this backend's version.
    result = await client.execute(runtime_install_script(), timeout=350)
    if result.exit_code != 0:
        raise BrowserRuntimeUnavailable(
            "Browser runtime repair failed; desktop cannot be marked ready. "
            "Check /opt/openbox/backups/browser-runtime-*/npm-install.log on the desktop."
        )
    return verified_result(result.stdout or "")


async def ensure_desktop_browser_runtime(desktop_id: str) -> dict:
    """Works even when a pool desktop's application tunnel is revoked."""
    from sandbox.channel import run_desktop_command

    try:
        for command in runtime_cloud_commands():
            output = await run_desktop_command(desktop_id, command, timeout=480)
    except Exception as exc:
        raise BrowserRuntimeUnavailable(f"Browser runtime preparation failed for {desktop_id}") from exc
    return verified_result(output)
