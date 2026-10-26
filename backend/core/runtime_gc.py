"""Optional one-time separation of startup imports from request-time GC.

Call before infrastructure, workers and requests are created. Only the startup
heap is frozen; cyclic collection remains enabled for all later allocations.
Never call this per request or periodically: that would retain request cycles.
The flag is opt-in so a deployment can compare pause time and resident memory.
"""
import gc
import os
import time

from core.log import create_logger

log = create_logger("runtime_gc")
_started = False


def freeze_startup_heap() -> bool:
    global _started
    if _started:
        return False
    # Mark the one startup boundary even when disabled. Changing an env var
    # later must not freeze objects allocated while serving requests.
    _started = True
    if os.getenv("OPENBOX_FREEZE_STARTUP_HEAP", "0") != "1":
        return False
    if not gc.isenabled():
        return False  # Respect an embedding host that disabled cyclic GC.
    started = time.monotonic()
    gc.collect()
    gc.freeze()
    log.info("Startup heap frozen objects=%d elapsed_ms=%.1f runtime_gc_enabled=%s",
        gc.get_freeze_count(), (time.monotonic() - started) * 1000, gc.isenabled())
    return True
