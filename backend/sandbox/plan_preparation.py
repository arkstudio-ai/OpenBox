"""Plan-file preparation before model dispatch, with a durable Driver origin."""
from pathlib import PurePosixPath
import shlex

from agent.driver import _current_lease
from assistant import resource_control as controls
from sandbox.runtime_operation import run_runtime_operation


async def prepare_plan_file(sandbox, *, path, session_id, user_id,
                            create_directory, step=0, run_fence=None):
    probe = f"test -f {shlex.quote(path)} && echo exists || echo missing"
    mkdir = f"mkdir -p -- {shlex.quote(str(PurePosixPath(path).parent))}"
    lease = _current_lease.get()
    desktop = getattr(sandbox, "desktop_id", None)
    physical = lease is not None and isinstance(desktop, str) and bool(desktop)

    async def prepare():
        result = await sandbox.execute(probe, timeout=5, workdir="/")
        if result.exit_code != 0 or result.stdout.strip() not in {"exists", "missing"}:
            raise RuntimeError("Plan-file observation did not complete")
        exists = result.stdout.strip() == "exists"
        if create_directory and not exists:
            result = await sandbox.execute(mkdir, timeout=5, workdir="/")
            if result.exit_code != 0:
                raise RuntimeError("Plan directory preparation did not complete")
        return {"exists": exists}

    if physical:
        if run_fence is not None and run_fence != (lease.session_id, lease.run_id, lease.generation):
            raise controls.unavailable()
        receipt = await run_runtime_operation(sandbox, session_id=session_id, user_id=user_id,
            stage="plan_entry" if create_directory else "plan_transition",
            key=f"{lease.run_id}:{lease.generation}:{step}",
            payload={"path": path, "create_directory": create_directory}, operation=prepare)
        if not isinstance(receipt, dict) or set(receipt) != {"exists"} or type(receipt["exists"]) is not bool:
            raise RuntimeError("Plan preparation receipt is unavailable")
        return receipt["exists"]

    # Preserve ordinary Docker and non-Driver best-effort reminders. They do
    # not acquire or claim a physical resource lease through this adapter.
    exists = False
    try:
        result = await sandbox.execute(probe, timeout=5)
        exists = result.stdout.strip() == "exists"
    except Exception:
        pass
    if create_directory and not exists:
        try:
            await sandbox.execute(mkdir, timeout=5)
        except Exception:
            pass
    return exists
