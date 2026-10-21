"""Ship the current built-in video skill to new and existing Wuying guests.

Only the application's fixed, root-owned bundle is updated. User skills,
workspaces, services and desktop lifecycle are outside this operation.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path
import shlex

SKILL_ROOT = Path(__file__).resolve().parents[1] / ".openbox/skills/video-production"
REMOTE_ROOT = "/opt/openbox/skills/video-production"


def video_skill_bundle() -> dict[str, dict[str, str]]:
    files = {}
    for path in sorted(SKILL_ROOT.rglob("*")):
        relative = path.relative_to(SKILL_ROOT)
        if not path.is_file() or any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue
        content = path.read_bytes()
        if path.suffix == ".py":
            compile(content, str(path), "exec")
        files[relative.as_posix()] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "content": content.decode("utf-8"),
        }
    if "SKILL.md" not in files or "scripts/state.py" not in files:
        raise RuntimeError("Built-in video skill bundle is incomplete")
    return files


def video_skill_commands(*, remote_root: str = REMOTE_ROOT) -> list[str]:
    """Bounded, replay-safe publication retaining originals and all staging.

    remote_root is only an isolated test fixture seam. Runtime callers use
    the fixed system bundle. No recursive removal, service restart or chmod
    of pre-existing directory trees is performed.
    """
    files = video_skill_bundle()
    expected = {name: value["sha256"] for name, value in files.items()}
    identity = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
    packed = base64.b64encode(gzip.compress(json.dumps(files, sort_keys=True, ensure_ascii=False).encode(), mtime=0)).decode()
    chunks = [packed[i:i + 6500] for i in range(0, len(packed), 6500)]
    setup = f'''import os,pathlib,stat
root=pathlib.Path({remote_root!r})
def protected(path):
    for item in (path,*path.parents):
        info=item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022:
            raise RuntimeError('Video skill path is not root protected')
if os.geteuid()!=0: raise RuntimeError('Video skill deployment requires root service authority')
protected(root.parent)
root.mkdir(mode=0o755,exist_ok=True)
protected(root)
releases=root.parent/'.video-production-releases'
releases.mkdir(mode=0o700,exist_ok=True)
protected(releases)
stage=releases/{identity!r}
stage.mkdir(mode=0o700,exist_ok=True)
protected(stage)
'''
    def command(code):
        return "python3 -I -S -c " + shlex.quote(code)

    commands = []
    for index, chunk in enumerate(chunks):
        commands.append(command(setup + f'''
path=stage/{('chunk-%04d' % index)!r}
data={chunk!r}.encode()
if path.exists():
    protected(path)
    if path.read_bytes()!=data: raise RuntimeError('Skill release chunk changed')
else:
    with path.open('xb') as out: out.write(data)
    path.chmod(0o600)
print('skill chunk {index + 1}/{len(chunks)} retained')
'''))
    commands.append(command(setup + f'''
import base64,gzip,hashlib,json
expected={expected!r}
raw=b''.join((stage/('chunk-%04d'%i)).read_bytes() for i in range({len(chunks)}))
bundle=json.loads(gzip.decompress(base64.b64decode(raw)))
if set(bundle)!=set(expected): raise RuntimeError('Skill bundle file set changed')
contents={{}}
for name,entry in bundle.items():
    parts=pathlib.PurePosixPath(name)
    if parts.is_absolute() or '..' in parts.parts: raise RuntimeError('Invalid bundle path')
    data=entry['content'].encode('utf-8')
    if hashlib.sha256(data).hexdigest()!=expected[name]: raise RuntimeError('Skill bundle checksum changed')
    if name.endswith('.py'): compile(data,name,'exec')
    contents[name]=data
originals=stage/'originals.json'
before={{}}
for name in contents:
    target=root/name
    parent=target.parent
    parent.mkdir(mode=0o755,parents=True,exist_ok=True)
    protected(parent)
    if target.exists() or target.is_symlink():
        protected(target)
        if not target.is_file(): raise RuntimeError('Skill target is not a regular file')
        before[name]=hashlib.sha256(target.read_bytes()).hexdigest()
    else: before[name]=None
if not originals.exists():
    with originals.open('x') as out: json.dump(before,out,sort_keys=True)
    originals.chmod(0o600)
protected(originals)
previous=json.loads(originals.read_text())
if set(previous)!=set(contents): raise RuntimeError('Skill preimage file set changed')
for name in contents:
    if before[name] not in (previous[name],expected[name]): raise RuntimeError('Skill preimage changed: '+name)
for name,data in contents.items():
    target=root/name
    if before[name]==expected[name]: continue
    if previous[name] is not None:
        backup=stage/'original'/name
        backup.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        protected(backup.parent)
        if not backup.exists():
            with backup.open('xb') as out: out.write(target.read_bytes())
            backup.chmod(0o600)
        protected(backup)
        if hashlib.sha256(backup.read_bytes()).hexdigest()!=previous[name]: raise RuntimeError('Skill backup differs')
    with target.open('wb') as out:
        out.write(data);out.flush();os.fsync(out.fileno())
    target.chmod(0o755 if name.startswith('scripts/') else 0o644)
actual={{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in expected}}
if actual!=expected: raise RuntimeError('Published skill checksum differs')
print(json.dumps({{'video_skill_verified':True,'bundle_sha256':{identity!r},'files':len(actual)}}))
'''))
    if any(len(base64.b64encode(script.encode())) >= 16000 for script in commands):
        raise RuntimeError("Video skill Cloud Assistant command exceeds transport limit")
    return commands


async def ensure_desktop_video_skill(desktop_id: str, *, authority_check) -> None:
    from sandbox.channel import run_desktop_command

    for script in video_skill_commands():
        await authority_check()
        await run_desktop_command(desktop_id, script, timeout=120)
