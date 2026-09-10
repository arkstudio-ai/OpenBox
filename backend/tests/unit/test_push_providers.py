"""Provider wire contracts, with ephemeral keys and no external requests."""
import base64
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from jose import jwt

from notifications.providers import PushProviders
from notifications.schema import BUNDLE_ID, DeviceRegistration
from notifications.vendor_config import VENDORS, vendor_options

PAYLOAD = {"source": "openbox", "schemaVersion": 1, "eventId": "event-123",
           "recipientId": "user-1", "bindingId": "binding-1", "type": "system_test",
           "title": "通知测试", "body": "测试内容"}


@pytest.mark.parametrize("environment,host", [("sandbox", "api.sandbox.push.apple.com"), ("production", "api.push.apple.com")])
async def test_apns_auth_topic_environment_and_ttl(environment, host):
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    requests = []
    def handle(request):
        requests.append(request)
        token = request.headers["authorization"].split()[1]
        claims = jwt.decode(token, key.public_key(), algorithms=["ES256"])
        assert claims["iss"] == "test-team"
        assert abs(claims["iat"] - time.time()) < 5
        assert jwt.get_unverified_header(token)["kid"] == "test-key"
        assert request.url.host == host
        assert request.url.path == "/3/device/" + "ab" * 32
        assert request.headers["apns-topic"] == BUNDLE_ID
        assert request.headers["apns-push-type"] == "alert"
        assert request.headers["apns-collapse-id"] == PAYLOAD["eventId"]
        assert 85 <= int(request.headers["apns-expiration"]) - time.time() <= 90
        body = json.loads(request.content)
        assert body["aps"]["alert"]["body"] == PAYLOAD["body"]
        assert body["bindingId"] == "binding-1"
        return httpx.Response(200, headers={"apns-id": "accepted-id"})
    providers = PushProviders(apns_key=pem, apns_key_id="test-key", apns_team_id="test-team", transport=httpx.MockTransport(handle))
    try:
        for _ in range(2):
            result = await providers.send("apns", "ab" * 32, environment, PAYLOAD, ttl=90)
            assert result.ok and result.message_id == "accepted-id"
        assert requests[0].headers["authorization"] == requests[1].headers["authorization"]
    finally:
        await providers.close()


async def test_jpush_is_direct_single_registration_and_retains_routing_payload():
    def handle(request):
        assert request.url == "https://api.jpush.cn/v3/push"
        assert request.headers["authorization"] == "Basic " + base64.b64encode(b"app-key:server-secret").decode()
        assert b"server-secret" not in request.content
        body = json.loads(request.content)
        assert body["platform"] == ["android"]
        assert body["audience"] == {"registration_id": ["registration-1"]}
        assert body["options"]["time_to_live"] == 300
        assert body["options"]["classification"] == 1
        channels = body["options"]["third_party_channel"]
        assert set(channels) == set(VENDORS)
        assert all(value["distribution"] == "secondary_push" for value in channels.values())
        assert channels["huawei"]["distribution_fcm"] == "secondary_pns_push"
        android = body["notification"]["android"]
        assert android["extras"]["bindingId"] == "binding-1"
        assert android["display_foreground"] == "0"
        assert android["channel_id"] == "bossip_system_notifications"
        assert f"{BUNDLE_ID}.PUSH_OPEN" in android["intent"]["url"]
        return httpx.Response(200, json={"msg_id": 42})
    providers = PushProviders(jpush_app_key="app-key", jpush_secret="server-secret", transport=httpx.MockTransport(handle))
    try:
        result = await providers.send("jpush", "registration-1", "production", PAYLOAD, ttl=300)
        assert result.ok and result.message_id == "42"
    finally:
        await providers.close()


@pytest.mark.parametrize("status,code,retry,invalid", [
    (400, 1003, False, False), (401, 1004, False, False),
    (400, 1011, False, True), (429, 2002, True, False), (503, None, True, False),
])
async def test_jpush_only_retires_proven_invalid_target(status, code, retry, invalid):
    providers = PushProviders(jpush_app_key="a", jpush_secret="s", transport=httpx.MockTransport(
        lambda _: httpx.Response(status, json={"error": {"code": code, "message": "do not expose raw provider text"}})))
    try:
        result = await providers.send("jpush", "reg", "production", PAYLOAD)
        assert not result.ok and result.retry == retry and result.invalid_device == invalid
        assert "do not expose" not in result.error
    finally:
        await providers.close()


@pytest.mark.parametrize("status,reason,retry,invalid", [
    (410, "Unregistered", False, True), (400, "BadDeviceToken", False, True),
    (403, "InvalidProviderToken", False, False), (429, "TooManyRequests", True, False),
])
async def test_apns_provider_errors_do_not_retire_unrelated_tokens(status, reason, retry, invalid):
    providers = PushProviders(apns_key="test", transport=httpx.MockTransport(
        lambda _: httpx.Response(status, json={"reason": reason})))
    providers._provider_token = lambda: "test-signed-token"
    try:
        result = await providers.send("apns", "ab" * 32, "production", PAYLOAD)
        assert not result.ok and result.retry == retry and result.invalid_device == invalid
    finally:
        await providers.close()


async def test_oversized_unicode_payload_never_reaches_a_provider():
    def no_request(_):
        pytest.fail("An oversized payload must not be sent")
    providers = PushProviders(apns_key="test", jpush_secret="test", transport=httpx.MockTransport(no_request))
    try:
        for provider in ("apns", "jpush"):
            result = await providers.send(provider, "test", "production", {**PAYLOAD, "body": "中" * 2000})
            assert result.error == "payload_too_large"
    finally:
        await providers.close()


@pytest.mark.parametrize("data", [
    {"platform": "ios", "provider": "jpush", "token": "reg"},
    {"platform": "android", "provider": "apns", "token": "ab" * 32},
    {"platform": "ios", "provider": "apns", "token": "not-a-token"},
    {"platform": "android", "provider": "jpush", "token": "valid", "userId": "someone-else"},
])
def test_registration_rejects_cross_provider_and_identity_overrides(data):
    with pytest.raises(ValueError):
        DeviceRegistration.model_validate(data)


async def test_vendor_categories_and_channel_ids_preserve_single_device_and_click_target():
    config = {"xiaomi": {"channel_id": "approved-tasks"},
              "huawei": {"category": "TODO", "importance": "NORMAL"},
              "oppo": {"category": "TODO", "notify_level": 2}}
    def handle(request):
        body = json.loads(request.content)
        assert body["audience"] == {"registration_id": ["registration-1"]}
        channels = body["options"]["third_party_channel"]
        assert channels["xiaomi"]["channel_id"] == "approved-tasks"
        assert channels["huawei"]["category"] == "TODO"
        assert channels["oppo"]["notify_level"] == 2
        assert channels["oppo"]["distribution"] == "secondary_push"
        assert body["notification"]["android"]["extras"]["recipientId"] == "user-1"
        assert f"{BUNDLE_ID}.MainActivity" in body["notification"]["android"]["intent"]["url"]
        return httpx.Response(200, json={"msg_id": 43})
    providers = PushProviders(jpush_app_key="app-key", jpush_secret="server-secret",
        jpush_vendor_options=json.dumps(config), transport=httpx.MockTransport(handle))
    try:
        assert (await providers.send("jpush", "registration-1", "production", PAYLOAD)).ok
    finally:
        await providers.close()


@pytest.mark.parametrize("raw", [
    "{", "[]", '{"unknown":{}}', '{"huawei":[]}', '{"xiaomi":{"channel_id":""}}',
    '{"huawei":{"category":true}}', '{"huawei":{"importance":"urgent"}}',
    '{"honor":{"importance":"HIGH"}}', '{"oppo":{"notify_level":true}}',
    '{"oppo":{"notify_level":16}}', '{"huawei":{"intent":"arbitrary"}}',
    '{"fcm":{"distribution":"ospush"}}', "x" * 4097,
])
def test_invalid_vendor_configuration_fails_without_exposing_values(raw):
    with pytest.raises(ValueError, match="^Invalid BOSSIP_JPUSH_VENDOR_OPTIONS; see docs/ANDROID_PUSH_VENDORS.md$"):
        vendor_options(raw)


async def test_vendor_configuration_loads_from_server_environment(monkeypatch):
    for name in ("BOSSIP_APNS_KEY_PATH", "BOSSIP_APNS_KEY_ID", "BOSSIP_APNS_TEAM_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOSSIP_APNS_TOPIC", BUNDLE_ID)
    monkeypatch.setenv("BOSSIP_JPUSH_APP_KEY", "test-app")
    monkeypatch.setenv("BOSSIP_JPUSH_MASTER_SECRET", "test-secret")
    monkeypatch.setenv("BOSSIP_JPUSH_VENDOR_OPTIONS", '{"xiaomi":{"channel_id":"approved"}}')
    providers = PushProviders.from_env()
    try:
        assert providers.jpush_vendor_options["xiaomi"]["channel_id"] == "approved"
    finally:
        await providers.close()
