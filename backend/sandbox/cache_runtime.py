"""Publish the fixed cache runtime and low-priority local systemd timers."""
import base64
import hashlib
import json
import shlex

from scripts.wuying_deploy_action_server import action_server_bundle, retained_deployment_commands

DB_PATH = "/var/lib/openbox-cache/control.sqlite3"
_SERVICE = """[Unit]
Description=OpenBox {description}
ConditionPathIsDirectory=/workspace

[Service]
Type=oneshot
StateDirectory=openbox-cache
StateDirectoryMode=0700
ExecStart=/usr/bin/python3 -I /opt/action_server/cache_control.py {command} --db={db}
Nice=19
IOSchedulingClass=idle
CPUQuota=20%
MemoryMax={memory}
TasksMax=16
TimeoutStartSec={timeout}
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/workspace /var/lib/openbox-cache
PrivateTmp=true
"""
_TIMER = """[Unit]
Description=OpenBox {description}

[Timer]
OnBootSec={interval}s
OnUnitInactiveSec={interval}s
AccuracySec=5s
RandomizedDelaySec=5s

[Install]
WantedBy=timers.target
"""


def unit_files():
    result = {"openbox-action-server.service.d/10-openbox-cache.conf":
              "[Service]\nEnvironment=OPENBOX_CACHE_CONTROL_DB=" + DB_PATH + "\n"}
    for name, command, interval, memory, timeout in (
            ("reclaim", "tick", 60, "128M", 20), ("recover", "recover", 300, "256M", 1800)):
        description = "cache reclamation" if name == "reclaim" else "pending output recovery"
        result[f"openbox-cache-{name}.service"] = _SERVICE.format(description=description, command=command,
            db=DB_PATH, memory=memory, timeout=timeout)
        result[f"openbox-cache-{name}.timer"] = _TIMER.format(description=description, interval=interval)
    return result


def units_command(*, start=True):
    payload = base64.b64encode(json.dumps(unit_files()).encode()).decode()
    script = f'''import base64,json,os,pathlib,stat,subprocess
if os.geteuid()!=0:raise RuntimeError('Cache unit installation requires root')
root=pathlib.Path('/etc/systemd/system')
files=json.loads(base64.b64decode({payload!r}))
for name,body in files.items():
 path=root/name
 path.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
 for parent in (path.parent,*path.parent.parents):
  s=parent.lstat()
  if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise RuntimeError('Unprotected service path')
 if path.exists() or path.is_symlink():
  s=path.lstat()
  if not stat.S_ISREG(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022 or path.read_text()!=body:raise RuntimeError('Existing cache unit differs')
for name,body in files.items():
 path=root/name
 if not path.exists():
  with path.open('x') as output:output.write(body)
  path.chmod(0o644)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable',{'"--now",' if start else ''}'openbox-cache-reclaim.timer','openbox-cache-recover.timer'],check=True)
print('cache timer units verified')
'''
    return "python3 -I -S -c " + shlex.quote(script)


def runtime_commands():
    files = action_server_bundle()
    identity = hashlib.sha256(json.dumps({k: v["sha256"] for k, v in files.items()}, sort_keys=True).encode()).hexdigest()
    return identity, [*retained_deployment_commands(files, "cache_" + identity[:32]), units_command()]


async def ensure_desktop_cache_runtime(desktop_id, *, authority_check):
    from sandbox.channel import run_desktop_command
    from sandbox.managed_cache import enabled
    if not enabled():
        return
    files = action_server_bundle()
    expected = {name: value["sha256"] for name, value in files.items()}
    probe = f'''import hashlib,json,pathlib
root=pathlib.Path('/opt/action_server')
expected={expected!r}
current=all((root/name).is_file() and not (root/name).is_symlink() and hashlib.sha256((root/name).read_bytes()).hexdigest()==digest for name,digest in expected.items())
units={unit_files()!r}
current=current and all((pathlib.Path('/etc/systemd/system')/name).is_file() and (pathlib.Path('/etc/systemd/system')/name).read_text()==body for name,body in units.items())
print(json.dumps({{'current':current}}))
'''
    await authority_check()
    observed = json.loads(await run_desktop_command(desktop_id, "python3 -I -S -c " + shlex.quote(probe), timeout=60))
    if observed["current"]:
        return
    _, commands = runtime_commands()
    for command in commands:
        await authority_check()
        await run_desktop_command(desktop_id, command, timeout=90)
    # Channel installation starts the service after all runtime prerequisites.
