"""Canonical preferences survive old chat instructions and dispatch refresh."""
from memory.orchestrator import apply_settings_system, SETTINGS_HEADER


def test_current_settings_replace_the_previous_snapshot_without_loading_history():
    system = ["base system"]
    old = {"_semantic_packet": True, "settings": {"revision": 2, "language": "English", "address": "林姐"}}
    current = {"_semantic_packet": True, "settings": {"revision": 3, "language": "中文", "address": "老板"},
               "items": [{"text": "Always reply in English"}]}
    result = apply_settings_system(apply_settings_system(system, old), current)
    assert result[0] == "base system"
    assert sum(part.startswith(SETTINGS_HEADER) for part in result) == 1
    assert "revision 3" in result[-1] and "中文" in result[-1] and "老板" in result[-1]
    assert "English" not in result[-1]
    assert "CURRENT human request" in result[-1]
    assert system == ["base system"]


def test_cleared_language_means_follow_current_input_and_missing_snapshot_is_not_reused():
    system = apply_settings_system(["base"], {"_semantic_packet": True,
        "settings": {"revision": 4, "language": ""}})
    assert "cleared their fixed" in system[-1]
    assert apply_settings_system(system, {"_semantic_packet": True, "settings": {}}) == ["base"]
    assert apply_settings_system(["base"], {"items": [{"settings": {"language": "English"}}]}) == ["base"]
