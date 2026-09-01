"""Per-user Wuying ECD provisioning: derived identity, service state machine,
ownership verification. All ECD/EDS calls are stubbed — no network."""
import asyncio

import pytest

from core.config import OpenBoxConfig
from sandbox import wuying_ecd
from sandbox.wuying_desktop_service import (
    DesktopNotReady,
    WuyingDesktopService,
)


class _Cfg(OpenBoxConfig):
    pass


def _config(**overrides) -> OpenBoxConfig:
    cfg = OpenBoxConfig()
    for key, value in overrides.items():
        object.__setattr__(cfg, key, value)
    return cfg


# ---------------------------------------------------------------------------
# Derived identity
# ---------------------------------------------------------------------------

def test_eu_id_is_stable_short_and_ascii():
    a = wuying_ecd.eu_id_for("usr_01J8FAKEULIDVALUE0000000000")
    b = wuying_ecd.eu_id_for("usr_01J8FAKEULIDVALUE0000000000")
    assert a == b
    assert a.startswith("obx-")
    # ECD EndUser ids max out at 32 chars; the hash form is always 20.
    assert len(a) == 20
    assert all(c in "0123456789abcdef-obx" for c in a)


def test_eu_id_differs_per_user():
    assert wuying_ecd.eu_id_for("user-a") != wuying_ecd.eu_id_for("user-b")


def test_password_requires_salt(monkeypatch):
    monkeypatch.setattr(wuying_ecd, "get_config", lambda: _config(wuying_password_salt=""))
    with pytest.raises(wuying_ecd.ProvisioningConfigError):
        wuying_ecd.password_for("user-a")


def test_password_stable_and_complex(monkeypatch):
    monkeypatch.setattr(wuying_ecd, "get_config", lambda: _config(wuying_password_salt="s3cret"))
    pwd = wuying_ecd.password_for("user-a")
    assert pwd == wuying_ecd.password_for("user-a")
    assert pwd != wuying_ecd.password_for("user-b")
    # Wuying wants 8-30 chars, >=3 classes (upper, lower, digit, special).
    assert 8 <= len(pwd) <= 30
    assert any(c.isupper() for c in pwd)
    assert any(c.islower() for c in pwd)
    assert "!" in pwd


def test_password_changes_with_salt(monkeypatch):
    monkeypatch.setattr(wuying_ecd, "get_config", lambda: _config(wuying_password_salt="a"))
    one = wuying_ecd.password_for("user-a")
    monkeypatch.setattr(wuying_ecd, "get_config", lambda: _config(wuying_password_salt="b"))
    assert wuying_ecd.password_for("user-a") != one


# ---------------------------------------------------------------------------
# Config surface
# ---------------------------------------------------------------------------

def test_provisioning_config_defaults():
    cfg = OpenBoxConfig()
    assert cfg.wuying_mode == "shared"
    assert cfg.wuying_image_id == ""
    assert cfg.wuying_desktop_type == "eds.enterprise_office.4c8g"
    assert cfg.wuying_system_disk_size == 40
    assert cfg.wuying_charge_type == "PostPaid"
    assert cfg.wuying_env_tag == "default"


# ---------------------------------------------------------------------------
# Ownership verification
# ---------------------------------------------------------------------------

async def test_verify_ownership_accepts_owner(monkeypatch):
    async def tags(_desktop_id):
        return {wuying_ecd.TAG_USER: "user-a", wuying_ecd.TAG_EU: "obx-abc"}

    monkeypatch.setattr(wuying_ecd, "desktop_tags", tags)
    assert await wuying_ecd.verify_ownership("ecd-1", "user-a") == "obx-abc"


async def test_verify_ownership_rejects_other_user(monkeypatch):
    async def tags(_desktop_id):
        return {wuying_ecd.TAG_USER: "user-b"}

    monkeypatch.setattr(wuying_ecd, "desktop_tags", tags)
    with pytest.raises(wuying_ecd.DesktopOwnershipError):
        await wuying_ecd.verify_ownership("ecd-1", "user-a")


async def test_verify_ownership_rejects_untagged(monkeypatch):
    async def tags(_desktop_id):
        return {}

    monkeypatch.setattr(wuying_ecd, "desktop_tags", tags)
    with pytest.raises(wuying_ecd.DesktopOwnershipError):
        await wuying_ecd.verify_ownership("ecd-1", "user-a")


# ---------------------------------------------------------------------------
# EndUser cleanup retry (desktop teardown propagates slowly)
# ---------------------------------------------------------------------------

async def test_end_user_cleanup_retries_until_released(monkeypatch):
    calls = []

    async def remove(ids):
        calls.append(ids)
        if len(calls) < 3:
            raise RuntimeError("RemoveUsers failed: obx-x: Used in some region")
        return ids

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(wuying_ecd, "remove_openbox_end_users", remove)
    monkeypatch.setattr(wuying_ecd.asyncio, "sleep", no_sleep)
    await wuying_ecd._remove_end_users_with_retry(["obx-x"], attempts=6, delay_sec=0)
    assert len(calls) == 3


async def test_end_user_cleanup_gives_up_quietly(monkeypatch):
    async def remove(ids):
        raise RuntimeError("RemoveUsers failed: obx-x: Used in some region")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(wuying_ecd, "remove_openbox_end_users", remove)
    monkeypatch.setattr(wuying_ecd.asyncio, "sleep", no_sleep)
    # Must not raise — cleanup failure is logged, never propagated to callers.
    await wuying_ecd._remove_end_users_with_retry(["obx-x"], attempts=2, delay_sec=0)


# ---------------------------------------------------------------------------
# Service state machine (stubbed ECD)
# ---------------------------------------------------------------------------

def _stub_ecd(monkeypatch, service_module="sandbox.wuying_desktop_service", **behaviour):
    """Install no-network stand-ins for every wuying_ecd call the service makes."""
    import sandbox.wuying_desktop_service as svc_mod

    async def create_desktop(user_id, display_name=None):
        return behaviour.get("desktop_id", "ecd-new")

    async def wait_desktop_ready(desktop_id, timeout_sec=360, poll_interval=5):
        if behaviour.get("wait_fails"):
            raise RuntimeError("Desktop provisioning failed: Failed")

    async def start_desktop(desktop_id):
        return None

    async def list_desktops(user_id=None):
        return behaviour.get("cloud_list", [])

    async def describe_desktop(desktop_id):
        return behaviour.get("describe")

    async def delete_desktop(desktop_id):
        behaviour.setdefault("deleted", []).append(desktop_id)

    monkeypatch.setattr(wuying_ecd, "create_desktop", create_desktop)
    monkeypatch.setattr(wuying_ecd, "wait_desktop_ready", wait_desktop_ready)
    monkeypatch.setattr(wuying_ecd, "start_desktop", start_desktop)
    monkeypatch.setattr(wuying_ecd, "list_desktops", list_desktops)
    monkeypatch.setattr(wuying_ecd, "describe_desktop", describe_desktop)
    monkeypatch.setattr(wuying_ecd, "delete_desktop", delete_desktop)
    monkeypatch.setattr(svc_mod, "get_config", lambda: _config(wuying_password_salt="s"))
    return behaviour


async def _drain(service: WuyingDesktopService, user_id: str):
    task = service._inflight.get(user_id)
    if task:
        await task


async def test_provision_creates_and_reaches_running(monkeypatch):
    _stub_ecd(monkeypatch)
    service = WuyingDesktopService()
    user = "user-prov-1"

    state = await service.provision(user)
    assert state["state"] == "creating"
    await _drain(service, user)

    state = await service.status(user)
    assert state["state"] == "running"
    assert state["desktopId"] == "ecd-new"


async def test_provision_is_idempotent_while_inflight(monkeypatch):
    _stub_ecd(monkeypatch)
    service = WuyingDesktopService()
    user = "user-prov-2"

    first = await service.provision(user)
    second = await service.provision(user)
    assert first["state"] == second["state"] == "creating"
    await _drain(service, user)
    # Only one live record — the unique index would also enforce this.
    from db.repository.cloud_desktop_repo import cloud_desktop_repo
    record = await cloud_desktop_repo.get_for_user(user)
    assert record["desktop_id"] == "ecd-new"


async def test_failed_create_reports_failed_then_reprovisions(monkeypatch):
    behaviour = _stub_ecd(monkeypatch, wait_fails=True)
    service = WuyingDesktopService()
    user = "user-prov-3"

    await service.provision(user)
    await _drain(service, user)
    state = await service.status(user)
    assert state["state"] == "failed"
    assert "Failed" in state["error"]

    behaviour["wait_fails"] = False
    await service.provision(user)
    await _drain(service, user)
    assert (await service.status(user))["state"] == "running"


async def test_status_not_provisioned_without_desktop(monkeypatch):
    _stub_ecd(monkeypatch)
    service = WuyingDesktopService()
    assert (await service.status("user-none"))["state"] == "not_provisioned"


async def test_status_adopts_tagged_desktop(monkeypatch):
    _stub_ecd(
        monkeypatch,
        cloud_list=[
            {
                "desktop_id": "ecd-found",
                "status": "Running",
                "tags": {wuying_ecd.TAG_EU: "obx-found"},
                "end_user_ids": ["obx-found"],
            }
        ],
    )
    service = WuyingDesktopService()
    state = await service.status("user-adopt")
    assert state == {"state": "running", "desktopId": "ecd-found"}


async def test_ticket_target_pending_while_creating(monkeypatch):
    _stub_ecd(monkeypatch)
    service = WuyingDesktopService()
    user = "user-ticket-1"

    await service.provision(user)
    with pytest.raises(DesktopNotReady) as excinfo:
        await service.resolve_ticket_target(user)
    assert excinfo.value.payload["state"] == "creating"
    await _drain(service, user)


async def test_ticket_target_running_verifies_ownership(monkeypatch):
    _stub_ecd(monkeypatch)

    async def verify(desktop_id, user_id):
        assert desktop_id == "ecd-new"
        return "obx-verified"

    monkeypatch.setattr(wuying_ecd, "verify_ownership", verify)
    service = WuyingDesktopService()
    user = "user-ticket-2"

    await service.provision(user)
    await _drain(service, user)
    assert await service.resolve_ticket_target(user) == ("ecd-new", "obx-verified")


async def test_stopped_desktop_wakes_on_ticket(monkeypatch):
    _stub_ecd(monkeypatch)
    service = WuyingDesktopService()
    user = "user-ticket-3"

    await service.provision(user)
    await _drain(service, user)
    from db.repository.cloud_desktop_repo import cloud_desktop_repo
    record = await cloud_desktop_repo.get_for_user(user)
    await cloud_desktop_repo.update(record["id"], status="stopped")

    with pytest.raises(DesktopNotReady) as excinfo:
        await service.resolve_ticket_target(user)
    assert excinfo.value.payload["state"] == "starting"
    await _drain(service, user)
    assert (await service.status(user))["state"] == "running"


async def test_ticket_target_releases_ghost_when_tags_report_not_found(monkeypatch):
    behaviour = _stub_ecd(monkeypatch)

    async def verify(desktop_id, user_id):
        raise RuntimeError(
            "Error: InvalidResourceId.NotFound code: 400, ResourceId [[ecd-new]] is not found."
        )

    monkeypatch.setattr(wuying_ecd, "verify_ownership", verify)
    service = WuyingDesktopService()
    user = "user-ghost-tags"

    await service.provision(user)
    await _drain(service, user)
    with pytest.raises(DesktopNotReady) as excinfo:
        await service.resolve_ticket_target(user)
    assert excinfo.value.payload == {"state": "not_provisioned"}
    assert behaviour["deleted"] == ["ecd-new"]
    # The stale record is gone, so the user can provision a fresh desktop.
    assert (await service.status(user))["state"] == "not_provisioned"


async def test_release_ghost_deletes_and_forgets(monkeypatch):
    behaviour = _stub_ecd(monkeypatch)
    service = WuyingDesktopService()
    user = "user-ghost"

    await service.provision(user)
    await _drain(service, user)
    await service.release_ghost(user)
    assert behaviour["deleted"] == ["ecd-new"]
    assert (await service.status(user))["state"] == "not_provisioned"
