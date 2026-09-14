"""The relay can only authorize a live, non-revoked desktop fingerprint."""

from core.config import OpenBoxConfig
from db.repository.cloud_desktop_repo import cloud_desktop_repo


async def test_tunnel_keys_auth_and_revocation(monkeypatch):
    import api.internal as internal

    monkeypatch.setattr(
        internal,
        "get_config",
        lambda: OpenBoxConfig(internal_api_token="internal-test-token"),
    )
    record = await cloud_desktop_repo.create(
        "authkeys-owner",
        "cn-shanghai",
        status="running",
        desktop_id="ecd-authkeys",
        channel_kind="ssh",
        tunnel_port=18831,
        tunnel_bind="172.17.0.1",
        tunnel_pubkey="ssh-ed25519 AAAATEST",
        tunnel_fingerprint="SHA256:authkeys-test-fingerprint",
        tunnel_state="up",
    )

    denied = await internal.tunnel_keys(
        fingerprint="SHA256:authkeys-test-fingerprint", x_internal_token="wrong"
    )
    assert denied.status_code == 403

    allowed = await internal.tunnel_keys(
        fingerprint="SHA256:authkeys-test-fingerprint",
        x_internal_token="internal-test-token",
    )
    assert allowed.status_code == 200
    assert b'permitlisten="172.17.0.1:18831"' in allowed.body
    assert b"ssh-ed25519 AAAATEST" in allowed.body

    await cloud_desktop_repo.update(record["id"], tunnel_state="revoked")
    revoked = await internal.tunnel_keys(
        fingerprint="SHA256:authkeys-test-fingerprint",
        x_internal_token="internal-test-token",
    )
    assert revoked.status_code == 200
    assert revoked.body == b""
