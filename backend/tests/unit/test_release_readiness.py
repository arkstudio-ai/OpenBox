"""Release gates for deployment mode and process readiness."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import main
from core.config import ProviderConfig, ToolExposureConfig


def _config(**overrides):
    values = {
        "jwt_secret": "",
        "sandbox_provider": "wuying",
        "wuying_api_key": "desktop-secret",
        "cors_origins": ["http://localhost:3000"],
        "model": "openai/gpt-5.6-luna",
        "models": [],
        "provider": {
            "openai": ProviderConfig(
                api_key="model-secret",
                base_url="https://model-gateway.example/v1",
            )
        },
        "tool_exposure": ToolExposureConfig(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_shared_wuying_allows_single_user_mode():
    main._validate_deployment_contract(_config(jwt_secret=""))


async def test_lifespan_allows_single_user_to_enter_initialization(monkeypatch):
    class InitializationReached(Exception):
        pass

    def init_infrastructure(_config):
        raise InitializationReached

    monkeypatch.setattr(
        "core.config.get_config",
        lambda: _config(jwt_secret=""),
    )
    monkeypatch.setattr(main, "_init_infrastructure", init_infrastructure)

    with pytest.raises(InitializationReached):
        async with main.lifespan(SimpleNamespace()):
            pass


def test_shared_wuying_rejects_jwt_multi_user_mode():
    with pytest.raises(RuntimeError, match="one isolated WUYING desktop per user"):
        main._validate_deployment_contract(_config(jwt_secret="configured"))


async def test_lifespan_rejects_shared_multi_user_before_initialization(monkeypatch):
    initialized = False

    def init_infrastructure(_config):
        nonlocal initialized
        initialized = True

    monkeypatch.setattr(
        "core.config.get_config",
        lambda: _config(jwt_secret="configured"),
    )
    monkeypatch.setattr(main, "_init_infrastructure", init_infrastructure)

    with pytest.raises(RuntimeError, match="shared single-desktop provider"):
        async with main.lifespan(SimpleNamespace()):
            pass

    assert initialized is False


class _FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeWuyingProvider:
    def __init__(self, status_code=200, error=None, alive_payload=None):
        self.status_code = status_code
        self.error = error
        self.calls = []
        self.alive_payload = alive_payload or {
            "version": "2026.08.31-run-lease-receipt-v12",
            "capabilities": sorted(main._REQUIRED_WUYING_CAPABILITIES),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def get_user_container(self, user_id):
        assert user_id == "default"
        return SimpleNamespace(id="desktop-1")

    async def forward_to_container(self, container_id, method, path, **kwargs):
        self.calls.append((container_id, method, path, kwargs))
        if self.error:
            raise self.error
        if path == "/alive":
            return _FakeResponse(200, self.alive_payload)
        return _FakeResponse(self.status_code)


async def test_wuying_readiness_requires_a_configured_api_key():
    provider = _FakeWuyingProvider()

    result = await main._wuying_readiness(
        _config(wuying_api_key=""),
        provider,
    )

    assert result == {
        "ready": False,
        "configured": False,
        "reachable": False,
        "authenticated": False,
        "reason": "api_key_missing",
    }
    assert provider.calls == []


@pytest.mark.parametrize(
    ("status_code", "ready", "reason"),
    [
        (200, True, None),
        (403, False, "api_key_rejected"),
        (500, False, "action_server_unready"),
    ],
)
async def test_wuying_readiness_probes_a_protected_endpoint(
    status_code,
    ready,
    reason,
):
    provider = _FakeWuyingProvider(status_code=status_code)

    result = await main._wuying_readiness(_config(), provider)

    assert result["ready"] is ready
    assert result["reachable"] is True
    assert result["authenticated"] is ready
    assert result.get("reason") == reason
    assert provider.calls == [
        (
            "desktop-1",
            "GET",
            "/alive",
            {"user_id": "default", "timeout": 3.0},
        ),
        (
            "desktop-1",
            "GET",
            "/system_info",
            {"user_id": "default", "timeout": 3.0},
        )
    ]


@pytest.mark.parametrize(
    ("alive_payload", "reason"),
    [
        (
            {
                "version": "legacy",
                "capabilities": ["run_fencing_v1"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "run_lease_receipt_unsupported",
        ),
        (
            {
                "version": "receipt-without-clock",
                "capabilities": [
                    "run_lease_receipt_v2",
                    "terminal_project_cwd_v1",
                ],
            },
            "action_server_clock_unverified",
        ),
        (
            {
                "version": "receipt-with-invalid-clock",
                "capabilities": [
                    "run_lease_receipt_v2",
                    "terminal_project_cwd_v1",
                ],
                "timestamp": "not-a-time",
            },
            "action_server_clock_unverified",
        ),
        (
            {
                "version": "clock-skewed",
                "capabilities": [
                    "run_lease_receipt_v2",
                    "terminal_project_cwd_v1",
                ],
                "timestamp": (
                    datetime.now(timezone.utc) + timedelta(minutes=2)
                ).isoformat(),
            },
            "action_server_clock_skew",
        ),
    ],
)
async def test_wuying_readiness_fails_closed_on_receipt_or_clock_contract(
    alive_payload,
    reason,
):
    provider = _FakeWuyingProvider(alive_payload=alive_payload)

    result = await main._wuying_readiness(_config(), provider)

    assert result["ready"] is False
    assert result["reachable"] is True
    assert result["authenticated"] is False
    assert result["reason"] == reason
    assert [call[2] for call in provider.calls] == ["/alive"]


async def test_wuying_readiness_requires_project_scoped_terminal_capability():
    provider = _FakeWuyingProvider(alive_payload={
        "version": "receipt-only",
        "capabilities": ["run_lease_receipt_v2"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    result = await main._wuying_readiness(_config(), provider)

    assert result["ready"] is False
    assert result["reachable"] is True
    assert result["authenticated"] is False
    assert result["receipt_capable"] is True
    assert result["project_terminal_capable"] is False
    assert result["reason"] == "terminal_project_cwd_unsupported"
    assert [call[2] for call in provider.calls] == ["/alive"]


async def test_wuying_readiness_requires_complete_product_capability_contract():
    capabilities = set(main._REQUIRED_WUYING_CAPABILITIES)
    capabilities.remove("mcp_supervisor_v1")
    provider = _FakeWuyingProvider(alive_payload={
        "version": "incomplete-v12",
        "capabilities": sorted(capabilities),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    result = await main._wuying_readiness(_config(), provider)

    assert result["ready"] is False
    assert result["reachable"] is True
    assert result["authenticated"] is False
    assert result["reason"] == "action_server_capabilities_missing"
    assert result["missing_capabilities"] == ["mcp_supervisor_v1"]
    assert [call[2] for call in provider.calls] == ["/alive"]


async def test_wuying_readiness_reports_transport_failure():
    provider = _FakeWuyingProvider(error=TimeoutError())

    result = await main._wuying_readiness(_config(), provider)

    assert result["ready"] is False
    assert result["reachable"] is False
    assert result["reason"] == "unreachable"


@pytest.mark.parametrize(
    "failed_check",
    [None, "database", "cron", "model_provider", "wuying"],
)
async def test_readiness_report_requires_every_dependency(monkeypatch, failed_check):
    from cron.service import cron_service
    import db.base as db_base

    async def database_ready():
        return failed_check != "database"

    async def wuying_ready(_config):
        ready = failed_check != "wuying"
        return {
            "ready": ready,
            "configured": True,
            "reachable": ready,
            "authenticated": ready,
        }

    monkeypatch.setattr(db_base, "database_schema_ready", database_ready)
    monkeypatch.setattr(main, "_wuying_readiness", wuying_ready)
    monkeypatch.setattr(
        cron_service,
        "readiness_status",
        lambda: {
            "ready": failed_check != "cron",
            "started": True,
            "heartbeat_fresh": failed_check != "cron",
            "last_tick_at": "2026-08-31T00:00:00+00:00",
        },
    )

    provider = {
        "openai": ProviderConfig(
            api_key="" if failed_check == "model_provider" else "model-secret",
            base_url="https://model-gateway.example/v1",
        )
    }
    report = await main._readiness_report(_config(provider=provider))

    assert report["status"] == ("ready" if failed_check is None else "not_ready")


@pytest.mark.parametrize(
    ("provider", "expected_reason"),
    [
        (
            ProviderConfig(
                api_key="",
                base_url="https://model-gateway.example/v1",
            ),
            "api_key_missing",
        ),
        (ProviderConfig(api_key="model-secret", base_url=""), "base_url_missing"),
    ],
)
def test_model_provider_readiness_rejects_incomplete_gateway_config(
    provider,
    expected_reason,
):
    result = main._model_provider_readiness(
        _config(provider={"openai": provider}),
    )

    assert result == {
        "configured": True,
        "ready": False,
        "reason": expected_reason,
    }
    assert set(result) == {"configured", "ready", "reason"}
    assert "model-secret" not in json.dumps(result)


def test_model_provider_readiness_accepts_complete_gateway_config():
    result = main._model_provider_readiness(_config())

    assert result == {
        "configured": True,
        "ready": True,
        "reason": "configured",
    }


def test_model_provider_readiness_resolves_the_default_models_provider_slot():
    config = _config(
        model="anthropic/claude-sonnet-4-20250514",
        provider={
            # A complete but unrelated slot must not make Anthropic ready.
            "openai": ProviderConfig(
                api_key="unrelated-secret",
                base_url="https://model-gateway.example/v1",
            ),
        },
    )

    assert main._model_provider_readiness(config) == {
        "configured": False,
        "ready": False,
        "reason": "provider_missing",
    }

    config.provider["anthropic"] = ProviderConfig(api_key="anthropic-secret")
    assert main._model_provider_readiness(config) == {
        "configured": True,
        "ready": True,
        "reason": "configured",
    }


@pytest.mark.parametrize(
    ("mode", "build_mode", "non_build_mode", "reason"),
    [
        ("legacy_eager", "legacy_eager", "legacy_eager", "explicit_legacy_rollback"),
        ("shadow", "shadow", "shadow", "shadow_observation"),
        ("portable", "portable", "shadow", "portable_active"),
        (
            "native_auto",
            "native_auto",
            "shadow",
            "native_auto_portable_fallback",
        ),
    ],
)
def test_tool_exposure_readiness_reports_effective_release_posture(
    mode,
    build_mode,
    non_build_mode,
    reason,
):
    result = main._tool_exposure_readiness(
        _config(tool_exposure=ToolExposureConfig(mode=mode)),
    )

    assert result["ready"] is True
    assert result["configured_mode"] == mode
    assert result["build_effective_mode"] == build_mode
    assert result["non_build_without_opt_in_mode"] == non_build_mode
    assert result["native_allowlists_present"] is False
    assert result["reason"] == reason
    assert result["limits"] == {
        "resident_hard_chars": 24_000,
        "active_hard_chars": 32_000,
        "native_wire_hard_chars": 128_000,
        "max_search_calls_per_step": 2,
        "max_reveals_per_step": 5,
        "max_search_result_chars_per_step": 2_000,
    }


def test_tool_exposure_readiness_never_discloses_native_allowlist_values():
    endpoint = "https://private-gateway.example/v1"
    model = "openai/private-model"
    result = main._tool_exposure_readiness(
        _config(
            tool_exposure=ToolExposureConfig(
                mode="native_auto",
                native_endpoint_allowlist=[endpoint],
                native_model_allowlist=[model],
            ),
        ),
    )

    assert result["native_allowlists_present"] is True
    assert result["native_endpoint_allowlist_entries"] == 1
    assert result["native_model_allowlist_entries"] == 1
    assert result["reason"] == "native_binding_gate_pending"
    serialized = json.dumps(result)
    assert endpoint not in serialized
    assert model not in serialized


async def test_ready_endpoint_returns_non_200_when_gate_fails(monkeypatch):
    config = _config()
    monkeypatch.setattr("core.config.get_config", lambda: config)

    async def not_ready(_config):
        return {
            "status": "not_ready",
            "version": "0.1.0",
            "checks": {"database": {"ready": False}},
        }

    monkeypatch.setattr(main, "_readiness_report", not_ready)
    app = main.create_app()
    endpoint = next(route.endpoint for route in app.routes if route.path == "/ready")

    response = await endpoint()

    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "not_ready"


@pytest.mark.parametrize(
    ("provider", "expected_status", "expected_reason"),
    [
        (
            ProviderConfig(
                api_key="",
                base_url="https://model-gateway.example/v1",
            ),
            503,
            "api_key_missing",
        ),
        (
            ProviderConfig(api_key="model-secret", base_url=""),
            503,
            "base_url_missing",
        ),
        (
            ProviderConfig(
                api_key="model-secret",
                base_url="https://model-gateway.example/v1",
            ),
            200,
            "configured",
        ),
    ],
)
async def test_ready_endpoint_gates_on_default_model_provider(
    monkeypatch,
    provider,
    expected_status,
    expected_reason,
):
    from cron.service import cron_service
    import db.base as db_base

    config = _config(provider={"openai": provider})
    monkeypatch.setattr("core.config.get_config", lambda: config)

    async def database_ready():
        return True

    async def wuying_ready(_config):
        return {
            "ready": True,
            "configured": True,
            "reachable": True,
            "authenticated": True,
        }

    monkeypatch.setattr(db_base, "database_schema_ready", database_ready)
    monkeypatch.setattr(main, "_wuying_readiness", wuying_ready)
    monkeypatch.setattr(
        cron_service,
        "readiness_status",
        lambda: {
            "ready": True,
            "started": True,
            "heartbeat_fresh": True,
            "last_tick_at": "2026-08-31T00:00:00+00:00",
        },
    )

    app = main.create_app()
    endpoint = next(route.endpoint for route in app.routes if route.path == "/ready")
    response = await endpoint()
    body = json.loads(response.body)

    assert response.status_code == expected_status
    assert body["checks"]["model_provider"] == {
        "configured": True,
        "ready": expected_status == 200,
        "reason": expected_reason,
    }
    assert body["checks"]["tool_exposure"]["configured_mode"] == "portable"
    assert body["checks"]["tool_exposure"]["build_effective_mode"] == "portable"
    assert "model-secret" not in json.dumps(body)
