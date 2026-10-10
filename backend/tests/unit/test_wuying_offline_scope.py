"""Intentionally outside the offline module list: patches must not leak here."""
import socket
import ssl
import importlib

from aiohttp.client_reqrep import ClientRequest
import httpx
from requests.adapters import HTTPAdapter
from Tea.core import TeaCore

from sandbox import wuying_ecd


# Collection happens before the per-item protocol wrapper executes. The
# default alphabetical run executes guarded cases before this scope check.
def _boundary_methods():
    return (socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto,
            socket.socket.send, socket.socket.sendall, socket.socket.sendfile,
            getattr(socket.socket, "sendmsg", None),
            ssl.SSLSocket.send, ssl.SSLSocket.sendall, ssl.SSLSocket.write,
            socket.getaddrinfo, socket.gethostbyname, socket.gethostbyname_ex,
            httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request,
            TeaCore.do_action, TeaCore.async_do_action, HTTPAdapter.send, ClientRequest.send,
            wuying_ecd.ecd_client, wuying_ecd.eds_user_client)


_ORIGINALS = _boundary_methods()
_SDK_MODULES = [importlib.import_module(package + ".client") for package in (
    "alibabacloud_ecd20200930", "alibabacloud_eds_user20210308", "alibabacloud_bssopenapi20171214")]
_SDK_CLASSES = tuple(module.Client for module in _SDK_MODULES)


def test_offline_fixture_restores_unrelated_module_without_io():
    assert _ORIGINALS == _boundary_methods()
    assert _SDK_CLASSES == tuple(module.Client for module in _SDK_MODULES)
