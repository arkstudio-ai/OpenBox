from agent.native_tool_search import NativeCapabilityCache, decide_native_adapter
from session.internal_parts import ProviderCapabilityBinding


def test_anthropic_litellm_native_tool_search_is_capability_gated_to_portable():
    binding = ProviderCapabilityBinding(
        provider="anthropic",
        endpoint="https://api.anthropic.com/path:abc",
        account_id="account-config:abc",
        api_version="2023-06-01",
        model="anthropic/claude-sonnet-4-6",
        dialect="litellm",
        beta_headers=("anthropic-beta=tool-search",),
    )
    decision = decide_native_adapter(
        requested_mode="native_auto",
        model_id="anthropic/claude-sonnet-4-6",
        configured_endpoint="https://api.anthropic.com/v1",
        binding=binding,
        endpoint_allowlist=["https://api.anthropic.com/v1"],
        model_allowlist=["claude-*"],
        config_generation="cfg-1",
        session_id="session-1",
        cache=NativeCapabilityCache(),
        has_deferred_tools=True,
        catalogue_wire_chars=10_000,
    )

    assert decision.enabled is False
    assert decision.probe is False
    assert decision.reason == "adapter_not_verified"
