#!/usr/bin/env python3
"""Publish only the streaming reader/exit receipt to an explicit WUYING profile.

Keeps the guest's existing authentication, resource ownership and execution
identity code. Saves the original, checks its hash again before publication,
and never installs dependencies, deletes data or changes desktop bindings.
"""
from __future__ import annotations

import argparse
import ast
import base64
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import textwrap

from wuying_bootstrap import Desktop
from wuying_deploy_action_server import read_env


REMOTE = '/opt/action_server/action_server.py'
LOCAL = Path(__file__).resolve().parents[2] / 'container/action_server.py'


def stream_nodes(source):
    tree = ast.parse(source)
    route, = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'execute_stream']
    generator, = [n for n in route.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'event_generator']
    reader, = [n for n in generator.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'reader']
    receipt = generator.body[-1]
    if not isinstance(receipt, ast.Expr) or not isinstance(receipt.value, ast.Yield):
        raise ValueError('Unsupported stream exit layout')
    value = receipt.value.value
    if not isinstance(value, ast.Dict) or not any(
        isinstance(k, ast.Constant) and k.value == 'event' and isinstance(v, ast.Constant) and v.value == 'exit'
        for k, v in zip(value.keys, value.values)
    ):
        raise ValueError('Unsupported stream exit receipt')
    declared = {n.id for n in ast.walk(generator) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    if not {'stdout_done', 'stderr_done', 'process_exited', 'exit_code'} <= declared:
        raise ValueError('Unsupported stream state')
    return reader, receipt


def fragment(source, node):
    return textwrap.dedent(''.join(source.splitlines(keepends=True)[node.lineno - 1:node.end_lineno]))


def patched_source(source, reader, receipt):
    nodes = stream_nodes(source)
    lines = source.splitlines(keepends=True)
    for node, replacement in sorted(zip(nodes, (reader, receipt)), key=lambda pair: pair[0].lineno, reverse=True):
        lines[node.lineno - 1:node.end_lineno] = [textwrap.indent(replacement.rstrip() + '\n', ' ' * node.col_offset)]
    patched = ''.join(lines)
    compile(patched, 'action_server.py', 'exec')
    return patched


def publication_script(expected_sha, reader, receipt, release):
    helpers = '\n\n'.join(inspect.getsource(f) for f in (stream_nodes, patched_source))
    return f'''python3 -I -S - <<'STREAM_PATCH'
import ast,base64,hashlib,json,os,pathlib,stat,textwrap
{helpers}
target=pathlib.Path({REMOTE!r})
for p in (target,*target.parents):
    meta=p.lstat()
    if stat.S_ISLNK(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022:
        raise RuntimeError('Expected root-protected action server')
before=target.read_bytes()
if hashlib.sha256(before).hexdigest() != {expected_sha!r}:
    raise RuntimeError('Action server changed since inspection; publication stopped')
after=patched_source(before.decode(),{reader!r},{receipt!r}).encode()
stage=target.parent/'releases'/{release!r}
stage.parent.mkdir(mode=0o700,exist_ok=True)
meta=stage.parent.lstat()
if stat.S_ISLNK(meta.st_mode) or meta.st_uid!=0 or meta.st_mode & 0o022:
    raise RuntimeError('Expected root-protected release directory')
stage.mkdir(mode=0o700,exist_ok=False)
original=stage/'original-action_server.py'
original.write_bytes(before)
os.chmod(original,0o600)
candidate=stage/'action_server.py'
candidate.write_bytes(after)
os.chmod(candidate,stat.S_IMODE(target.stat().st_mode))
if target.read_bytes()!=before:
    raise RuntimeError('Action server changed before publication; original retained')
os.replace(candidate,target)
print(json.dumps({{'desktop_file':str(target),'original':str(original),'before_sha256':hashlib.sha256(before).hexdigest(),'after_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}}))
STREAM_PATCH'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', required=True, type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-restart', action='store_true')
    args = parser.parse_args()
    desktop = read_env('WUYING_DESKTOP_ID', args.env_file)
    region = read_env('WUYING_REGION_ID', args.env_file)
    if not desktop or not region:
        parser.error('The explicit profile must identify a desktop and region')
    source = LOCAL.read_text()
    reader, receipt = [fragment(source, n) for n in stream_nodes(source)]
    release = 'stream_output_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
    print(json.dumps({'desktop_id': desktop, 'region': region, 'path': REMOTE,
                      'release': release, 'restart': not args.no_restart, 'dry_run': args.dry_run}), flush=True)
    if args.dry_run:
        return
    connection = Desktop(desktop, region)
    before = json.loads(connection.run("python3 -I -S - <<'PROBE'\nimport hashlib,json,pathlib\np=pathlib.Path('/opt/action_server/action_server.py')\nprint(json.dumps({'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))\nPROBE", timeout=30))
    script = publication_script(before['sha256'], reader, receipt, release)
    if len(base64.b64encode(script.encode())) > 16 * 1024:
        raise ValueError('Patch exceeds Cloud Assistant command limit')
    print(connection.run(script, timeout=30), flush=True)
    if not args.no_restart:
        print(connection.run('systemctl restart openbox-action-server && systemctl is-active openbox-action-server', timeout=30), flush=True)


if __name__ == '__main__':
    main()
