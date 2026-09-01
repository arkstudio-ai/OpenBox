"""Test auth module — JWT, password, ticket."""
import asyncio
import pytest

from auth.jwt import (
    create_access_token,
    create_asset_download_token,
    create_refresh_token,
    decode_access_token,
    decode_asset_download_token,
    decode_refresh_token,
    init_auth,
)
from auth.password import hash_password, verify_password, validate_password_strength
from auth.ticket import init_ticket_store, create_ticket, consume_ticket
from cache.memory_cache import MemoryCache


# ── Password tests ──

def test_hash_and_verify():
    hashed = hash_password("mypassword123")
    assert verify_password("mypassword123", hashed)
    assert not verify_password("wrongpassword", hashed)


def test_password_strength_too_short():
    assert validate_password_strength("abc1") is not None


def test_password_strength_no_digit():
    assert validate_password_strength("abcdefgh") is not None


def test_password_strength_no_letter():
    assert validate_password_strength("12345678") is not None


def test_password_strength_valid():
    assert validate_password_strength("mypass123") is None


# ── JWT tests ──

@pytest.fixture(autouse=True)
def setup_jwt():
    init_auth("test-secret-key-for-unit-tests-32bytes", 15, 7)


def test_access_token_roundtrip():
    token = create_access_token("user123", "admin")
    payload = decode_access_token(token)
    assert payload is not None
    assert payload["sub"] == "user123"
    assert payload["role"] == "admin"
    assert payload["type"] == "access"


def test_refresh_token_roundtrip():
    token = create_refresh_token("user456")
    payload = decode_refresh_token(token)
    assert payload is not None
    assert payload["sub"] == "user456"
    assert payload["type"] == "refresh"


def test_access_token_not_refresh():
    token = create_access_token("user123")
    assert decode_refresh_token(token) is None


def test_refresh_token_not_access():
    token = create_refresh_token("user123")
    assert decode_access_token(token) is None


def test_invalid_token():
    assert decode_access_token("invalid.token.here") is None


def test_asset_download_token_is_bound_to_one_asset():
    token = create_asset_download_token("user123", "asset_123")
    payload = decode_asset_download_token(token, "asset_123")

    assert payload is not None
    assert payload["sub"] == "user123"
    assert decode_asset_download_token(token, "asset_other") is None


# ── Ticket tests ──

@pytest.fixture
def ticket_cache():
    cache = MemoryCache()
    init_ticket_store(cache)
    return cache


async def test_ticket_create_and_consume(ticket_cache):
    ticket = await create_ticket("user789", "user")
    assert isinstance(ticket, str)
    assert len(ticket) > 20

    result = await consume_ticket(ticket)
    assert result is not None
    assert result["user_id"] == "user789"
    assert result["role"] == "user"


async def test_ticket_one_time_use(ticket_cache):
    ticket = await create_ticket("user789")
    await consume_ticket(ticket)
    # Second use should fail
    result = await consume_ticket(ticket)
    assert result is None


async def test_ticket_invalid(ticket_cache):
    result = await consume_ticket("nonexistent-ticket")
    assert result is None


# ── Coded refusals ──
#
# The clients pick their copy from `detail.code`; a bare string `detail` leaves
# them falling back to the status, so a wrong password rendered as "Your session
# expired. Please sign in again." Both locale catalogs had carried
# AUTH_INVALID_CREDENTIALS since before that, unreachable, which is exactly what
# makes this worth pinning rather than trusting to review.

def _auth_refusals():
    from auth import routes
    return routes


def test_login_refusal_carries_a_code():
    routes = _auth_refusals()
    err = routes._coded_error(401, "AUTH_INVALID_CREDENTIALS", "Invalid credentials")
    assert err.detail["code"] == "AUTH_INVALID_CREDENTIALS"
    assert err.detail["message"] == "Invalid credentials"
    assert err.headers["X-Error-Code"] == "AUTH_INVALID_CREDENTIALS"


def test_every_auth_code_raised_is_one_the_clients_can_render():
    """The codes the backend emits and the copy the clients hold must agree.

    Either half alone looks fine: the backend raises something plausible, the
    catalogs hold something plausible, and the mismatch only shows up as the
    wrong sentence on someone's screen.
    """
    import json
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    source = (root / "backend/auth/routes.py").read_text()
    raised = set(re.findall(r'_coded_error\(\s*\d+,\s*"([A-Z_]+)"', source))
    assert raised, "no coded refusals found — did the helper get renamed?"

    for catalog in (
        root / "frontend-v2/src/locales/en-US/errors.json",
        root / "frontend-v2/src/locales/zh-CN/errors.json",
        root / "mobile/assets/locales/en-US/errors.json",
        root / "mobile/assets/locales/zh-CN/errors.json",
    ):
        known = set(json.loads(catalog.read_text()))
        missing = raised - known
        assert not missing, f"{catalog.name} cannot render {sorted(missing)}"
