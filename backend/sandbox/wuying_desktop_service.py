"""Per-workspace cloud desktop orchestration (wuying_mode="per_user").

Ports the ensure-desktop skeleton of bossip's wuying-cloud.service: check the
local record, recover by ECD tag when the record is gone, start a Stopped
desktop, create one only when nothing exists — all behind a per-user in-flight
guard so members firing several requests in the same second cannot race-create
multiple (billable) desktops.

Provisioning takes 2-3 minutes, so it always runs as a background task and the
API reports progress; callers poll ``status()``. The unique partial index on
cloud_desktops(workspace_id) is the cross-worker backstop: two workers that both
miss the in-process guard cannot both insert a live record.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

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


def _parse_expired_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        log.warning("Could not parse ECD expired_time=%r", value)
        return None


class WuyingDesktopService:
    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Task] = {}
        self._patrol: asyncio.Task | None = None

    # -- public API ---------------------------------------------------------

    async def status(self, workspace_id: str) -> dict:
        """Current provisioning state for this workspace.

        States: not_provisioned | creating | starting | running | stopped | failed.
        """
        record = await cloud_desktop_repo.get_for_workspace(workspace_id)
        if record is None:
            record = await self._adopt_from_tags(workspace_id)
            if record is None:
                return {"state": "not_provisioned"}

        if workspace_id in self._inflight:
            return self._payload(record)

        if record.get("pool_state") == "assigning":
            self._spawn(
                workspace_id,
                self._assign_pool_flow(
                    record, workspace_id, record.get("user_id")
                ),
            )
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
            self._spawn(workspace_id, self._channel_flow(workspace_id, record["id"]))
        return self._payload(record)

    async def provision(
        self,
        workspace_id: str,
        triggered_by_user_id: str | None = None,
        display_name: str | None = None,
    ) -> dict:
        """Idempotent kick: create or start this workspace's desktop as needed."""
        record = await cloud_desktop_repo.get_for_workspace(workspace_id)
        if record is None:
            record = await self._adopt_from_tags(workspace_id)

        if workspace_id in self._inflight:
            return self._payload(record) if record else {"state": "creating"}

        if record is None or record["status"] == "failed":
            if record is not None:
                await cloud_desktop_repo.soft_delete(record["id"])
            from sandbox.pool import pool_service

            claimed = await pool_service.claim(workspace_id, triggered_by_user_id)
            if claimed is not None:
                if claimed.get("pool_state") == "assigning":
                    self._spawn(
                        workspace_id,
                        self._assign_pool_flow(
                            claimed, workspace_id, triggered_by_user_id
                        ),
                    )
                # Another worker may have assigned/created the workspace row
                # between the initial read and the pool claim transaction.
                # In that case return the row it found instead of attempting a
                # second billable desktop and relying on a uniqueness failure.
                return self._payload(claimed)
            record = await cloud_desktop_repo.create(
                workspace_id,
                region_id=get_config().wuying_region_id,
                status="creating",
                user_id=triggered_by_user_id,
                charge_type=get_config().wuying_charge_type,
            )
            if triggered_by_user_id:
                from audit import record as audit_record

                await audit_record(
                    triggered_by_user_id,
                    workspace_id,
                    "desktop.provision",
                    "cloud_desktop",
                    record["id"],
                )
            self._spawn(
                workspace_id,
                self._create_flow(workspace_id, record["id"], display_name),
            )
            return self._payload(record)

        if record["status"] == "stopped" and record["desktop_id"]:
            await cloud_desktop_repo.update(record["id"], status="starting")
            record = {**record, "status": "starting"}
            self._spawn(
                workspace_id,
                self._start_flow(workspace_id, record["id"], record["desktop_id"]),
            )
        return self._payload(record)

    async def resolve_ticket_target(self, workspace_id: str) -> tuple[str, str]:
        """(desktop_id, end_user_id) for a Running desktop, ownership verified.

        Raises DesktopNotReady with the status payload otherwise; a Stopped
        desktop is kicked awake first so the caller's 202 retry loop lands on
        a Running one eventually.
        """
        state = await self.status(workspace_id)
        if state["state"] == "stopped":
            state = await self.provision(workspace_id)
        channel_unready = (
            get_config().wuying_routing == "per_desktop"
            and state.get("channel", {}).get("state") != "up"
        )
        if state["state"] != "running" or channel_unready or not state.get("desktopId"):
            raise DesktopNotReady(state)
        desktop_id = state["desktopId"]
        try:
            eu_id = await wuying_ecd.verify_ownership(desktop_id, workspace_id)
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
                    await self.release_ghost(workspace_id)
                except Exception as release_error:
                    # DeleteDesktops on an already-gone desktop also raises
                    # NotFound; the record is still cleared (finally block),
                    # so the caller must get the clean not_provisioned answer.
                    log.warning(f"Ghost release cleanup failed: {release_error}")
                raise DesktopNotReady({"state": "not_provisioned"})
            raise
        return desktop_id, eu_id

    async def release_ghost(
        self, workspace_id: str, actor_user_id: str | None = None
    ) -> None:
        """Release a ghost assignment without destroying prepaid value."""
        record = await cloud_desktop_repo.get_for_workspace(workspace_id)
        if not record or not record["desktop_id"]:
            return
        log.warning(
            f"Releasing ghost desktop {record['desktop_id']} for workspace {workspace_id}"
        )
        charge_type = record.get("charge_type")
        stored_expiry = record.get("expires_at")
        expired_time = (
            stored_expiry.isoformat()
            if isinstance(stored_expiry, datetime)
            else stored_expiry
        )
        if not charge_type:
            try:
                remote = await wuying_ecd.describe_desktop(record["desktop_id"])
            except Exception as exc:
                log.warning("Could not classify ghost %s: %s", record["desktop_id"], exc)
                remote = None
            if remote:
                charge_type = remote.get("charge_type")
                expired_time = remote.get("expired_time")
        try:
            await wuying_channel.revoke(record)
            audit_actor = actor_user_id or record.get("user_id")
            if audit_actor:
                from audit import record as audit_record

                await audit_record(
                    audit_actor,
                    workspace_id,
                    "desktop.revoke",
                    "cloud_desktop",
                    record["desktop_id"],
                )
            # Hard deletion is allowed only for a positively identified
            # pay-as-you-go desktop. Unknown is deliberately non-destructive.
            if charge_type == "PostPaid":
                await wuying_ecd.delete_desktop(record["desktop_id"])
            else:
                await cloud_desktop_repo.update(
                    record["id"], status="reclaimed", error="ghost"
                )
        finally:
            await cloud_desktop_repo.soft_delete(record["id"])
            audit_actor = actor_user_id or record.get("user_id")
            if audit_actor:
                from audit import record as audit_record

                await audit_record(
                    audit_actor,
                    workspace_id,
                    "desktop.ghost",
                    "cloud_desktop",
                    record["desktop_id"],
                    {
                        "charge_type": charge_type or "Unknown",
                        "expired_time": expired_time,
                    },
                )
            log.error(
                "Ghost desktop reclaimed: desktop_id=%s workspace_id=%s charge_type=%s",
                record["desktop_id"],
                workspace_id,
                charge_type or "Unknown",
            )

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
                by_id = {d["desktop_id"]: d for d in desktops}
                active_records = await cloud_desktop_repo.list_active()
                for record in active_records:
                    remote = by_id.get(record.get("desktop_id"))
                    if not remote:
                        continue
                    await cloud_desktop_repo.update(
                        record["id"],
                        charge_type=remote.get("charge_type") or record.get("charge_type"),
                        expires_at=_parse_expired_time(remote.get("expired_time")),
                    )
                if get_config().wuying_routing == "per_desktop":
                    for record in active_records:
                        if record["status"] == "running" and record.get("tunnel_state") != "revoked":
                            await wuying_channel.probe(record)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"ECD fleet patrol failed: {e}")

    # -- internals ----------------------------------------------------------

    def _payload(self, record: dict) -> dict:
        state = "assigning" if record.get("pool_state") == "assigning" else record["status"]
        payload = {"state": state}
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

    def _spawn(self, workspace_id: str, coro) -> None:
        task = asyncio.create_task(coro)
        self._inflight[workspace_id] = task
        task.add_done_callback(lambda _: self._inflight.pop(workspace_id, None))

    async def _create_flow(
        self, workspace_id: str, record_id: str, display_name: str | None
    ) -> None:
        try:
            desktop_id = await wuying_ecd.create_desktop(workspace_id, display_name)
            await cloud_desktop_repo.update(
                record_id,
                desktop_id=desktop_id,
                end_user_id=wuying_ecd.eu_id_for(workspace_id),
            )
            await wuying_ecd.wait_desktop_ready(desktop_id)
            info = await wuying_ecd.describe_desktop(desktop_id)
            if info:
                await cloud_desktop_repo.update(
                    record_id,
                    charge_type=info.get("charge_type") or get_config().wuying_charge_type,
                    expires_at=_parse_expired_time(info.get("expired_time")),
                    spec=info.get("desktop_type") or get_config().wuying_desktop_type,
                    golden_image_id=info.get("image_id") or get_config().wuying_image_id,
                )
        except Exception as e:
            log.error(f"Desktop provisioning failed for workspace {workspace_id}: {e}")
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
                log.warning("Desktop channel setup failed for workspace %s: %s", workspace_id, e)
                await cloud_desktop_repo.update(
                    record_id,
                    status="starting",
                    error=str(e)[:2000],
                    tunnel_state="down",
                    channel_error=str(e)[:2000],
                )
                return
        await cloud_desktop_repo.update(record_id, status="running", error=None)
        log.info(f"Desktop ready for workspace {workspace_id}: {desktop_id}")

    async def _assign_pool_flow(
        self,
        record: dict,
        workspace_id: str,
        triggered_by_user_id: str | None,
    ) -> None:
        from sandbox.pool import pool_service

        try:
            assigned = await pool_service.assign_claimed(
                record, workspace_id, triggered_by_user_id
            )
            log.info(
                "Pool desktop ready for workspace %s: %s",
                workspace_id,
                assigned.get("desktop_id"),
            )
        except Exception as exc:
            # assign_claimed restores the desktop to prewarm. The next explicit
            # provision request may retry or use the existing create fallback.
            log.error("Pool assignment failed for workspace %s: %s", workspace_id, exc)

    async def _start_flow(
        self, workspace_id: str, record_id: str, desktop_id: str
    ) -> None:
        try:
            await wuying_ecd.start_desktop(desktop_id)
            await wuying_ecd.wait_desktop_ready(desktop_id)
        except Exception as e:
            log.error(f"Desktop start failed for workspace {workspace_id}: {e}")
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
                log.warning(
                    "Desktop channel recovery failed after start for workspace %s: %s",
                    workspace_id,
                    e,
                )
                await cloud_desktop_repo.update(
                    record_id,
                    status="starting",
                    error=str(e)[:2000],
                    tunnel_state="down",
                    channel_error=str(e)[:2000],
                )
                return
        await cloud_desktop_repo.update(record_id, status="running", error=None)

    async def _channel_flow(self, workspace_id: str, record_id: str) -> None:
        try:
            record = await cloud_desktop_repo.get(record_id)
            if not record:
                return
            if not record.get("action_api_key_ciphertext"):
                record = await wuying_channel.install(record)
            await wuying_channel.verify(record)
            await cloud_desktop_repo.update(record_id, status="running", error=None)
            log.info(
                "Desktop channel recovered for workspace %s: %s",
                workspace_id,
                record.get("desktop_id"),
            )
        except Exception as e:
            log.warning("Desktop channel recovery failed for workspace %s: %s", workspace_id, e)

    async def _adopt_from_tags(self, workspace_id: str) -> dict | None:
        """Recover a desktop the DB forgot: look it up by workspace ownership tag.

        Filtered by the environment tag, so prod never adopts (or later reaps)
        a dev desktop when both share one Alibaba Cloud account.
        """
        try:
            desktops = await wuying_ecd.list_desktops(user_id=workspace_id)
        except Exception as e:
            log.warning(
                f"Tag-based desktop recovery failed for workspace {workspace_id}: {e}"
            )
            return None
        live = [d for d in desktops if d["status"] not in ("Deleting", "Deleted", "Expired")]
        if not live:
            return None
        best = next((d for d in live if d["status"] == "Running"), live[0])
        status = _ECD_STATUS.get(best["status"], "starting")
        if status == "running" and get_config().wuying_routing == "per_desktop":
            status = "starting"
        record = await cloud_desktop_repo.create(
            workspace_id,
            region_id=get_config().wuying_region_id,
            status=status,
            desktop_id=best["desktop_id"],
            end_user_id=best["tags"].get(wuying_ecd.TAG_EU)
            or wuying_ecd.eu_id_for(workspace_id),
            charge_type=best.get("charge_type"),
            expires_at=_parse_expired_time(best.get("expired_time")),
        )
        log.info(
            f"Adopted desktop {best['desktop_id']} for workspace {workspace_id} "
            f"(status={status})"
        )
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
        update_fields = {
            "status": persisted_status,
            "charge_type": info.get("charge_type") or record.get("charge_type"),
            "expires_at": _parse_expired_time(info.get("expired_time")),
        }
        await cloud_desktop_repo.update(record["id"], **update_fields)
        refreshed = {**record, **update_fields}
        if needs_channel:
            self._spawn(
                record["workspace_id"],
                self._channel_flow(record["workspace_id"], record["id"]),
            )
        return refreshed


wuying_desktop_service = WuyingDesktopService()
