from agent.prompt_visibility import build_tool_visibility_fragment


def test_empty_frontier_overrides_generic_tool_instructions():
    fragment = build_tool_visibility_fragment([], strategy="portable")
    assert "No tools are materialized" in fragment
    assert "Answer in text only" in fragment


def test_portable_discovery_instruction_is_present_only_when_callable():
    with_search = build_tool_visibility_fragment(
        ["read", "capability_search"], strategy="portable", deferred_count=5
    )
    without_search = build_tool_visibility_fragment(
        ["read"], strategy="portable", deferred_count=5
    )
    assert "Use capability_search" in with_search
    assert "capability_search" not in without_search


def test_native_and_portable_discovery_are_never_both_instructed():
    fragment = build_tool_visibility_fragment(
        ["read"], strategy="native_openai", deferred_count=5
    )
    assert "native tool search" in fragment
    assert "capability_search" not in fragment


def test_tool_specific_rules_only_name_visible_tools():
    fragment = build_tool_visibility_fragment(["read", "cron"], strategy="portable")
    assert "crontab" in fragment
    assert "web_search" not in fragment
    assert "computer" not in fragment
    assert "todo_write" not in fragment
