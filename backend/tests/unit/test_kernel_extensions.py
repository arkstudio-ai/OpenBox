"""The kernel's extension registry: empty by default, built and sealed at startup."""
import copy

import pytest

from agent import extensions as ext
from tool.tool import ToolContext


def test_an_empty_registry_has_inert_ports():
    with ext.use(ext.KernelExtensions()) as registry:
        assert registry.recorder is ext.NULL_RECORDER
        assert registry.call_wrapper is ext.NULL_CALL_WRAPPER
        assert registry.timing is None
        assert registry.recorder.enabled("u") is False
        assert registry.recorder.current() is None


def test_plugins_register_once_and_the_installed_registry_is_sealed():
    class Plugin:
        name = "probe"

        def register(self, registry):
            registry.add_prompt_section(object())

    registry = ext.KernelExtensions().add(Plugin())
    with pytest.raises(ValueError):
        registry.add(Plugin())
    with ext.use(registry):
        assert ext.extensions() is registry
        with pytest.raises(RuntimeError):
            registry.add_tool_pack(object())
    assert ext.extensions() is not registry


async def test_recovery_hooks_run_in_order_and_one_failure_does_not_stop_the_next():
    calls = []

    class Hook:
        def __init__(self, name, result):
            self.name, self.result = name, result

        async def recover(self):
            calls.append(self.name)
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    registry = ext.KernelExtensions()
    for hook in (Hook("a", 2), Hook("b", RuntimeError("down")), Hook("c", 1)):
        registry.add_recovery_hook(hook)
    with ext.use(registry):
        assert await ext.run_recovery_hooks() == 3
    assert calls == ["a", "b", "c"]


def test_plugin_state_writes_never_reach_the_context_a_copy_came_from():
    parent = ToolContext(session_id="s")
    ext.set_plugin_state(parent, "probe.value", 1)
    child = copy.copy(parent)
    ext.set_plugin_state(child, "probe.value", 2)
    assert ext.plugin_state(parent, "probe.value") == 1
    assert ext.plugin_state(child, "probe.value") == 2
    assert ext.plugin_state(ToolContext(), "probe.value", "absent") == "absent"
