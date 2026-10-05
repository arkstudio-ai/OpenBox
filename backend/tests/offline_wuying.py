"""Automatic, per-test network boundary for explicitly listed Wuying unit tests.

Only loopback/Unix transport or an explicit in-process stub is permitted. The
item wrapper restores patches after fixture teardown; unrelated tests are unchanged.
"""
import importlib
import ipaddress
from pathlib import Path
import socket
import ssl
from urllib.parse import urlparse

import httpx
import pytest


WUYING_UNIT_MODULES = frozenset({
    "test_action_server_resource_control.py",
    "test_action_server_resource_transition.py",
    "test_assistant_native_ticket.py",
    "test_assistant_resource_commands.py",
    "test_assistant_resource_control.py",
    "test_assistant_resource_transition.py",
    "test_channel_install_attempt.py",
    "test_channel_maintenance_attempt.py",
    "test_channel_probe_revocation.py",
    "test_channel_verify_revocation.py",
    "test_desktop_activation.py",
    "test_desktop_view_matches_sandbox.py",
    "test_dev_browser_channel_access.py",
    "test_pool.py",
    "test_terminal_channel_access.py",
    "test_wuying_channel.py",
    "test_wuying_fleet_api.py",
    "test_wuying_image_v3.py",
    "test_wuying_offline_boundary.py",
    "test_wuying_provision_smoke.py",
    "test_wuying_provisioning.py",
    "test_wuying_session_tunnel.py",
})
_UNIT_DIRECTORY = Path(__file__).parent / "unit"


class OfflineWuyingAccess(BaseException):
    """Do not let production retry/repair ``except Exception`` hide a leak."""


def _loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host in (None, "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_loopback(host, boundary):
    if not _loopback(host):
        raise OfflineWuyingAccess(f"External {boundary} is forbidden in an offline Wuying test")


def install_wuying_offline_guard(monkeypatch):
    from aiohttp.client_reqrep import ClientRequest
    from requests.adapters import HTTPAdapter
    from Tea.core import TeaCore
    from sandbox import wuying_ecd

    def no_sdk(*args, **kwargs):
        raise OfflineWuyingAccess("Unstubbed Wuying SDK client is forbidden in an offline test")

    monkeypatch.setattr(wuying_ecd, "ecd_client", no_sdk)
    monkeypatch.setattr(wuying_ecd, "eds_user_client", no_sdk)
    # Native ticket and account-balance code construct the SDK directly.
    # Cases exercising request shapes can replace these with local clients.
    for package in ("alibabacloud_ecd20200930", "alibabacloud_eds_user20210308",
                    "alibabacloud_bssopenapi20171214"):
        monkeypatch.setattr(importlib.import_module(package + ".client"), "Client", no_sdk)

    connect, connect_ex, sendto = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto

    def checked_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _require_loopback(address[0], "socket connection")
        return connect(self, address)

    def checked_connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _require_loopback(address[0], "socket connection")
        return connect_ex(self, address)

    def checked_sendto(self, data, *args):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _require_loopback(args[-1][0], "datagram destination")
        return sendto(self, data, *args)

    monkeypatch.setattr(socket.socket, "connect", checked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", checked_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", checked_sendto)

    def require_peer(sock):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            # A closed/unconnected descriptor must fail before any write too.
            _require_loopback(sock.getpeername()[0], "connected socket peer")

    def guarded_write(original):
        def write(self, *args, **kwargs):
            require_peer(self)
            return original(self, *args, **kwargs)
        return write

    # Cached clients can reuse a connection opened before this item. SSLSocket
    # writes call _sslobj directly, bypassing socket.socket.send.
    for cls, names in ((socket.socket, ("send", "sendall", "sendfile")),
                       (ssl.SSLSocket, ("send", "sendall", "write"))):
        for name in names:
            monkeypatch.setattr(cls, name, guarded_write(getattr(cls, name)))

    if hasattr(socket.socket, "sendmsg"):
        sendmsg = socket.socket.sendmsg

        def checked_sendmsg(self, buffers, ancdata=(), flags=0, address=None):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                if address is None:
                    require_peer(self)
                else:
                    _require_loopback(address[0], "datagram destination")
            if address is None:
                return sendmsg(self, buffers, ancdata, flags)
            return sendmsg(self, buffers, ancdata, flags, address)

        monkeypatch.setattr(socket.socket, "sendmsg", checked_sendmsg)

    def guarded_dns(original):
        def lookup(host, *args, **kwargs):
            _require_loopback(host, "DNS destination")
            return original(host, *args, **kwargs)
        return lookup

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, guarded_dns(getattr(socket, name)))

    # Check the request destination even if environment proxies use loopback.
    # ASGITransport and MockTransport do not submit network I/O and remain usable.
    handle = httpx.HTTPTransport.handle_request
    async_handle = httpx.AsyncHTTPTransport.handle_async_request

    def checked_http(self, request):
        _require_loopback(request.url.host, "HTTP destination")
        return handle(self, request)

    async def checked_async_http(self, request):
        _require_loopback(request.url.host, "HTTP destination")
        return await async_handle(self, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", checked_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", checked_async_http)

    # SDK instances and request pools created before the guard also reach
    # these dispatch methods. Check the target independently of proxy peers.
    tea_send, tea_async_send = TeaCore.do_action, TeaCore.async_do_action
    requests_send, aiohttp_send = HTTPAdapter.send, ClientRequest.send

    def checked_tea(request, *args, **kwargs):
        _require_loopback(urlparse(TeaCore.compose_url(request)).hostname, "SDK destination")
        return tea_send(request, *args, **kwargs)

    async def checked_async_tea(request, *args, **kwargs):
        _require_loopback(urlparse(TeaCore.compose_url(request)).hostname, "SDK destination")
        return await tea_async_send(request, *args, **kwargs)

    def checked_requests(self, request, *args, **kwargs):
        _require_loopback(urlparse(request.url).hostname, "HTTP destination")
        return requests_send(self, request, *args, **kwargs)

    async def checked_aiohttp(self, *args, **kwargs):
        _require_loopback(self.url.host, "HTTP destination")
        return await aiohttp_send(self, *args, **kwargs)

    monkeypatch.setattr(TeaCore, "do_action", staticmethod(checked_tea))
    monkeypatch.setattr(TeaCore, "async_do_action", staticmethod(checked_async_tea))
    monkeypatch.setattr(HTTPAdapter, "send", checked_requests)
    monkeypatch.setattr(ClientRequest, "send", checked_aiohttp)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    path = Path(item.path)
    if path.parent == _UNIT_DIRECTORY and path.name in WUYING_UNIT_MODULES:
        # Surround setup AND teardown so an earlier autouse fixture cannot
        # undo its shared monkeypatch after the guard and leak guarded methods.
        # This owns no shared fixtures and changes no fixture dependency order.
        with pytest.MonkeyPatch.context() as patch:
            install_wuying_offline_guard(patch)
            yield
    else:
        yield
