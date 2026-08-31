"""Fast desktop interaction: local action batches and adaptive screenshots."""

import pytest
from pydantic import ValidationError

from sandbox.client import ExecuteResult
from sandbox.desktop import OBX_DISPLAY_SCRIPT, OBX_SHOT_SCRIPT, take_stable_screenshot
from tool.computer import ComputerAction, ComputerArgs, computer_tool, execute


GEOMETRY = {
    "native": [1024, 768],
    "scaled": [1024, 768],
    "bytes": 1234,
    "stable": True,
    "settle_ms": 280,
    "frame_delta": 0.0002,
}


class _Sandbox:
    base_url = "http://desktop.test"

    def __init__(self):
        self.commands: list[tuple[str, int]] = []

    async def execute(self, command: str, timeout: int = 120):
        self.commands.append((command, timeout))
        return ExecuteResult(exit_code=0, stdout="", stderr="")


class _Ctx:
    def __init__(self):
        self.sandbox = _Sandbox()
        self.user_id = "u1"
        self.session_id = "s1"
        self.part_id = "p1"


def test_batch_is_in_the_tool_schema_and_guidance():
    from agent.llm import _inline_refs

    schema = _inline_refs(ComputerArgs.model_json_schema())
    assert schema["type"] == "object"
    assert "$ref" not in str(schema)
    assert len(schema["oneOf"]) == 2
    batch = next(
        branch for branch in schema["oneOf"]
        if branch["properties"]["action"].get("const") == "batch"
    )
    single = next(branch for branch in schema["oneOf"] if branch is not batch)
    assert set(batch["properties"]) == {"action", "actions"}
    assert batch["properties"]["actions"]["maxItems"] == 12
    assert batch["required"] == ["action", "actions"]
    assert batch["additionalProperties"] is False
    assert "actions" not in single["properties"]
    assert "batch" not in single["properties"]["action"]["enum"]
    assert single["additionalProperties"] is False
    assert "one final screenshot" in computer_tool.description
    assert "OSS" in computer_tool.description
    assert 'action: "batch"' in computer_tool.description
    assert "generic parallel" in computer_tool.description


def test_runtime_rejects_ambiguous_single_and_batch_shapes():
    nested = [ComputerAction(action="key", text="Return")]
    with pytest.raises(ValidationError, match="only valid when action='batch'"):
        ComputerArgs(action="type", text="Settings", actions=nested)
    with pytest.raises(ValidationError, match="accepts only action and actions"):
        ComputerArgs(action="batch", text="Settings", actions=nested)


@pytest.mark.asyncio
async def test_tool_wrapper_rejects_ignored_nested_actions_before_execution():
    ctx = _Ctx()
    result = await computer_tool.execute({
        "action": "type",
        "text": "Settings",
        "actions": [{"action": "key", "text": "Return"}],
    }, ctx)

    assert "Parameter validation error" in result.output
    assert "only valid when action='batch'" in result.output
    assert not ctx.sandbox.commands


def test_screenshot_helper_uses_fast_xcb_capture_with_scrot_fallback():
    assert "ImageGrab.grab()" in OBX_SHOT_SCRIPT
    assert 'return image.convert("RGB"), "xcb"' in OBX_SHOT_SCRIPT
    assert 'return image, "scrot"' in OBX_SHOT_SCRIPT
    assert "compress_level=3" in OBX_SHOT_SCRIPT


def test_display_helper_pins_linux_x11_to_recommended_xga():
    assert 'target="1024x768"' in OBX_DISPLAY_SCRIPT
    assert 'xrandr -s "$target"' in OBX_DISPLAY_SCRIPT
    assert "xdpyinfo" in OBX_DISPLAY_SCRIPT


@pytest.mark.asyncio
async def test_batch_runs_in_one_sandbox_call_and_uploads_one_final_frame(monkeypatch):
    import core.oss as oss_mod
    import tool.computer as computer_mod

    ctx = _Ctx()
    captures: list[str] = []
    uploads: list[dict] = []

    monkeypatch.setattr(oss_mod, "get_oss", lambda: object())

    async def prepared(*_args):
        return None

    async def geometry(*_args):
        return GEOMETRY

    async def stable(*_args):
        captures.append("capture")
        return GEOMETRY

    async def attach(_ctx, observed):
        uploads.append(observed)
        return "1024x768"

    monkeypatch.setattr(computer_mod, "_prepare", prepared)
    monkeypatch.setattr(computer_mod, "_geometry", geometry)
    monkeypatch.setattr(computer_mod, "take_stable_screenshot", stable)
    monkeypatch.setattr(computer_mod, "_attach_screenshot", attach)

    result = await execute(
        ComputerArgs(
            action="batch",
            actions=[
                ComputerAction(action="left_click", coordinate=[100, 200]),
                ComputerAction(action="type", text="penguin"),
                ComputerAction(action="key", text="Return"),
            ],
        ),
        ctx,
    )

    assert len(ctx.sandbox.commands) == 1
    command, timeout = ctx.sandbox.commands[0]
    assert "obx-x sh -c" in command
    assert "obx-display" in command
    assert "xdotool mousemove 100 200" in command
    assert "xdotool type" in command
    assert "xdotool key" in command
    assert timeout == 120
    assert captures == ["capture"]
    assert uploads == [GEOMETRY]
    assert result.metadata["batch_size"] == 3
    assert result.metadata["timings"]["total_ms"] >= 0
    assert "via OSS" in result.output
    assert "Timings:" in result.output
    assert "total=" in result.output


@pytest.mark.asyncio
async def test_invalid_batch_stops_before_touching_the_desktop(monkeypatch):
    import core.oss as oss_mod
    import tool.computer as computer_mod

    ctx = _Ctx()
    monkeypatch.setattr(oss_mod, "get_oss", lambda: object())

    async def prepared(*_args):
        return None

    async def geometry(*_args):
        return GEOMETRY

    monkeypatch.setattr(computer_mod, "_prepare", prepared)
    monkeypatch.setattr(computer_mod, "_geometry", geometry)

    result = await execute(
        ComputerArgs(
            action="batch",
            actions=[ComputerAction(action="left_click")],
        ),
        ctx,
    )

    assert "batch action 1 invalid" in result.title
    assert not ctx.sandbox.commands


def test_batch_caps_total_wait_time_and_releases_a_held_mouse_button():
    from tool.computer import _build_batch

    too_slow = _build_batch(
        ComputerArgs(
            action="batch",
            actions=[ComputerAction(action="wait", duration=10) for _ in range(4)],
        ),
        GEOMETRY,
    )
    assert "at most 30 seconds" in too_slow.output

    command, _ = _build_batch(
        ComputerArgs(
            action="batch",
            actions=[
                ComputerAction(action="left_mouse_down"),
                ComputerAction(action="mouse_move", coordinate=[20, 20]),
                ComputerAction(action="left_mouse_up"),
            ],
        ),
        GEOMETRY,
    )
    assert "trap" in command
    assert "mouseup 1" in command


@pytest.mark.asyncio
async def test_stable_capture_requests_local_sampling_and_parses_metadata():
    class Client:
        def __init__(self):
            self.command = ""

        async def execute(self, command, timeout):
            self.command = command
            return ExecuteResult(
                exit_code=0,
                stdout='{"native":[1024,768],"scaled":[1024,768],"bytes":99,'
                '"stable":true,"settle_ms":240,"frame_delta":0.0001}\n',
                stderr="",
            )

    client = Client()
    observed = await take_stable_screenshot(
        client,
        timeout_ms=900,
        interval_ms=100,
        threshold=0.004,
    )

    assert "obx-display" in client.command
    assert "obx-shot 1024 768 /tmp/obx-screen.png 900 100 0.004" in client.command
    assert observed["stable"] is True
    assert observed["settle_ms"] == 240


@pytest.mark.asyncio
async def test_prepare_probe_is_reused_within_ttl(monkeypatch):
    import tool.computer as computer_mod

    ctx = _Ctx()
    key = "ttl-test-desktop"
    computer_mod._probe_valid_until.pop(key, None)

    async def ready(*_args):
        return None

    monkeypatch.setattr(computer_mod, "ensure_desktop_tools", ready)
    await computer_mod._prepare(ctx, key)
    await computer_mod._prepare(ctx, key)

    probes = [command for command, _timeout in ctx.sandbox.commands if "command -v obx-shot" in command]
    assert len(probes) == 1
