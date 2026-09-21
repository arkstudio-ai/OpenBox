"""One durable before/after file boundary per team, outside journal locks.

The short journal claim fences duplicate workers. No model or member tool may
start before the initial capture settles; closing retains project ownership
until the final capture settles. A failed capture is visible, never an empty
diff. Expired claims are safe to retry because git write-tree is idempotent.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import re
import uuid

from team.commands import run_status
from team.errors import TeamError
from team.journal import Actor, command, snapshot, utcnow
from team.state import TERMINAL

CAPTURE_TIMEOUT = 30
CLAIM_SECONDS = 40
SETTLED = {"ready", "unavailable"}


async def capture(root_id, owner_id, sandbox=None):
    from snapshot import snapshot as files
    return await files.track(root_id, sandbox, user_id=owner_id)


async def ensure(run_id: str, actor: Actor, phase: str, *, sandbox=None) -> bool:
    """Return False only while another worker owns an unexpired capture."""
    if phase not in {"start", "end"}:
        raise ValueError("Unknown snapshot boundary")
    actor = replace(actor, kind="server", member_id=None, driver_run_id=None, generation=None)
    state = await snapshot(run_id, actor)
    records = state["run"].get("workspace_snapshots")
    # Older runs have no before-image. Never invent one retrospectively.
    if records is None or records.get(phase, {}).get("status") in SETTLED:
        return True
    if state["run"]["state"] in TERMINAL:
        return True
    prior = records.get(phase, {})
    if prior.get("lease_until") and datetime.fromisoformat(prior["lease_until"]) > utcnow():
        return False
    token = uuid.uuid4().hex

    async def claim(writer):
        current = writer.state["run"]
        records = dict(current["workspace_snapshots"])
        prior = records.get(phase, {})
        if prior.get("status") in SETTLED:
            return {"settled": True}
        if prior.get("lease_until") and datetime.fromisoformat(prior["lease_until"]) > utcnow():
            return {"claimed": False}
        if phase == "end" and current["state"] not in {"canceling", "completing"}:
            raise TeamError("TEAM_SNAPSHOT_PENDING", "The team is still using its workspace.")
        records[phase] = {"status": "capturing", "token": token,
            "lease_until": (utcnow() + timedelta(seconds=CLAIM_SECONDS)).isoformat()}
        run_status(writer, current["state"], workspace_snapshots=records)
        return {"claimed": True, "root": current["root_session_id"]}

    claimed = await command(run_id, actor, f"snapshot-claim:{phase}:{token}", {}, claim)
    if not claimed.get("claimed"):
        return bool(claimed.get("settled"))
    try:
        tree = await asyncio.wait_for(capture(claimed["root"], actor.owner_user_id, sandbox), CAPTURE_TIMEOUT)
        if not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{40,64}", tree):
            tree = None
    except Exception:
        tree = None

    async def settle(writer):
        current = writer.state["run"]
        records = dict(current["workspace_snapshots"])
        if records.get(phase, {}).get("token") != token:
            return {"settled": records.get(phase, {}).get("status") in SETTLED}
        records[phase] = {"status": "ready" if tree else "unavailable", "hash": tree}
        run_status(writer, current["state"], workspace_snapshots=records)
        if not tree:
            writer.append("team.notice", "notice", {"id": f"snapshot:{phase}",
                "code": "WORKSPACE_SNAPSHOT_UNAVAILABLE", "phase": phase,
                "message": "The workspace snapshot could not be captured; file changes are unavailable."})
        return {"settled": True}

    return (await command(run_id, actor, f"snapshot-settle:{phase}:{token}", {}, settle))["settled"]


async def before_model(sandbox=None) -> bool:
    """The loop uses the return value to suppress all per-step team snapshots."""
    from team.runtime_binding import current_binding
    binding = current_binding()
    if binding is None or binding.run_id is None:
        return False
    actor = Actor(binding.owner_user_id, binding.workspace_id, "server")
    async with asyncio.timeout(CLAIM_SECONDS + CAPTURE_TIMEOUT + 5):
        while not await ensure(binding.run_id, actor, "start", sandbox=sandbox):
            await asyncio.sleep(0.2)
    return True


async def diff(run_id: str, actor: Actor, *, full: bool = False):
    from db.base import get_db_session
    from team.journal import owned_run
    from snapshot import snapshot as files
    async with get_db_session() as db:
        run = await owned_run(db, run_id, actor)
        before, after, root = run.start_snapshot, run.end_snapshot, run.root_session_id
    if not before or not after:
        raise TeamError("WORKSPACE_SNAPSHOT_UNAVAILABLE", "Both workspace snapshots are required to show this team's changes.", status=409)
    if full:
        return await files.diff_full(before, after, session_id=root, user_id=actor.owner_user_id)
    return [row.__dict__ for row in await files.diff(before, after, session_id=root, user_id=actor.owner_user_id)]
