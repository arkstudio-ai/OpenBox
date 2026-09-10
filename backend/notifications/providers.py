"""APNs token auth and JPush REST v3; secrets stay on the server."""
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from jose import jwt

from notifications.schema import BUNDLE_ID
from notifications.vendor_config import vendor_options


@dataclass(frozen=True)
class SendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None
    retry: bool = False
    invalid_device: bool = False


class PushProviders:
    def __init__(self, *, apns_key=None, apns_key_id="", apns_team_id="", jpush_app_key="",
                 jpush_secret="", jpush_vendor_options="", transport=None):
        self.apns_key, self.apns_key_id, self.apns_team_id = apns_key, apns_key_id, apns_team_id
        self.jpush_app_key, self.jpush_secret = jpush_app_key, jpush_secret
        self.jpush_vendor_options = vendor_options(jpush_vendor_options)
        self.client = httpx.AsyncClient(http2=True, timeout=10, transport=transport)
        self._jwt = None
        self._jwt_at = 0

    @classmethod
    def from_env(cls):
        path = os.getenv("BOSSIP_APNS_KEY_PATH", "").strip()
        kid = os.getenv("BOSSIP_APNS_KEY_ID", "").strip()
        team = os.getenv("BOSSIP_APNS_TEAM_ID", "").strip()
        app = os.getenv("BOSSIP_JPUSH_APP_KEY", "").strip()
        secret = os.getenv("BOSSIP_JPUSH_MASTER_SECRET", "").strip()
        if any((path, kid, team)) and not all((path, kid, team)):
            raise ValueError("Incomplete APNs configuration")
        if bool(app) != bool(secret):
            raise ValueError("Incomplete JPush configuration")
        topic = os.getenv("BOSSIP_APNS_TOPIC", BUNDLE_ID).strip()
        if topic != BUNDLE_ID:
            raise ValueError("APNs topic must match the mobile bundle ID")
        key = Path(path).read_text() if path else None
        if key:
            # Fail on invalid configuration at startup without printing key data.
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey, SECP256R1
            parsed = load_pem_private_key(key.encode(), password=None)
            if not isinstance(parsed, EllipticCurvePrivateKey) or not isinstance(parsed.curve, SECP256R1):
                raise ValueError("APNs requires a P-256 private key")
        return cls(apns_key=key, apns_key_id=kid, apns_team_id=team, jpush_app_key=app, jpush_secret=secret,
                   jpush_vendor_options=os.getenv("BOSSIP_JPUSH_VENDOR_OPTIONS", ""))

    @property
    def enabled(self):
        return {name for name, ready in (("apns", self.apns_key), ("jpush", self.jpush_secret)) if ready}

    async def close(self):
        await self.client.aclose()

    def _provider_token(self):
        current = int(time.time())
        if not self._jwt or current - self._jwt_at >= 3000:
            self._jwt = jwt.encode({"iss": self.apns_team_id, "iat": current}, self.apns_key,
                                   algorithm="ES256", headers={"kid": self.apns_key_id})
            self._jwt_at = current
        return self._jwt

    async def send(self, provider, token, environment, payload, *, ttl=86400):
        if provider not in self.enabled:
            return SendResult(False, error="provider_not_configured")
        try:
            if provider == "apns":
                return await self._apns(token, environment, payload, ttl)
            return await self._jpush(token, payload, ttl)
        except (httpx.TransportError, TimeoutError):
            return SendResult(False, error="provider_network_error", retry=True)

    async def _apns(self, token, environment, payload, ttl):
        custom = {key: value for key, value in payload.items() if key not in {"title", "body"}}
        body = {**custom, "aps": {"alert": {"title": payload["title"], "body": payload["body"]}, "sound": "default"}}
        raw = json.dumps(body, ensure_ascii=False).encode()
        if len(raw) > 4096:
            return SendResult(False, error="payload_too_large")
        host = "api.sandbox.push.apple.com" if environment == "sandbox" else "api.push.apple.com"
        response = await self.client.post(f"https://{host}/3/device/{token}", content=raw, headers={
            "authorization": f"bearer {self._provider_token()}", "apns-topic": BUNDLE_ID,
            "apns-push-type": "alert", "apns-priority": "10", "apns-collapse-id": payload["eventId"],
            "apns-expiration": str(int(time.time()) + ttl), "content-type": "application/json",
        })
        if response.is_success:
            return SendResult(True, message_id=response.headers.get("apns-id"))
        data = _json(response)
        reason = str(data.get("reason", "rejected"))
        # Only known provider codes, never raw response content or credentials.
        invalid = reason in {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"}
        return SendResult(False, error=f"apns_{response.status_code}", invalid_device=invalid,
                          retry=response.status_code == 429 or response.status_code >= 500)

    async def _jpush(self, token, payload, ttl):
        extras = {key: value for key, value in payload.items() if key not in {"title", "body"}}
        body = {"platform": ["android"], "audience": {"registration_id": [token]}, "notification": {
            "android": {"title": payload["title"], "alert": payload["body"], "extras": extras,
                        "channel_id": "bossip_system_notifications", "display_foreground": "0",
                        "intent": {"url": f"intent:#Intent;action={BUNDLE_ID}.PUSH_OPEN;component={BUNDLE_ID}/{BUNDLE_ID}.MainActivity;end"}}},
            "options": {"time_to_live": ttl, "classification": 1,
                        "third_party_channel": self.jpush_vendor_options}}
        raw = json.dumps(body, ensure_ascii=False).encode()
        if len(raw) > 4000:
            return SendResult(False, error="payload_too_large")
        response = await self.client.post("https://api.jpush.cn/v3/push", content=raw,
                                          headers={"content-type": "application/json"},
                                          auth=(self.jpush_app_key, self.jpush_secret))
        data = _json(response)
        if response.is_success:
            return SendResult(True, message_id=str(data.get("msg_id", "")))
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        code = error.get("code")
        return SendResult(False, error=f"jpush_{response.status_code}_{code if isinstance(code, int) else 'rejected'}",
                          invalid_device=code == 1011,
                          retry=response.status_code == 429 or response.status_code >= 500)


def _json(response):
    try:
        data = response.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
