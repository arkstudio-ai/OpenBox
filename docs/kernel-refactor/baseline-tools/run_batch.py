"""Run each test file of a batch as its own pytest process under coreutils timeout.

Usage: python run_batch.py <backend_dir> <list_file> <out_dir> <per_file_timeout_s>
Writes <out_dir>/xml/<slug>.xml, <out_dir>/log/<slug>.log and appends one JSON
line per file to <out_dir>/runs.jsonl.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PY = "/Volumes/fanxiang/workspace2/OpenBox/backend/.venv/bin/python"


def main() -> None:
    backend = Path(sys.argv[1]).resolve()
    files = [l.strip() for l in Path(sys.argv[2]).read_text().splitlines() if l.strip()]
    out = Path(sys.argv[3])
    limit = int(sys.argv[4])
    (out / "xml").mkdir(parents=True, exist_ok=True)
    (out / "log").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "TRAJECTORY_RECORDING_ENABLED": "false",
        "LITELLM_MODE": "PRODUCTION",          # litellm DEV mode would load the main checkout's backend/.env
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",  # same as main.py; no fetch of the cost map
        "PYTHONPATH": str(backend),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    for f in files:
        slug = f.replace("/", "__")
        xml = out / "xml" / f"{slug}.xml"
        log = out / "log" / f"{slug}.log"
        cmd = ["timeout", "-k", "15", str(limit), PY, "-m", "pytest", "-c", "pyproject.toml",
               "--rootdir", ".", f, "-q", "-p", "no:cacheprovider", "-rfE",
               f"--junitxml={xml}", "-o", "junit_family=xunit2"]
        t0 = time.monotonic()
        with log.open("w") as fh:
            rc = subprocess.call(cmd, cwd=backend, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL)
        dt = time.monotonic() - t0
        rec = {"file": f, "rc": rc, "wall_s": round(dt, 2), "timed_out": rc in (124, 137),
               "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - dt))}
        with (out / "runs.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    (out / "DONE").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))


if __name__ == "__main__":
    main()
