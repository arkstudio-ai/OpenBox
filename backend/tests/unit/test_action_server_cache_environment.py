"""A managed task's commands keep package-manager caches under the task's collected temp root."""
from pathlib import Path
import sys

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "container"))
import cache_api  # noqa: E402  (container/cache_api.py, shipped to desktops by the Action Server rollout)

TMPDIR = "/home/sandbox/.openbox/cache/tasks/session-a/run-a/tmp"
PENDING = "/home/sandbox/.openbox/pending/session-a/incoming/run-a"


class FakeStore:
    def __init__(self):
        self.begun = []

    def begin(self, task, generation, run):
        self.begun.append((task, generation, run))
        return {"task_id": task, "tmpdir": TMPDIR, "pending_dir": PENDING}


def request(**headers):
    encoded = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/execute", "headers": encoded})


TASK = {"X-OpenBox-Cache-Task": "session-a", "X-OpenBox-Cache-Run": "run-a", "X-OpenBox-Cache-Generation": "3"}
BASE = {"HOME": "/home/sandbox", "PATH": "/usr/bin:/bin", "USER": "sandbox"}


async def test_task_commands_put_pip_and_npm_caches_under_the_task_tmpdir():
    store = FakeStore()
    env = await cache_api.execution_environment(store, request(**TASK), dict(BASE))
    assert store.begun == [("session-a", 3, "run-a")]
    assert env == {**BASE, "TMPDIR": TMPDIR, "TMP": TMPDIR, "TEMP": TMPDIR,
                   "PIP_CACHE_DIR": f"{TMPDIR}/pip-cache",
                   "npm_config_cache": f"{TMPDIR}/npm-cache", "NPM_CONFIG_CACHE": f"{TMPDIR}/npm-cache",
                   "OPENBOX_CACHE_TASK": "session-a", "OPENBOX_PENDING_ROOT": PENDING,
                   "OPENBOX_CACHE_RUN": "run-a"}


@pytest.mark.parametrize("store,headers", [(None, TASK), (FakeStore(), {})])
async def test_unmanaged_commands_keep_their_environment(store, headers):
    env = await cache_api.execution_environment(store, request(**headers), dict(BASE))
    assert env == BASE
