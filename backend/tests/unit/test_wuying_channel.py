"""Per-desktop channel crypto, allocation, routing, and revocation."""
import asyncio
import os
import pathlib
import subprocess
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from core.config import OpenBoxConfig
from db.repository.cloud_desktop_repo import PgCloudDesktopRepo, cloud_desktop_repo
from sandbox.channel import (
    ChannelConfigError,
    WuyingChannel,
    action_key_hash,
    decrypt_action_key,
    encrypt_action_key,
    parse_port_range,
)


KEY_A = "11" * 32
KEY_B = "22" * 32


def test_action_server_refuses_to_start_without_key():
    server = pathlib.Path(__file__).resolve().parents[3] / "container" / "action_server.py"
    env = dict(os.environ)
    env.pop("SESSION_API_KEY", None)
    result = subprocess.run(
        [sys.executable, str(server), "--port", "0"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "SESSION_API_KEY is required" in result.stderr


def test_action_key_encryption_roundtrip_is_randomized_and_versioned():
    one = encrypt_action_key("desktop-secret", KEY_A)
    two = encrypt_action_key("desktop-secret", KEY_A)
    assert one.startswith("v1:")
    assert two.startswith("v1:")
    assert one != two
    assert decrypt_action_key(one, KEY_A) == "desktop-secret"
    assert action_key_hash("desktop-secret") != action_key_hash("other")


def test_action_key_rejects_wrong_or_invalid_master_key():
    encrypted = encrypt_action_key("desktop-secret", KEY_A)
    with pytest.raises(ChannelConfigError):
        decrypt_action_key(encrypted, KEY_B)
    with pytest.raises(ChannelConfigError, match="32 bytes"):
        encrypt_action_key("x", "too-short")


def test_port_range_validation():
    assert parse_port_range("18100-18999") == (18100, 18999)
    with pytest.raises(ChannelConfigError):
        parse_port_range("18999-18100")
    with pytest.raises(ChannelConfigError):
        parse_port_range("not-a-range")


async def test_port_allocation_is_distinct_under_concurrency():
    first = await cloud_desktop_repo.create("channel-port-a", "cn-shanghai")
    second = await cloud_desktop_repo.create("channel-port-b", "cn-shanghai")
    ports = await asyncio.gather(
        cloud_desktop_repo.reserve_tunnel_port(first["id"], 18810, 18811),
        cloud_desktop_repo.reserve_tunnel_port(second["id"], 18810, 18811),
    )
    assert sorted(ports) == [18810, 18811]
    assert await cloud_desktop_repo.reserve_tunnel_port(first["id"], 18810, 18811) == ports[0]


async def test_port_allocation_retries_a_cross_worker_unique_conflict(monkeypatch):
    import db.repository.cloud_desktop_repo as repo_module

    attempts = 0

    class FakeScalars:
        def __init__(self, values):
            self.values = values

        def __iter__(self):
            return iter(self.values)

    class FakeResult:
        def __init__(self, values):
            self.values = values

        def scalars(self):
            return FakeScalars(self.values)

    class FakeSession:
        def __init__(self, attempt):
            self.attempt = attempt

        async def scalar(self, _statement):
            return SimpleNamespace(is_deleted=False, tunnel_port=None, updated_at=None)

        async def execute(self, _statement):
            return FakeResult([] if self.attempt == 1 else [18840])

        async def flush(self):
            if self.attempt == 1:
                raise IntegrityError("insert", {}, RuntimeError("unique clash"))

    @asynccontextmanager
    async def fake_db_session():
        nonlocal attempts
        attempts += 1
        yield FakeSession(attempts)

    monkeypatch.setattr(repo_module, "get_db_session", fake_db_session)
    port = await PgCloudDesktopRepo().reserve_tunnel_port("cld-conflict", 18840, 18841)
    assert port == 18841
    assert attempts == 2


async def test_provider_routes_two_owners_and_rejects_revoked(monkeypatch):
    import core.config as config_module
    import sandbox.channel as channel_module
    from sandbox.wuying import WuyingProvider
    from sandbox.wuying_desktop_service import DesktopNotReady

    cfg = OpenBoxConfig(
        wuying_routing="per_desktop",
        wuying_channel_key=KEY_A,
        wuying_api_key="legacy-must-not-be-used",
    )
    monkeypatch.setattr(config_module, "get_config", lambda: cfg)
    monkeypatch.setattr(channel_module, "get_config", lambda: cfg)
    one = await cloud_desktop_repo.create(
        "channel-owner-a",
        "cn-shanghai",
        status="running",
        desktop_id="ecd-channel-a",
        channel_kind="ssh",
        tunnel_port=18821,
        tunnel_bind="172.17.0.1",
        action_api_key_hash=action_key_hash("key-a"),
        action_api_key_ciphertext=encrypt_action_key("key-a", KEY_A),
        tunnel_state="up",
    )
    await cloud_desktop_repo.create(
        "channel-owner-b",
        "cn-shanghai",
        status="running",
        desktop_id="ecd-channel-b",
        channel_kind="ssh",
        tunnel_port=18822,
        tunnel_bind="172.17.0.1",
        action_api_key_hash=action_key_hash("key-b"),
        action_api_key_ciphertext=encrypt_action_key("key-b", KEY_A),
        tunnel_state="up",
    )

    provider = WuyingProvider()
    route_a = await provider.resolve_user_container("channel-owner-a")
    route_b = await provider.resolve_user_container("channel-owner-b")
    assert (route_a.id, route_a.port, route_a.api_key) == ("ecd-channel-a", 18821, "key-a")
    assert (route_b.id, route_b.port, route_b.api_key) == ("ecd-channel-b", 18822, "key-b")

    await cloud_desktop_repo.update(one["id"], tunnel_state="revoked")
    with pytest.raises(DesktopNotReady):
        await provider.resolve_user_container("channel-owner-a")


async def test_install_can_rotate_action_key(monkeypatch):
    import sandbox.channel as channel_module

    cfg = OpenBoxConfig(
        wuying_channel="direct",
        wuying_channel_key=KEY_A,
    )
    monkeypatch.setattr(channel_module, "get_config", lambda: cfg)

    async def describe(_desktop_id):
        return {"private_ip": "10.0.0.8"}

    commands = []

    async def run(_desktop_id, script, timeout=300):
        commands.append(script)
        return ""

    monkeypatch.setattr(channel_module.wuying_ecd, "describe_desktop", describe)
    monkeypatch.setattr(channel_module, "run_desktop_command", run)
    record = await cloud_desktop_repo.create(
        "channel-rotate",
        "cn-shanghai",
        status="running",
        desktop_id="ecd-channel-rotate",
    )
    installed = await channel_module.wuying_channel.install(record)
    first_key = decrypt_action_key(installed["action_api_key_ciphertext"], KEY_A)
    rotated = await channel_module.wuying_channel.install(installed, rotate_key=True)
    second_key = decrypt_action_key(rotated["action_api_key_ciphertext"], KEY_A)
    assert first_key != second_key
    assert rotated["action_api_key_hash"] == action_key_hash(second_key)
    assert len(commands) == 2


async def test_probe_uses_action_server_system_info_endpoint(monkeypatch):
    import sandbox.channel as channel_module

    cfg = OpenBoxConfig(wuying_channel_key=KEY_A)
    monkeypatch.setattr(channel_module, "get_config", lambda: cfg)
    requested = []
    updates = []

    class Response:
        status_code = 200

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **_kwargs):
            requested.append(url)
            return Response()

    async def update(record_id, **values):
        updates.append((record_id, values))

    monkeypatch.setattr(channel_module.httpx, "AsyncClient", lambda **_kwargs: Client())
    monkeypatch.setattr(channel_module.cloud_desktop_repo, "update", update)
    record = {
        "id": "cld-probe",
        "channel_kind": "ssh",
        "tunnel_bind": "172.17.0.1",
        "tunnel_port": 18850,
        "tunnel_state": "up",
        "action_api_key_ciphertext": encrypt_action_key("probe-key", KEY_A),
    }

    assert await WuyingChannel().probe(record) is True
    assert requested == ["http://172.17.0.1:18850/system_info"]
    assert updates[0][1]["tunnel_state"] == "up"
