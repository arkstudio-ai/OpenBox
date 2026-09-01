"""Desktop/browser components respect the isolated WUYING runner boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandbox.browser import (
    BROWSER_RUNTIME_DIR,
    _chrome_launch_script,
    _policy_install_script,
    _relay_start_script,
    browser_policy_provision_script,
)
from sandbox import assets
from sandbox.assets import asset_cli_provision_script
from sandbox.client import ExecuteResult
from sandbox.desktop import (
    APT_PACKAGES,
    SHOT_PATH,
    _install_script,
    desktop_provision_script,
    ensure_desktop_tools,
)


def test_helper_install_uses_private_runner_staging_and_never_sudo(tmp_path):
    home = tmp_path / "home"
    staging = tmp_path / "tmp"
    home.mkdir()
    staging.mkdir()
    script = _install_script("obx-test-helper", "#!/bin/sh\nprintf ready\\n")

    assert "/tmp/.obx-test-helper" not in script
    assert "mktemp" in script
    assert "sudo" not in script

    completed = subprocess.run(
        ["sh", "-c", script],
        env={
            "HOME": str(home),
            "TMPDIR": str(staging),
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    helper = home / ".local" / "bin" / "obx-test-helper"
    assert helper.read_text() == "#!/bin/sh\nprintf ready\\n"
    assert not list(staging.iterdir())


def test_root_provisioner_contains_the_complete_desktop_component_set():
    script = desktop_provision_script()

    for package in [*APT_PACKAGES, "python3-pil"]:
        assert package in script
    for tool in ("xdotool", "scrot", "xdpyinfo", "wmctrl"):
        assert f"command -v {tool}" in script
    assert "/usr/local/bin/obx-x" in script
    assert "/usr/local/bin/obx-shot" in script
    assert "sudo" not in script
    for stale in (
        "/tmp/.obx-x",
        "/tmp/.obx-shot",
        "/tmp/.obx-file",
        "/tmp/obx-screen.png",
        SHOT_PATH,
    ):
        assert stale in script


def test_incremental_deploy_runs_desktop_repair_before_v12_restart():
    backend_root = Path(__file__).resolve().parents[2]
    deploy = (backend_root / "scripts" / "wuying_deploy_action_server.py").read_text()
    action_server = (backend_root.parent / "container" / "action_server.py").read_text()

    provision = "d.run(desktop_provision_script(), timeout=900)"
    assert provision in deploy
    assert deploy.index(provision) < deploy.index("systemctl restart")
    assert "--desktop-id does not match WUYING_DESKTOP_ID" in deploy
    assert "target desktop profile" in deploy
    assert 'ACTION_SERVER_VERSION = "2026.08.31-run-lease-receipt-v12"' in action_server


@pytest.mark.asyncio
async def test_runtime_reports_missing_packages_without_trying_to_gain_root():
    class Client:
        def __init__(self):
            self.commands: list[str] = []

        async def execute(self, command: str, timeout: int = 120):
            self.commands.append(command)
            if "X11-unix" in command:
                return ExecuteResult(exit_code=0, stdout="yes\n", stderr="")
            return ExecuteResult(exit_code=0, stdout="scrot\n", stderr="")

    client = Client()
    with pytest.raises(RuntimeError, match="root-only component installer"):
        await ensure_desktop_tools(client, "missing-components")

    assert all("sudo" not in command for command in client.commands)
    assert all("apt-get" not in command for command in client.commands)


def test_browser_runtime_state_and_launch_stay_in_scoped_runner_home():
    policy = _policy_install_script()
    chrome = _chrome_launch_script()
    relay = _relay_start_script("local")

    assert "/tmp/.obx-chrome-policy" not in policy
    assert f'$HOME/{BROWSER_RUNTIME_DIR}' in policy
    assert f'$HOME/{BROWSER_RUNTIME_DIR}' in chrome
    assert f'$HOME/{BROWSER_RUNTIME_DIR}' in relay
    assert "$HOME/.config/obx-chrome" in chrome
    assert "sudo -u" not in chrome
    assert "/tmp/obx-chrome.log" not in chrome
    assert "/tmp/obx-relay" not in relay
    assert SHOT_PATH == "/tmp/obx-sandbox-screen.png"


def test_browser_machine_policy_has_a_separate_root_provisioner():
    runtime = _policy_install_script()
    provision = browser_policy_provision_script()

    assert "[ -w \"$d\" ]" in runtime
    assert "/etc/opt/chrome/policies/managed/openbox-automation.json" in provision
    assert "chown root:root" in provision
    assert "sudo" not in provision


@pytest.mark.asyncio
async def test_asset_helper_uses_private_runner_staging_without_sudo(tmp_path):
    home = tmp_path / "home"
    staging = tmp_path / "tmp"
    home.mkdir()
    staging.mkdir()

    class Client:
        async def execute(self, command: str, timeout: int = 30):
            completed = subprocess.run(
                ["sh", "-c", command],
                env={
                    "HOME": str(home),
                    "TMPDIR": str(staging),
                    "PATH": "/usr/bin:/bin",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            assert "/tmp/.obx-file" not in command
            assert "sudo" not in command
            return SimpleNamespace(
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )

    assets._installed.discard("asset-private-staging")
    await assets.ensure_cli(Client(), "asset-private-staging")

    assert (home / ".local" / "bin" / "obx-file").is_file()
    assert not list(staging.iterdir())


def test_asset_helper_has_a_root_only_deployment_provisioner():
    provision = asset_cli_provision_script()

    assert "/usr/local/bin/obx-file" in provision
    assert "mktemp" in provision
    assert "chown root:root" in provision
    assert "sudo" not in provision
