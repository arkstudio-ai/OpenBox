"""Real GC in a child process; do not freeze pytest's own object graph."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("enabled", [False, True])
def test_request_cycles_still_collected_and_startup_boundary_cannot_move(enabled):
    script = '''
import gc,os,weakref
from core.runtime_gc import freeze_startup_heap
enabled=os.environ['OPENBOX_FREEZE_STARTUP_HEAP']=='1'
assert freeze_startup_heap() is enabled
assert gc.isenabled()
class Cycle: pass
value=Cycle();value.self=value;ref=weakref.ref(value)
os.environ['OPENBOX_FREEZE_STARTUP_HEAP']='1'
assert freeze_startup_heap() is False
del value
gc.collect()
assert ref() is None
'''
    subprocess.run([sys.executable, "-c", script], check=True, timeout=20,
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "OPENBOX_FREEZE_STARTUP_HEAP": "1" if enabled else "0"}, capture_output=True)


def test_existing_frozen_objects_remain_frozen_and_new_request_cycles_collect():
    script = '''
import gc,weakref
from core.runtime_gc import freeze_startup_heap
gc.collect();gc.freeze();before=gc.get_freeze_count()
assert freeze_startup_heap() is True
assert gc.get_freeze_count() >= before
class Cycle: pass
value=Cycle();value.self=value;ref=weakref.ref(value)
assert freeze_startup_heap() is False
del value
gc.collect()
assert ref() is None
'''
    subprocess.run([sys.executable, "-c", script], check=True, timeout=20,
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "OPENBOX_FREEZE_STARTUP_HEAP": "1"}, capture_output=True)
