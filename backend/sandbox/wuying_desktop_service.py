"""Per-user cloud desktop orchestration (wuying_mode="per_user").

Ports the ensure-desktop skeleton of bossip's wuying-cloud.service: check the
local record, recover by ECD tag when the record is gone, start a Stopped
desktop, create one only when nothing exists — all behind a per-user in-flight
guard so a user firing several requests in the same second cannot race-create
multiple (billable) desktops.

Provisioning takes 2-3 minutes, so it always runs as a background task and the
API reports progress; callers poll ``status()``. The unique partial index on
cloud_desktops(user_id) is the cross-worker backstop: two workers that both
miss the in-process guard cannot both insert a live record.
"""
from __future__ import annotations

import asyncio

from core.config import get_config
from core.log import create_logger
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from sandbox.channel import wuying_channel
from sandbox import wuying_ecd

log = create_logger("sandbox.wuying_desktops")

# ECD desktop_status -> our record status
_ECD_STATUS = {
    "Running": "running",
    "Stopped": "stopped",
    "Starting": "starting",
    "Pending": "starting",
    "Stopping": "stopped",
}


class DesktopNotReady(Exception):
    """The user's desktop cannot serve a ticket yet; carries the status payload."""

    def __init__(self, payload: dict):
        super().__init__(payload.get("state", "not_ready"))
        self.payload = payload


class WuyingDesktopService:
    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Task] = {}
        self._patrol: asyncio.Task | None = None

    # -- public API ---------------------------------------------------------

    async def status(self, user_id: str) -> dict:
        """Current provisioning state for this user.

        States: not_provisioned | creating | starting | running | stopped | failed.
        """
        record = await cloud_desktop_repo.get_for_user(user_id)
        if record is None:
            record = await self._adopt_from_tags(user_id)
            if record is None:
                return {"state": "not_provisioned"}

        if user_id in self._inflight:
            return self._payload(record)

        # No in-flight work but the record says work was happening: the process
        # restarted mid-provision. Re-sync against ECD instead of trusting it.
        if record["status"] in ("creating", "starting"):
            record = await self._resync(record)
        elif (
            record["status"] == "running"
            and record.get("tunnel_state") in (None, "pending", "down")
            and get_config().wuying_routing == "per_desktop"
        ):
            self._spawn(user_id, self._channel_flow(user_id, record["id"]))
        return self._payload(record)

    async def provision(self, user_id: str, display_name: str | None = None) -> dict:
        """Idempotent kick: create or start this user's desktop as needed."""
        record = await cloud_desktop_repo.get_for_user(user_id)
        if record is None:
            record = await self._adopt_from_tags(user_id)

        if user_id in self._inflight:
            return self._payload(record) if record else {"state": "creating"}

        if record is None or record["status"] == "failed":
            if record is not None:
                await cloud_desktop_repo.soft_delete(record["id"])
            record = await cloud_desktop_repo.create(
                user_id, region_id=get_config().wuying_region_id, status="creating"
            )
            self._spawn(user_id, self._create_flow(user_id, record["id"], display_name))
            return self._payload(record)

        if record["status"] == "stopped" and record["desktop_id"]:
            await cloud_desktop_repo.update(record["id"], status="starting")
            record = {**record, "status": "starting"}
            self._spawn(user_id, self._start_flow(user_id, record["id"], record["desktop_id"]))
        return self._payload(record)

    async def resolve_ticket_target(self, user_id: str) -> tuple[str, str]:
        """(desktop_id, end_user_id) for a Running desktop, ownership verified.

        Raises DesktopNotReady with the status payload otherwise; a Stopped
        desktop is kicked awake first so the caller's 202 retry loop lands on
        a Running one eventually.
        """
        state = await self.status(user_id)
        if state["state"] == "stopped":
            state = await self.provision(user_id)
        channel_unready = (
            get_config().wuying_routing == "per_desktop"
            and state.get("channel", {}).get("state") != "up"
        )
        if state["state"] != "running" or channel_unready or not state.get("desktopId"):
            raise DesktopNotReady(state)
        desktop_id = state["desktopId"]
        try:
            eu_id = await wuying_ecd.verify_ownership(desktop_id, user_id)
        except wuying_ecd.DesktopOwnershipError:
            raise
        except Exception as e:
            # A desktop deleted behind our back fails here first — the
            # ownership tag lookup raises InvalidResourceId.NotFound before
            # GetConnectionTicket ever runs — so the ghost must be released
            # at this layer too, or the stale record wedges every ticket.
            if any(x in str(e) for x in ("NotFound", "InvalidDesktopId", "InvalidResourceId")):
                log.warning(f"Desktop {desktop_id} gone at ownership check; releasing ghost")
                try:
                    await self.release_ghost(user_id)
                except Exception as release_error:
                    # DeleteDesktops on an already-gone desktop also raises
                    # NotFound; the record is still cleared (finally block),
                    # so the caller must get the clean not_provisioned answer.
                    log.warning(f"Ghost release cleanup failed: {release_error}")
                raise DesktopNotReady({"state": "not_provisioned"})
            raise
        return desktop_id, eu_id

    async def release_ghost(self, user_id: str) -> None:
        """Hard-delete a desktop whose ticket API reports NotFound.

        DescribeDesktops may still list it (inconsistent ECD backends); delete
        frees it so the next provision() builds a clean one.
        """
        record = await cloud_desktop_repo.get_for_user(user_id)
        if not record or not record["desktop_id"]:
            return
        log.warning(f"Releasing ghost desktop {record['desktop_id']} for user {user_id}")
        try:
            await wuying_channel.revoke(record)
            await wuying_ecd.delete_desktop(record["desktop_id"])
        finally:
            await cloud_desktop_repo.soft_delete(record["id"])

    # -- fleet patrol -------------------------------------------------------

    def start_patrol(self, interval_sec: int = 300) -> None:
        """Periodic fleet sweep, ported from bossip's reaper in resident mode.

        Desktops are subscription-resident: idle-stopping them makes the next
        use eat a 1-2 minute cold start, so the sweep only *logs* the fleet
        (scoped to this environment's tag) for operability. Explicit stop /
        destroy stays with subscription-expiry logic, not idleness.
        """
        if self._patrol is not None:
            return
        self._patrol = asyncio.create_task(self._patrol_loop(interval_sec))

    def stop_patrol(self) -> None:
        if self._patrol is not None:
            self._patrol.cancel()
            self._patrol = None

    async def _patrol_loop(self, interval_sec: int) -> None:
        while True:
            await asyncio.sleep(interval_sec)
            try:
                desktops = await wuying_ecd.list_desktops()
                running = [d for d in desktops if d["status"] == "Running"]
                if desktops:
                    log.info(
                        f"ECD fleet: {len(running)}/{len(desktops)} Running "
                        f"(env={get_config().wuying_env_tag}, resident — no idle reaping)"
                    )
                if get_config().wuying_routing == "per_desktop":
                    for record in await cloud_desktop_repo.list_active():
                        if record["status"] == "running" and record.get("tunnel_state") != "revoked":
                            await wuying_channel.probe(record)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"ECD fleet patrol failed: {e}")

    # -- internals ----------------------------------------------------------

    def _payload(self, record: dict) -> dict:
        payload = {"state": record["status"]}
        if record.get("desktop_id"):
            payload["desktopId"] = record["desktop_id"]
        if record["status"] == "failed" and record.get("error"):
            payload["error"] = record["error"]
        if get_config().wuying_routing == "per_desktop":
            payload["channel"] = {
                "state": record.get("tunnel_state") or "pending",
                "last_seen_at": (
                    record["last_seen_at"].isoformat()
                    if hasattr(record.get("last_seen_at"), "isoformat")
                    else record.get("last_seen_at")
                ),
            }
            if record.get("channel_error"):
                payload["channel"]["error"] = record["channel_error"]
        return payload

    def _spawn(self, user_id: str, coro) -> None:
        task = asyncio.create_task(coro)
        self._inflight[user_id] = task
        task.add_done_callback(lambda _: self._inflight.pop(user_id, None))

    async def _create_flow(self, user_id: str, record_id: str, display_name: str | None) -> None:
        try:
            desktop_id = await wuying_ecd.create_desktop(user_id, display_name)
            await cloud_desktop_repo.update(
                record_id, desktop_id=desktop_id, end_user_id=wuying_ecd.eu_id_for(user_id)
            )
            await wuying_ecd.wait_desktop_ready(desktop_id)
        except Exception as e:
            log.error(f"Desktop provisioning failed for user {user_id}: {e}")
            await cloud_desktop_repo.update(record_id, status="failed", error=str(e)[:2000])
            return

        if get_config().wuying_routing == "per_desktop":
            try:
                record = await cloud_desktop_repo.get(record_id)
                if record:
                    record = await wuying_channel.install(record)
                    await wuying_channel.verify(record)
            except Exception as e:
                # The billable desktop already exists.  Keep recovering this
                # assignment instead of marking it failed: provision() treats
                # failed as permission to create a replacement desktop.
                log.warning("Desktop channel setup failed for user %s: %s", user_id, e)
                await cloud_desktop_repo.update(
                    record_id,
                    status="starting",
                    error=str(e)[:2000],
                    tunnel_state="down",
                    channel_error=str(e)[:2000],
                )
                return
        await cloud_desktop_repo.update(record_id, status="running", error=None)
        log.info(f"Desktop ready for user {user_id}: {desktop_id}")

    async def _start_flow(self, user_id: str, record_id: str, desktop_id: str) -> None:
        try:
            await wuying_ecd.start_desktop(desktop_id)
            await wuying_ecd.wait_desktop_ready(desktop_id)
        except Exception as e:
            log.error(f"Desktop start failed for user {user_id}: {e}")
            await cloud_desktop_repo.update(record_id, status="failed", error=str(e)[:2000])
            return

        if get_config().wuying_routing == "per_desktop":
            try:
                record = await cloud_desktop_repo.get(record_id)
                if record:
                    if not record.get("action_api_key_ciphertext"):
                        record = await wuying_channel.install(record)
                    await wuying_channel.verify(record)
            except Exception as e:
                log.warning("Desktop channel recovery failed after start for user %s: %s", user_id, e)
                await cloud_desktop_repo.update(
                    record_id,
                    status="starting",
                    error=str(e)[:2000],
                    tunnel_state="down",
                    channel_error=str(e)[:2000],
                )
                return
        await cloud_desktop_repo.update(record_id, status="running", error=None)

    async def _channel_flow(self, user_id: str, record_id: str) -> None:
        try:
            record = await cloud_desktop_repo.get(record_id)
            if not record:
                return
            if not record.get("action_api_key_ciphertext"):
                record = await wuying_channel.install(record)
            await wuying_channel.verify(record)
            await cloud_desktop_repo.update(record_id, status="running", error=None)
            log.info("Desktop channel recovered for user %s: %s", user_id, record.get("desktop_id"))
        except Exception as e:
            log.warning("Desktop channel recovery failed for user %s: %s", user_id, e)

    async def _adopt_from_tags(self, user_id: str) -> dict | None:
        """Recover a desktop the DB forgot: look it up by openbox-user tag.

        Filtered by the environment tag, so prod never adopts (or later reaps)
        a dev desktop when both share one Alibaba Cloud account.
        """
        try:
            desktops = await wuying_ecd.list_desktops(user_id=user_id)
        except Exception as e:
            log.warning(f"Tag-based desktop recovery failed for user {user_id}: {e}")
            return None
        live = [d for d in desktops if d["status"] not in ("Deleting", "Deleted", "Expired")]
        if not live:
            return None
        best = next((d for d in live if d["status"] == "Running"), live[0])
        status = _ECD_STATUS.get(best["status"], "starting")
        if status == "running" and get_config().wuying_routing == "per_desktop":
            status = "starting"
        record = await cloud_desktop_repo.create(
            user_id,
            region_id=get_config().wuying_region_id,
            status=status,
            desktop_id=best["desktop_id"],
            end_user_id=best["tags"].get(wuying_ecd.TAG_EU) or wuying_ecd.eu_id_for(user_id),
        )
        log.info(f"Adopted desktop {best['desktop_id']} for user {user_id} (status={status})")
        return record

    async def _resync(self, record: dict) -> dict:
        if not record["desktop_id"]:
            await cloud_desktop_repo.update(
                record["id"], status="failed", error="provisioning interrupted by restart"
            )
            return {**record, "status": "failed", "error": "provisioning interrupted by restart"}
        try:
            info = await wuying_ecd.describe_desktop(record["desktop_id"])
        except Exception as e:
            log.warning(f"Desktop resync failed for {record['desktop_id']}: {e}")
            return record
        if info is None:
            await cloud_desktop_repo.update(
                record["id"], status="failed", error="desktop disappeared during provisioning"
            )
            return {**record, "status": "failed"}
        status = _ECD_STATUS.get(info["status"], record["status"])
        needs_channel = (
            status == "running"
            and get_config().wuying_routing == "per_desktop"
            and record.get("tunnel_state") != "up"
        )
        persisted_status = "starting" if needs_channel else status
        if persisted_status != record["status"]:
            await cloud_desktop_repo.update(record["id"], status=persisted_status)
        refreshed = {**record, "status": persisted_status}
        if needs_channel:
            self._spawn(record["user_id"], self._channel_flow(record["user_id"], record["id"]))
        return refreshed


wuying_desktop_service = WuyingDesktopService()
