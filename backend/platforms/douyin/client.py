"""Thin HTTP client for the Douyin open platform endpoints this product uses.

Everything here maps 1:1 to a documented endpoint (docs/plans/media/A5_AUTHORIZATION_CENTER.md
§2). Two things worth knowing that the docs bury:

* The OAuth endpoints (`/oauth/*`) want `application/x-www-form-urlencoded`
  and answer `{"data": {..., "error_code": N}, "message": ...}`; the newer
  ones (`/oauth/userinfo/`, `/share-id/`, `/open/getticket/`) want JSON and
  answer with `err_no`/`extra.error_code` variants. `_check` normalises all
  of them to `PlatformApiError`.
* `client_token` and `ticket` are application-wide and rate limited; both are
  cached through the shared cache so every replica shares one.
"""
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx

from cache import get_cache
from core.log import create_logger
from platforms.errors import PlatformApiError, PlatformAuthRequired, PlatformNotConfigured

log = create_logger("platforms.douyin")

OPEN_BASE = "https://open.douyin.com"
AUTHORIZE_URL = f"{OPEN_BASE}/platform/oauth/connect/"

#: Error codes that mean "the grant is gone, ask the person to scan again".
_REAUTH_CODES = {10008, 10010, 10020, 2190008, 28001003, 28001008}
#: Retrying the same request later may succeed.
_RETRYABLE_CODES = {10001, 2100004, 28001005, 28001006}
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def _check(payload: dict) -> dict:
    """Raise on any platform-level error; return the `data` object."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = {}
    code = data.get("error_code")
    if code in (None, "", 0, "0"):
        code = payload.get("err_no", 0) if isinstance(payload, dict) else 0
    if code in (None, "", 0, "0"):
        extra = payload.get("extra") if isinstance(payload, dict) else None
        if isinstance(extra, dict):
            code = extra.get("error_code", 0)
    try:
        code_int = int(code or 0)
    except (TypeError, ValueError):
        code_int = -1
    if code_int == 0:
        return data
    description = (
        data.get("description")
        or payload.get("err_msg")
        or payload.get("message")
        or ""
    )
    if code_int in _REAUTH_CODES:
        raise PlatformAuthRequired(f"{code_int}: {description}", detail={"platform_code": code_int})
    raise PlatformApiError(code_int, str(description), retryable=code_int in _RETRYABLE_CODES)


class DouyinClient:
    def __init__(self, client_key: str, client_secret: str, *, transport: httpx.AsyncBaseTransport | None = None):
        if not client_key or not client_secret:
            raise PlatformNotConfigured("DOUYIN_CLIENT_KEY / DOUYIN_CLIENT_SECRET are not set")
        self.client_key = client_key
        self._client_secret = client_secret
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=OPEN_BASE, timeout=_TIMEOUT, transport=self._transport)

    # ── OAuth (user grants) ─────────────────────────────────────────────
    def authorize_url(
        self, *, redirect_uri: str, state: str, scope: str = "user_info", call_app: bool = False
    ) -> str:
        params = {
            "client_key": self.client_key,
            "response_type": "code",
            "scope": scope,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        if call_app:
            # On a phone, try to open the Douyin app instead of the web QR page.
            params["is_call_app"] = "1"
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict:
        async with self._http() as http:
            resp = await http.post(
                "/oauth/access_token/",
                data={
                    "client_key": self.client_key,
                    "client_secret": self._client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                },
            )
        return _check(resp.json())

    async def refresh_access_token(self, refresh_token: str) -> dict:
        async with self._http() as http:
            resp = await http.post(
                "/oauth/refresh_token/",
                data={
                    "client_key": self.client_key,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )
        return _check(resp.json())

    async def renew_refresh_token(self, refresh_token: str) -> dict:
        async with self._http() as http:
            resp = await http.post(
                "/oauth/renew_refresh_token/",
                data={"client_key": self.client_key, "refresh_token": refresh_token},
            )
        return _check(resp.json())

    async def userinfo(self, access_token: str, open_id: str) -> dict:
        async with self._http() as http:
            resp = await http.post(
                "/oauth/userinfo/",
                json={"access_token": access_token, "open_id": open_id},
            )
        return _check(resp.json())

    # ── Application credentials (no user involved) ──────────────────────
    async def client_token(self) -> str:
        cache = get_cache()
        key = f"douyin:client_token:{self.client_key}"
        if cache is not None:
            cached = await cache.get(key)
            if cached:
                return str(cached)
        async with self._http() as http:
            resp = await http.post(
                "/oauth/client_token/",
                json={
                    "client_key": self.client_key,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credential",
                },
            )
        data = _check(resp.json())
        token = str(data.get("access_token") or "")
        if not token:
            raise PlatformApiError(-1, "client_token missing in response")
        ttl = max(60, int(data.get("expires_in") or 7200) - 300)
        if cache is not None:
            await cache.set(key, token, ttl=ttl)
        return token

    async def open_ticket(self) -> str:
        cache = get_cache()
        key = f"douyin:ticket:{self.client_key}"
        if cache is not None:
            cached = await cache.get(key)
            if cached:
                return str(cached)
        token = await self.client_token()
        async with self._http() as http:
            resp = await http.get(
                "/open/getticket/",
                headers={"access-token": token, "content-type": "application/json"},
            )
        data = _check(resp.json())
        ticket = str(data.get("ticket") or "")
        if not ticket:
            raise PlatformApiError(-1, "ticket missing in response")
        ttl = max(60, int(data.get("expires_in") or 7200) - 300)
        if cache is not None:
            await cache.set(key, ticket, ttl=ttl)
        return ticket

    async def share_id(self, *, need_callback: bool = True, default_hashtag: str = "") -> str:
        token = await self.client_token()
        params = {"need_callback": "true" if need_callback else "false"}
        if default_hashtag:
            params["default_hashtag"] = default_hashtag
        async with self._http() as http:
            resp = await http.post(
                "/share-id/",
                params=params,
                headers={"access-token": token, "content-type": "application/json"},
            )
        data = _check(resp.json())
        share = str(data.get("share_id") or "")
        if not share:
            raise PlatformApiError(-1, "share_id missing in response")
        return share

    async def get_share_schema(self, body: dict) -> str:
        """Short-link schema from Douyin (scope jump.basic); raises when not granted."""
        token = await self.client_token()
        async with self._http() as http:
            resp = await http.post(
                "/api/douyin/v1/schema/get_share/",
                json=body,
                headers={"access-token": token, "content-type": "application/json"},
            )
        data = _check(resp.json())
        schema = str(data.get("schema") or "")
        if not schema:
            raise PlatformApiError(-1, "schema missing in get_share response")
        return schema

    # ── H5 share signing (pure functions, kept here for one place to test) ──
    @staticmethod
    def sign_share(ticket: str, nonce_str: str, timestamp: str) -> str:
        """MD5 over the ASCII-sorted `key=value&…` of the three fields."""
        fields = {"nonce_str": nonce_str, "ticket": ticket, "timestamp": timestamp}
        joined = "&".join(f"{k}={fields[k]}" for k in sorted(fields))
        return hashlib.md5(joined.encode()).hexdigest()

    @staticmethod
    def new_nonce() -> str:
        return secrets.token_hex(8)

    @staticmethod
    def now_timestamp() -> str:
        return str(int(time.time()))

    # ── Webhook signature ───────────────────────────────────────────────
    def webhook_signature(self, body: bytes) -> str:
        return hashlib.sha1(self._client_secret.encode() + body).hexdigest()
