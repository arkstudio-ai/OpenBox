"""The material APIs may live on a different origin than video generation.

The BossIP relay serves /v1/videos but answers /api/material with an nginx 404
HTML page, which used to surface as a baffling "returned a non-JSON response"
after the real-person authorization had already failed.
"""
import pytest

from core.config import ProviderConfig, get_config
from video.materials import MaterialProviderError, configured_material_target

RELAY = "https://openapi.bossipai.com.cn"
TOKENSPACE = "https://api.tokenspace.net.cn"


def _configure(monkeypatch, *, base_url, material_base_url="", material_api_key=""):
    config = get_config()
    monkeypatch.setattr(config.video_generation, "provider", "doubao")
    monkeypatch.setattr(config.video_generation, "material_base_url", material_base_url)
    monkeypatch.setattr(config.video_generation, "material_api_key", material_api_key)
    monkeypatch.setitem(
        config.provider, "doubao", ProviderConfig(api_key="generation-key", base_url=base_url)
    )
    monkeypatch.delenv("DOUBAO_MATERIAL_BASE_URL", raising=False)
    monkeypatch.delenv("DOUBAO_MATERIAL_API_KEY", raising=False)


def test_relay_without_material_api_fails_with_an_actionable_error(monkeypatch):
    _configure(monkeypatch, base_url=RELAY)
    with pytest.raises(MaterialProviderError) as excinfo:
        configured_material_target()
    assert excinfo.value.code == "material_api_unavailable"
    # The message must name both knobs; "non-JSON response" told nobody
    # anything, and naming only the origin leads straight to a 401.
    assert "material_base_url" in str(excinfo.value)
    assert "material_api_key" in str(excinfo.value)


def test_material_base_url_overrides_the_generation_origin(monkeypatch):
    _configure(
        monkeypatch,
        base_url=RELAY,
        material_base_url=TOKENSPACE,
    )
    assert configured_material_target().base_url == TOKENSPACE


def test_env_override_is_honoured(monkeypatch):
    _configure(monkeypatch, base_url=RELAY)
    monkeypatch.setenv("DOUBAO_MATERIAL_BASE_URL", TOKENSPACE)
    assert configured_material_target().base_url == TOKENSPACE


def test_direct_tokenspace_deployment_is_unchanged(monkeypatch):
    _configure(monkeypatch, base_url=TOKENSPACE)
    target = configured_material_target()
    assert target.base_url == TOKENSPACE
    assert target.api_key == "generation-key"


def test_a_separate_material_origin_carries_its_own_key(monkeypatch):
    # A different origin is a different account: sending the relay's key to
    # TokenSpace authenticates against a provider that never issued it.
    _configure(
        monkeypatch,
        base_url=RELAY,
        material_base_url=TOKENSPACE,
        material_api_key="tokenspace-key",
    )
    target = configured_material_target()
    assert target.base_url == TOKENSPACE
    assert target.api_key == "tokenspace-key"


def test_material_key_falls_back_to_the_generation_key(monkeypatch):
    _configure(monkeypatch, base_url=RELAY, material_base_url=TOKENSPACE)
    assert configured_material_target().api_key == "generation-key"


def test_material_key_env_override(monkeypatch):
    _configure(monkeypatch, base_url=RELAY, material_base_url=TOKENSPACE)
    monkeypatch.setenv("DOUBAO_MATERIAL_API_KEY", "env-key")
    assert configured_material_target().api_key == "env-key"
