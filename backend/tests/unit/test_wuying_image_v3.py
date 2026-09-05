"""The v3 image path bakes and verifies the unattended 1080p guard."""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import wuying_bootstrap  # noqa: E402
import wuying_image_verify  # noqa: E402


class FakeDesktop:
    def __init__(self):
        self.commands = []

    def run(self, command, timeout):
        self.commands.append((command, timeout))


def test_image_mode_bakes_enabled_display_guard():
    desktop = FakeDesktop()
    wuying_bootstrap.install_desktop_tools(desktop)
    command, timeout = desktop.commands[-1]
    assert timeout == 900
    assert "/usr/local/bin/obx-display-guard" in command
    assert "/etc/systemd/system/obx-display-guard.service" in command
    assert "systemctl enable --now obx-display-guard" in command


def test_image_verifier_requires_enabled_display_guard():
    script = wuying_image_verify.VERIFY_SCRIPT
    assert "check display_guard_script" in script
    assert "check display_guard_enabled" in script
    assert "check display_guard_unit" in script
