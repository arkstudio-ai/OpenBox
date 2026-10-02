"""Read-only search adapter for desktops predating include_sensitive support.

The legacy /grep and /glob endpoints silently ignore unknown JSON fields.
Run a self-contained program through their existing /execute contract so the
same default exclusions apply without upgrading a user's running desktop.
"""

import json
import shlex


_SEARCH_PROGRAM = r'''
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import sys
import time

req = json.loads(sys.argv[1])
root = Path(req["path"])
operation = req["operation"]

def sensitive(path):
    return any(part.casefold().startswith(".env") or part.casefold() == ".ssh"
               or "credentials" in part.casefold() for part in Path(path).parts)

allow_sensitive = req["include_sensitive"] and (
    sensitive(root) or (operation == "glob" and sensitive(req["pattern"]))
)

def visible(path):
    return allow_sensitive or not (sensitive(path) or sensitive(path.resolve()))

if not root.exists():
    raise FileNotFoundError(f"Path not found: {root}")

if operation == "glob":
    matches = []
    if visible(root):
        for path in root.glob(req["pattern"]):
            if path.is_symlink() or not path.is_file() or not visible(path):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0
            matches.append((str(path), mtime))
    matches.sort(key=lambda item: item[1], reverse=True)
    print(json.dumps({"files": [path for path, _ in matches[:1000]]}))
else:
    started = time.monotonic()
    def candidates():
        if not visible(root):
            return
        if root.is_file():
            yield root
            return
        for directory, dirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            dirs[:] = [name for name in dirs if visible(base / name)
                       and not (base / name).is_symlink()]
            for name in files:
                path = base / name
                if not path.is_symlink() and path.is_file() and visible(path):
                    yield path

    output = []
    batch = []
    def search(files):
        remaining = 30 - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("grep timed out after 30s")
        result = subprocess.run(
            ["grep", "-nH", "--color=never", "-m", str(req["max_results"]),
             "--", req["pattern"], *files],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=remaining,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError("grep failed: " + result.stderr.decode("utf-8", errors="replace"))
        output.append(result.stdout.decode("utf-8", errors="replace"))

    for path in candidates():
        if req.get("type") and not fnmatch.fnmatch(path.name, "*." + req["type"]):
            continue
        batch.append(str(path))
        if len(batch) == 100:
            search(batch)
            batch = []
    if batch:
        search(batch)
    print(json.dumps({"output": "".join(output)}))
'''


def search_command(operation: str, **request: object) -> str:
    """Quote the program and data separately; no model text becomes shell code."""
    payload = json.dumps({"operation": operation, **request}, ensure_ascii=False)
    return shlex.join(["python3", "-c", _SEARCH_PROGRAM, payload])
