"""Periodic probe of desktop login state across the fleet.

Runs every six hours at cookie level (S1). Once a day, in the early-morning
window when desktops are least likely to be in use, the pass also does the
same-origin endpoint check (S2) for sites that have not had one in 20 hours.
A desktop that is busy (lease held) or unreachable is skipped, never forced.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.log import create_logger
from platforms.desktop import service
from platforms.errors import PlatformError

log = create_logger("platforms.desktop.tasks")

INTERVAL_SEC = 6 * 60 * 60
#: Local hours (Asia/Shanghai) during which the pass runs level 2.
LEVEL2_WINDOW = (6, 9)
#: A workspace probed more recently than this is left alone by the tick.
MIN_GAP = timedelta(hours=5)
#: Spread desktops out a little so the pass never looks like a sweep.
MAX_JITTER_SEC = 20
_TZ = ZoneInfo("Asia/Shanghai")


def level_for(now: datetime | None = None) -> int:
    local = (now or datetime.now(timezone.utc)).astimezone(_TZ)
    return 2 if LEVEL2_WINDOW[0] <= local.hour < LEVEL2_WINDOW[1] else 1


async def _recently_probed(workspace_id: str, now: datetime) -> bool:
    from sqlalchemy import func, select

    from db.base import get_db_session
    from db.models.platform_account import PlatformAccount

    async with get_db_session() as db:
        last = (
            await db.execute(
                select(func.max(PlatformAccount.last_probe_at)).where(
                    PlatformAccount.workspace_id == workspace_id,
                    PlatformAccount.auth_kind == service.AUTH_KIND,
                    PlatformAccount.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if last is None:
        return False
    last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
    return now - last < MIN_GAP


async def run_desktop_login_probe(*, sleep=asyncio.sleep, now: datetime | None = None) -> dict:
    """One pass over every assigned desktop. Returns counts for the log."""
    from db.repository.cloud_desktop_repo import cloud_desktop_repo

    now = now or datetime.now(timezone.utc)
    level = level_for(now)
    counts = {"level": level, "probed": 0, "busy": 0, "unreachable": 0, "skipped": 0}
    for record in await cloud_desktop_repo.list_pool_state("assigned"):
        workspace_id = record.get("workspace_id")
        if not workspace_id or not record.get("desktop_id") or record.get("tunnel_state") != "up":
            counts["skipped"] += 1
            continue
        if await _recently_probed(workspace_id, now):
            counts["skipped"] += 1
            continue
        await sleep(random.uniform(0, MAX_JITTER_SEC))
        try:
            await service.probe_workspace(
                workspace_id, level=level, lease=True, session_id="desktop-login-probe",
            )
            counts["probed"] += 1
        except service.DesktopBusy:
            counts["busy"] += 1
        except PlatformError as exc:
            counts["unreachable"] += 1
            log.info("desktop login probe skipped workspace=%s: %s", workspace_id, exc)
        except Exception:
            counts["unreachable"] += 1
            log.warning("desktop login probe failed workspace=%s", workspace_id, exc_info=True)
    log.info("desktop login probe pass %s", counts)
    return counts


async def run() -> None:
    await run_desktop_login_probe()
