"""Pool state transitions are serialized and cloud calls are explicit."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.config import OpenBoxConfig
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from db.repository.user_repo import PgUserRepo
from sandbox import pool as pool_module
from sandbox.pool import (
    DestructiveApprovalRequired,
    LegacyGatewayReleaseRequired,
    PoolService,
    PoolStateError,
    STABLE_STATES,
    TRANSIENT_STATES,
    verify_prewarm,
)


def _config(**overrides):
    values = dict(
        pool_enabled=True,
        pool_assign_on_provision=True,
        pool_adopt_allowlist="ecd-allowed",
        wuying_image_id="img-v3",
        wuying_policy_group_id="pg-1080p",
        wuying_env_tag="prod",
        wuying_desktop_type="eds.enterprise_office.6c12g",
    )
    values.update(overrides)
    return OpenBoxConfig(**values)


async def test_verify_prewarm_rejects_source_image(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())

    async def describe(_desktop_id):
        return {
            "status": "Running",
            "image_id": "img-v2",
            "policy_group_id": "pg-1080p",
        }

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("tool verification must wait for the target image")

    monkeypatch.setattr(pool_module.wuying_ecd, "describe_desktop", describe)
    monkeypatch.setattr(pool_module, "run_desktop_command", must_not_run)
    with pytest.raises(PoolStateError, match="image is img-v2, expected img-v3"):
        await verify_prewarm("ecd-source")


async def _user(prefix: str):
    suffix = uuid.uuid4().hex[:10]
    return await PgUserRepo().create(
        id=f"{prefix}-{suffix}", username=f"{prefix}-{suffix}", password_hash="unused"
    )


async def _prewarm(desktop_id: str, expires_days: int = 30):
    return await cloud_desktop_repo.create(
        None,
        "cn-shanghai",
        status="running",
        desktop_id=desktop_id,
        pool_state="prewarm",
        expires_at=datetime.now(timezone.utc) + timedelta(days=expires_days),
        charge_type="PrePaid",
    )


def test_state_model_is_six_stable_plus_one_transient():
    assert STABLE_STATES == {
        "reserve", "prewarm", "assigned", "released", "recycling", "retired",
    }
    assert TRANSIENT_STATES == {"assigning"}


async def test_concurrent_claims_never_share_a_desktop(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())
    first, second = await _user("claim-a"), await _user("claim-b")
    suffix = uuid.uuid4().hex[:8]
    await _prewarm(f"ecd-claim-1-{suffix}")
    await _prewarm(f"ecd-claim-2-{suffix}")

    one, two = await asyncio.gather(
        PoolService().claim(first["default_workspace_id"], first["id"]),
        PoolService().claim(second["default_workspace_id"], second["id"]),
    )
    assert one and two
    assert one["desktop_id"] != two["desktop_id"]
    assert one["pool_state"] == two["pool_state"] == "assigning"


async def test_assign_claimed_completes_entitlement_tags_and_channel(monkeypatch):
    config = _config()
    monkeypatch.setattr(pool_module, "get_config", lambda: config)
    user = await _user("assign")
    source = await _prewarm(f"ecd-assign-{uuid.uuid4().hex[:8]}")
    service = PoolService()
    claimed = await service.claim(user["default_workspace_id"], user["id"])
    assert claimed and claimed["id"] == source["id"]

    calls = []

    async def ensure(workspace_id):
        calls.append(("ensure", workspace_id))
        return "eu-1", "password"

    async def entitlement(desktop_id, users):
        calls.append(("entitlement", desktop_id, users))

    async def tags(desktop_id, values):
        calls.append(("tags", desktop_id, values))

    async def install(record, rotate_key=False):
        calls.append(("install", record["desktop_id"], rotate_key))
        return record

    async def verify(record):
        calls.append(("verify", record["desktop_id"]))

    monkeypatch.setattr(pool_module.wuying_ecd, "ensure_end_user", ensure)
    monkeypatch.setattr(pool_module.wuying_ecd, "modify_entitlement", entitlement)
    monkeypatch.setattr(pool_module.wuying_ecd, "tag_desktop", tags)
    monkeypatch.setattr(pool_module.wuying_channel, "install", install)
    monkeypatch.setattr(pool_module.wuying_channel, "verify", verify)

    result = await service.assign_claimed(
        claimed, user["default_workspace_id"], user["id"]
    )
    assert result["pool_state"] == "assigned"
    assert result["workspace_id"] == user["default_workspace_id"]
    assert result["end_user_id"] == "eu-1"
    assert ("install", result["desktop_id"], True) in calls
    assert ("verify", result["desktop_id"]) in calls


async def test_failed_assignment_restores_prewarm(monkeypatch):
    config = _config()
    monkeypatch.setattr(pool_module, "get_config", lambda: config)
    user = await _user("assign-fail")
    source = await _prewarm(f"ecd-assign-fail-{uuid.uuid4().hex[:8]}")
    service = PoolService()
    claimed = await service.claim(user["default_workspace_id"], user["id"])

    async def ensure(_workspace_id):
        return "eu-1", "password"

    async def noop(*_args, **_kwargs):
        return None

    async def fail(_record, rotate_key=False):
        raise RuntimeError("channel install failed")

    monkeypatch.setattr(pool_module.wuying_ecd, "ensure_end_user", ensure)
    monkeypatch.setattr(pool_module.wuying_ecd, "modify_entitlement", noop)
    monkeypatch.setattr(pool_module.wuying_ecd, "tag_desktop", noop)
    monkeypatch.setattr(pool_module.wuying_ecd, "untag_desktop", noop)
    monkeypatch.setattr(pool_module.wuying_channel, "install", fail)

    with pytest.raises(RuntimeError, match="channel install failed"):
        await service.assign_claimed(claimed, user["default_workspace_id"], user["id"])
    restored = await cloud_desktop_repo.get(source["id"])
    assert restored["pool_state"] == "prewarm"
    assert restored["workspace_id"] is None


async def test_failed_assignment_with_uncleared_entitlement_is_quarantined(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())
    user = await _user("assign-quarantine")
    source = await _prewarm(f"ecd-assign-quarantine-{uuid.uuid4().hex[:8]}")
    service = PoolService()
    claimed = await service.claim(user["default_workspace_id"], user["id"])

    async def ensure(_workspace_id):
        return "eu-still-bound", "password"

    async def entitlement(_desktop_id, users):
        if not users:
            raise RuntimeError("empty entitlement rejected")

    async def noop(*_args, **_kwargs):
        return None

    async def fail(_record, rotate_key=False):
        raise RuntimeError("channel install failed")

    monkeypatch.setattr(pool_module.wuying_ecd, "ensure_end_user", ensure)
    monkeypatch.setattr(pool_module.wuying_ecd, "modify_entitlement", entitlement)
    monkeypatch.setattr(pool_module.wuying_ecd, "tag_desktop", noop)
    monkeypatch.setattr(pool_module.wuying_ecd, "untag_desktop", noop)
    monkeypatch.setattr(pool_module.wuying_channel, "install", fail)

    with pytest.raises(RuntimeError, match="channel install failed"):
        await service.assign_claimed(claimed, user["default_workspace_id"], user["id"])
    quarantined = await cloud_desktop_repo.get(source["id"])
    assert quarantined["pool_state"] == "released"
    assert quarantined["workspace_id"] is None
    assert quarantined["end_user_id"] == "eu-still-bound"
    assert "empty entitlement rejected" in quarantined["error"]


async def test_adopt_requires_allowlist_before_cloud_io(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())

    async def forbidden(_desktop_id):
        raise AssertionError("cloud must not be called")

    monkeypatch.setattr(pool_module.wuying_ecd, "describe_desktop", forbidden)
    with pytest.raises(PoolStateError, match="ALLOWLIST"):
        await PoolService().adopt("ecd-not-allowed", "reserve", "admin")


async def test_prewarm_adopt_requires_explicit_rebuild_approval(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())

    async def describe(_desktop_id):
        return {"desktop_id": "ecd-allowed", "status": "Running", "image_id": "img-old"}

    monkeypatch.setattr(pool_module.wuying_ecd, "describe_desktop", describe)
    with pytest.raises(DestructiveApprovalRequired, match="approve=true"):
        await PoolService().adopt("ecd-allowed", "prewarm", "admin")


@pytest.mark.parametrize(
    ("legacy_pool", "verified", "message"),
    [
        ("trial", True, "release its gateway registration first"),
        ("prewarm", False, "gateway_release_verified=true"),
    ],
)
async def test_legacy_adopt_requires_released_pool_and_gateway_verification(
    monkeypatch, legacy_pool, verified, message
):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())

    async def describe(_desktop_id):
        return {
            "desktop_id": "ecd-allowed",
            "status": "Running",
            "image_id": "img-v3",
            "desktop_type": "eds.enterprise_office.6c12g",
        }

    async def tags(_desktop_id):
        return {"codex-user": "slot15", "pool": legacy_pool}

    monkeypatch.setattr(pool_module.wuying_ecd, "describe_desktop", describe)
    monkeypatch.setattr(pool_module.wuying_ecd, "desktop_tags", tags)
    with pytest.raises(LegacyGatewayReleaseRequired, match=message):
        await PoolService().adopt(
            "ecd-allowed",
            "reserve",
            "admin",
            gateway_release_verified=verified,
        )


async def test_adopted_history_clears_stale_channel_credentials(monkeypatch):
    desktop_id = "ecd-allowed"
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())
    user = await _user("adopt-history")
    history = await cloud_desktop_repo.create(
        user["default_workspace_id"],
        "cn-shanghai",
        status="running",
        desktop_id=desktop_id,
        pool_state="assigned",
        channel_kind="ssh",
        tunnel_port=18991,
        tunnel_bind="172.17.0.1",
        tunnel_pubkey="ssh-ed25519 old",
        tunnel_fingerprint=f"SHA256:{uuid.uuid4().hex}",
        action_api_key_hash="old-hash",
        action_api_key_ciphertext="old-ciphertext",
        tunnel_state="up",
    )
    await cloud_desktop_repo.soft_delete(history["id"])

    async def describe(_desktop_id):
        return {
            "desktop_id": desktop_id,
            "status": "Running",
            "image_id": "img-v3",
            "desktop_type": "eds.enterprise_office.6c12g",
            "charge_type": "PrePaid",
            "end_user_ids": ["legacy-user"],
        }

    async def tags(_desktop_id):
        return {"purpose": "codex"}

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(pool_module.wuying_ecd, "describe_desktop", describe)
    monkeypatch.setattr(pool_module.wuying_ecd, "desktop_tags", tags)
    monkeypatch.setattr(pool_module.wuying_ecd, "untag_desktop", noop)
    monkeypatch.setattr(pool_module.wuying_ecd, "tag_desktop", noop)

    adopted = await PoolService().adopt(desktop_id, "reserve", "admin")
    assert adopted["pool_state"] == "reserve"
    assert adopted["workspace_id"] is None
    assert adopted["channel_kind"] is None
    assert adopted["tunnel_port"] is None
    assert adopted["tunnel_fingerprint"] is None
    assert adopted["action_api_key_ciphertext"] is None
    assert adopted["tunnel_state"] == "revoked"


async def test_retired_desktop_cannot_be_recycled(monkeypatch):
    monkeypatch.setattr(pool_module, "get_config", lambda: _config())
    record = await cloud_desktop_repo.create(
        None,
        "cn-shanghai",
        status="running",
        desktop_id=f"ecd-retired-{uuid.uuid4().hex[:8]}",
        pool_state="retired",
    )
    with pytest.raises(PoolStateError, match="not recyclable"):
        await PoolService().recycle(record["desktop_id"], "admin", approve=True)
