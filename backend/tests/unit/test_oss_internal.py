"""WUYING asset traffic stays on Alibaba's regional OSS network."""

from types import SimpleNamespace
from urllib.parse import urlsplit

from core.oss import OssClient
from sandbox import assets


def client(endpoint: str = "oss-cn-hangzhou.aliyuncs.com") -> OssClient:
    return OssClient(
        bucket="openbox-assets",
        region="cn-hangzhou",
        endpoint=endpoint,
        key_id="test-key",
        key_secret="test-secret",
    )


def test_internal_presign_changes_only_the_host():
    oss = client()
    public = urlsplit(oss.presign_put("screen.png", "image/png", expires_sec=60))
    internal = urlsplit(
        oss.presign_put("screen.png", "image/png", expires_sec=60, internal=True)
    )

    assert public.hostname == "openbox-assets.oss-cn-hangzhou.aliyuncs.com"
    assert internal.hostname == "openbox-assets.oss-cn-hangzhou-internal.aliyuncs.com"
    assert public.path == internal.path
    assert public.query == internal.query


def test_custom_endpoint_does_not_invent_an_internal_host():
    oss = client("oss.example.test")
    assert oss.internal_host == oss.host
    assert urlsplit(oss.presign_get("screen.png", internal=True)).hostname == oss.host


def test_only_wuying_selects_internal_asset_urls(monkeypatch):
    oss = client()
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(
            sandbox_provider="wuying",
            wuying_region_id="cn-hangzhou",
        ),
    )
    assert assets._use_internal_oss(oss) is True

    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(
            sandbox_provider="docker",
            wuying_region_id="cn-hangzhou",
        ),
    )
    assert assets._use_internal_oss(oss) is False


def test_cross_region_wuying_uses_public_oss_endpoint(monkeypatch):
    oss = client()
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(
            sandbox_provider="wuying",
            wuying_region_id="cn-shanghai",
        ),
    )

    assert assets._use_internal_oss(oss) is False
