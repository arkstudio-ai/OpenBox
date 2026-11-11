"""Mainland / elsewhere: sign-in method and payment follow the caller's address."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from core import client_region

EDGE = "106.52.167.53"          # the public proxy in front of the service
MAINLAND = "114.114.114.114"
TEXAS = "8.8.8.8"


def request(forwarded: str | None = None, peer: str = "172.18.0.5") -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers,
                    "client": (peer, 1234), "query_string": b""})


@pytest.fixture(autouse=True)
def gate(monkeypatch):
    monkeypatch.setenv("REGION_GATE", "on")
    monkeypatch.setenv("REGION_TRUSTED_PROXIES", EDGE)
    client_region._trusted.cache_clear()
    yield
    client_region._trusted.cache_clear()


def test_the_address_table_knows_mainland_from_elsewhere():
    assert client_region.is_mainland(MAINLAND)
    assert client_region.is_mainland("223.5.5.5")
    assert not client_region.is_mainland(TEXAS)
    assert not client_region.is_mainland("1.1.1.1")
    assert not client_region.is_mainland("2001:4860:4860::8888")
    assert client_region.is_mainland("240e::1")              # China Telecom
    assert client_region.is_mainland("::ffff:114.114.114.114")


def test_the_caller_is_the_last_hop_that_is_not_ours():
    assert client_region.client_ip(request(f"{TEXAS}, {EDGE}")) == TEXAS
    # Whatever the client wrote to the left of its real address is ignored.
    assert client_region.client_ip(request(f"{MAINLAND}, {TEXAS}, {EDGE}")) == TEXAS
    assert client_region.client_ip(request(None)) is None
    assert client_region.client_ip(request("10.0.0.7")) is None
    assert client_region.client_ip(request(None, peer=TEXAS)) == TEXAS


def test_region_follows_the_address_and_unknown_counts_as_mainland():
    assert client_region.region_of(request(f"{TEXAS}, {EDGE}")) == "intl"
    assert client_region.region_of(request(f"{MAINLAND}, {EDGE}")) == "cn"
    assert client_region.region_of(request(None)) == "cn"
    assert client_region.region_of(request("not-an-address")) == "cn"


def test_an_unlisted_edge_proxy_would_be_taken_for_the_caller(monkeypatch):
    """Why REGION_TRUSTED_PROXIES must name the public proxy: it is a mainland address."""
    monkeypatch.setenv("REGION_TRUSTED_PROXIES", "")
    client_region._trusted.cache_clear()
    assert client_region.client_ip(request(f"{TEXAS}, {EDGE}")) == EDGE
    assert client_region.region_of(request(f"{TEXAS}, {EDGE}")) == "cn"


def test_without_the_gate_everyone_is_mainland(monkeypatch):
    monkeypatch.setenv("REGION_GATE", "")
    assert client_region.region_of(request(f"{TEXAS}, {EDGE}")) == "cn"
    client_region.require_payment_region(request(f"{TEXAS}, {EDGE}"))


def test_payment_is_refused_outside_the_mainland():
    client_region.require_payment_region(request(f"{MAINLAND}, {EDGE}"))
    with pytest.raises(HTTPException) as refused:
        client_region.require_payment_region(request(f"{TEXAS}, {EDGE}"))
    assert refused.value.status_code == 403
    assert refused.value.detail["code"] == "PAYMENT_REGION_UNAVAILABLE"


@pytest.mark.asyncio
async def test_sign_in_config_defaults_to_the_email_form_outside_the_mainland(monkeypatch):
    from auth import logto, routes
    monkeypatch.setattr(logto, "public_config", lambda: {
        "enabled": True, "endpoint": "https://auth.example", "issuer": "i", "app_id": "a", "native_app_id": "n"})
    abroad = await routes.logto_config(request(f"{TEXAS}, {EDGE}"))
    assert abroad["enabled"] is False and abroad["sso_available"] is True
    assert abroad["preferred"] == "password" and abroad["region"] == "intl"
    assert abroad["app_id"] == "a" and abroad["native_app_id"] == "n"   # the SSO link still works
    home = await routes.logto_config(request(f"{MAINLAND}, {EDGE}"))
    assert home["enabled"] is True and home["preferred"] == "sso" and home["region"] == "cn"


class Users:
    def __init__(self):
        self.rows: dict[str, dict] = {}

    async def get_by_username(self, username):
        return next((row for row in self.rows.values() if row["username"] == username), None)

    async def get_by_email(self, email):
        return next((row for row in self.rows.values() if (row.get("email") or "").lower() == email.lower()), None)


@pytest.mark.asyncio
async def test_email_sign_up_needs_a_usable_unused_address(monkeypatch):
    from auth import routes
    users = Users()
    users.rows["u1"] = {"id": "u1", "username": "amy", "email": "amy@example.com"}
    monkeypatch.setattr(routes, "_user_repo", users)
    monkeypatch.setattr(routes, "_cache", None)

    async def refusal(**body):
        with pytest.raises(HTTPException) as raised:
            await routes.register(routes.RegisterRequest(**body), request(f"{TEXAS}, {EDGE}"), None)
        return raised.value

    assert (await refusal(password="abc12345")).detail["code"] == "AUTH_EMAIL_REQUIRED"
    assert (await refusal(password="abc12345", email="nope")).detail["code"] == "AUTH_EMAIL_INVALID"
    assert (await refusal(password="abc12345", email="AMY@example.com")).detail["code"] == "AUTH_EMAIL_EXISTS"


@pytest.mark.asyncio
async def test_account_name_is_derived_from_the_address_and_made_unique(monkeypatch):
    from auth import routes
    users = Users()
    monkeypatch.setattr(routes, "_user_repo", users)
    assert await routes._username_for("minh.tran@example.vn") == "minh.tran"
    users.rows["u1"] = {"id": "u1", "username": "minh.tran", "email": None}
    second = await routes._username_for("minh.tran@example.vn")
    assert second.startswith("minh.tran-") and second != "minh.tran"
    assert await routes._username_for("张三@example.com") == "user"
