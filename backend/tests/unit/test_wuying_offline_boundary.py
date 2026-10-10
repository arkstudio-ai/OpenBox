"""Default pytest enforces this boundary without an extra plugin or CLI flag."""
import asyncio
import base64
import importlib
import io
import socket
import ssl
from types import SimpleNamespace
from uuid import uuid4

import aiohttp
import httpx
import pytest
import requests
from alibabacloud_ecd20200930.client import Client as EcdClient
from alibabacloud_ecd20200930.models import RunCommandRequest
from alibabacloud_tea_openapi.models import Config
from Tea.core import TeaCore
from yarl import URL

from sandbox import channel, wuying_ecd
from tests.offline_wuying import OfflineWuyingAccess


# Created during collection, before the per-item guard; no network or private
# credentials are involved. Factory-only protection would miss these clients.
_CACHED_SDK = EcdClient(Config(access_key_id="offline-fixture",
    access_key_secret="offline-fixture", endpoint="offline-wuying.invalid"))
_CACHED_REQUESTS = requests.Session()


@pytest.fixture
def guard_in_fixture_teardown(monkeypatch):
    yield
    with pytest.raises(OfflineWuyingAccess, match="Unstubbed Wuying SDK"):
        wuying_ecd.eds_user_client()


def test_guard_surrounds_shared_monkeypatch_and_fixture_teardown(monkeypatch, guard_in_fixture_teardown):
    local = object()
    monkeypatch.setattr(wuying_ecd, "ecd_client", lambda: local)
    assert wuying_ecd.ecd_client() is local


async def test_missing_run_command_stub_is_refused_before_credentials(monkeypatch):
    def no_credentials():
        raise AssertionError("The automatic SDK guard was missing")

    monkeypatch.setattr(wuying_ecd, "load_credentials", no_credentials)
    # Deliberately do not stub RunCommand or either SDK factory.
    with pytest.raises(OfflineWuyingAccess, match="Unstubbed Wuying SDK"):
        await channel.run_desktop_command("ecd-offline-fixture", "true", timeout=1)


async def test_missing_run_command_stub_in_real_pool_compensation_is_not_swallowed(monkeypatch):
    from db.repository.cloud_desktop_repo import cloud_desktop_repo
    from sandbox.pool import PoolService
    from tests.unit.desktop_test_support import desktop_workspace

    workspace = await desktop_workspace("offline-compensation-" + uuid4().hex[:12])
    record = await cloud_desktop_repo.create(workspace, "cn-offline-fixture", "running",
        desktop_id="ecd-offline-compensation", pool_state="assigning",
        channel_kind="ssh", tunnel_state="up")

    async def ensure(*args, **kwargs):
        return "obx-fixture", "unused"

    async def local_noop(*args, **kwargs):
        return None

    async def failed_install(*args, **kwargs):
        raise RuntimeError("fixture installation failed")

    def no_credentials():
        raise AssertionError("The automatic SDK guard was missing")

    monkeypatch.setattr(wuying_ecd, "ensure_end_user", ensure)
    monkeypatch.setattr(wuying_ecd, "modify_entitlement", local_noop)
    monkeypatch.setattr(wuying_ecd, "tag_desktop", local_noop)
    monkeypatch.setattr(wuying_ecd, "load_credentials", no_credentials)
    monkeypatch.setattr(channel.wuying_channel, "install", failed_install)
    # The real assign_claimed -> revoke -> RunCommand compensation is left
    # unstubbed. Its broad cleanup catches must not hide a boundary violation.
    try:
        with pytest.raises(OfflineWuyingAccess, match="Unstubbed Wuying SDK"):
            await PoolService().assign_claimed(record, workspace, None)
        assert (await cloud_desktop_repo.get(record["id"]))["tunnel_state"] == "revoked"
    finally:
        await cloud_desktop_repo.update(record["id"], pool_state="retired", workspace_id=None)


@pytest.mark.parametrize("package", ["alibabacloud_ecd20200930", "alibabacloud_eds_user20210308",
                                     "alibabacloud_bssopenapi20171214"])
def test_direct_sdk_factory_is_refused(package):
    with pytest.raises(OfflineWuyingAccess, match="Unstubbed Wuying SDK"):
        importlib.import_module(package + ".client").Client(None)


async def test_explicit_local_sdk_runs_real_command_wrapper(monkeypatch):
    calls = []

    async def submit(request):
        calls.append(("run", list(request.desktop_id)))
        return SimpleNamespace(body=SimpleNamespace(invoke_id="local-invocation"))

    async def poll(request):
        calls.append(("poll", request.invoke_id))
        target = SimpleNamespace(invocation_status="Success", exit_code=0,
            output=base64.b64encode(b"local fixture output").decode())
        return SimpleNamespace(body=SimpleNamespace(invocations=[SimpleNamespace(invoke_desktops=[target])]))

    monkeypatch.setattr(wuying_ecd, "ecd_client", lambda: SimpleNamespace(
        run_command_async=submit, describe_invocations_async=poll))
    assert await channel.run_desktop_command("ecd-offline-fixture", "true") == "local fixture output"
    assert calls == [("run", ["ecd-offline-fixture"]), ("poll", "local-invocation")]


@pytest.mark.parametrize("lookup", ["getaddrinfo", "gethostbyname", "gethostbyname_ex"])
def test_external_dns_is_refused(lookup):
    # A numeric documentation address makes even a missing guard test safe:
    # the OS does not need to send an actual DNS query to resolve it.
    args = ("203.0.113.10", 443) if lookup == "getaddrinfo" else ("203.0.113.10",)
    with pytest.raises(OfflineWuyingAccess, match="DNS destination"):
        getattr(socket, lookup)(*args)


@pytest.mark.parametrize("operation", ["connect", "connect_ex", "sendto"])
def test_external_socket_is_refused_before_os_call(operation):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if operation == "sendto" else socket.SOCK_STREAM)
    sock.close()  # No actual network is possible even if the automatic guard regresses.
    with pytest.raises(OfflineWuyingAccess, match="socket connection|datagram destination"):
        if operation == "sendto":
            sock.sendto(b"fixture", ("203.0.113.10", 443))
        else:
            getattr(sock, operation)(("203.0.113.10", 443))


@pytest.mark.parametrize("operation", ["send", "sendall", "sendfile"] +
    (["sendmsg"] if hasattr(socket.socket, "sendmsg") else []))
def test_reused_socket_write_is_refused_before_os_call(monkeypatch, operation):
    sock = socket.socket()
    sock.close()  # No packet is possible even if the write guard is removed.
    payload = io.BytesIO(b"fixture") if operation == "sendfile" else b"fixture"
    if operation == "sendmsg":
        payload = [payload]
    original_peer = socket.socket.getpeername
    with monkeypatch.context() as patch:
        patch.setattr(socket.socket, "getpeername", lambda self:
            ("203.0.113.10", 443) if self is sock else original_peer(self))
        with pytest.raises(OfflineWuyingAccess, match="connected socket peer"):
            getattr(sock, operation)(payload)


@pytest.mark.parametrize("operation", ["send", "sendall", "write"])
def test_reused_ssl_write_is_refused_before_ssl_call(monkeypatch, operation):
    sock = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).wrap_socket(socket.socket(),
        server_hostname="offline-wuying.invalid", do_handshake_on_connect=False)
    sock.close()  # Closed before the synthetic peer is installed; cannot send.
    original_peer = socket.socket.getpeername
    with monkeypatch.context() as patch:
        patch.setattr(socket.socket, "getpeername", lambda self:
            ("203.0.113.10", 443) if self is sock else original_peer(self))
        with pytest.raises(OfflineWuyingAccess, match="connected socket peer"):
            getattr(sock, operation)(b"fixture")


def test_preexisting_sdk_sync_dispatch_is_refused_before_cached_session(monkeypatch):
    def never_send(*args, **kwargs):
        raise AssertionError("The automatic SDK dispatch guard was missing")

    monkeypatch.setattr(TeaCore, "_get_session", lambda **kwargs: SimpleNamespace(send=never_send))
    request = RunCommandRequest(desktop_id=["ecd-offline-fixture"], command_content="true")
    with pytest.raises(OfflineWuyingAccess, match="SDK destination"):
        _CACHED_SDK.run_command(request)


async def test_preexisting_sdk_async_dispatch_is_refused_before_session(monkeypatch):
    def never_session(*args, **kwargs):
        raise AssertionError("The automatic SDK dispatch guard was missing")

    monkeypatch.setattr(aiohttp, "ClientSession", never_session)
    request = RunCommandRequest(desktop_id=["ecd-offline-fixture"], command_content="true")
    with pytest.raises(OfflineWuyingAccess, match="SDK destination"):
        await _CACHED_SDK.run_command_async(request)


def test_preexisting_requests_adapter_refuses_external_target_before_pool(monkeypatch):
    def never_connection(*args, **kwargs):
        raise AssertionError("The automatic requests dispatch guard was missing")

    adapter = _CACHED_REQUESTS.get_adapter("https://offline-wuying.invalid")
    monkeypatch.setattr(adapter, "get_connection_with_tls_context", never_connection)
    with pytest.raises(OfflineWuyingAccess, match="HTTP destination"):
        _CACHED_REQUESTS.get("https://offline-wuying.invalid/no-send")


async def test_aiohttp_dispatch_refuses_external_target_before_reused_connection():
    request = aiohttp.ClientRequest("GET", URL("https://offline-wuying.invalid/no-send"))
    # An inert connection cannot send even without the guard. This exercises
    # per-request dispatch, which aiohttp also uses for an existing pooled socket.
    connection = SimpleNamespace(protocol=None)
    with pytest.raises(OfflineWuyingAccess, match="HTTP destination"):
        await request.send(connection)


def test_real_http_transport_refuses_external_target_before_pool(monkeypatch):
    def never_send(*args, **kwargs):
        raise AssertionError("The automatic HTTP guard was missing")

    transport = httpx.HTTPTransport()
    monkeypatch.setattr(transport._pool, "handle_request", never_send)
    with httpx.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(OfflineWuyingAccess, match="HTTP destination"):
            client.get("https://offline-wuying.invalid/no-send")


async def test_real_async_http_transport_refuses_external_target_before_pool(monkeypatch):
    async def never_send(*args, **kwargs):
        raise AssertionError("The automatic HTTP guard was missing")

    transport = httpx.AsyncHTTPTransport()
    monkeypatch.setattr(transport._pool, "handle_async_request", never_send)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(OfflineWuyingAccess, match="HTTP destination"):
            await client.get("https://offline-wuying.invalid/no-send")


async def test_explicit_mock_http_transport_is_allowed():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="local mock"))
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get("https://offline-wuying.invalid/local-stub")).text == "local mock"


@pytest.mark.parametrize("client_kind", ["httpx", "requests", "aiohttp"])
async def test_real_loopback_http_transport_is_allowed(client_kind):
    async def respond(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}/fixture"
        if client_kind == "httpx":
            async with httpx.AsyncClient(trust_env=False) as client:
                assert (await client.get(url)).text == "ok"
        elif client_kind == "requests":
            def get():
                with requests.Session() as client:
                    client.trust_env = False
                    return client.get(url).text
            assert await asyncio.to_thread(get) == "ok"
        else:
            async with aiohttp.ClientSession(trust_env=False) as client:
                async with client.get(url) as response:
                    assert await response.text() == "ok"
    finally:
        server.close()
        await server.wait_closed()
