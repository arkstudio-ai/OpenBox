"""The v3 image path bakes and verifies the unattended 1080p guard."""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import wuying_bootstrap  # noqa: E402
import wuying_image_verify  # noqa: E402


class FakeDesktop:
    def __init__(self):
        self.commands = []
        self.uploads = []

    def run(self, command, timeout):
        self.commands.append((command, timeout))

    def put(self, local, remote, mode="644"):
        self.uploads.append((local, remote, mode))


def test_image_mode_installs_missing_baseline_packages():
    desktop = FakeDesktop()
    wuying_bootstrap.install_baseline_packages(desktop)
    command, timeout = desktop.commands[-1]
    assert desktop.uploads == [
        (
            wuying_bootstrap.IMAGE_BASELINE,
            "/tmp/openbox-image-baseline.txt",
            "600",
        )
    ]
    assert timeout == 14_400
    assert "comm -23" in command
    assert "xargs -r apt-get install" in command


def test_image_mode_bakes_enabled_display_guard():
    desktop = FakeDesktop()
    wuying_bootstrap.install_desktop_tools(desktop)
    commands = "\n".join(command for command, _ in desktop.commands)
    assert desktop.commands[0][1] == 900
    assert all(len(command) < 16_000 for command, _ in desktop.commands)
    assert "/usr/local/bin/obx-display-guard" in commands
    assert "/etc/systemd/system/obx-display-guard.service" in commands
    assert "systemctl enable --now obx-display-guard" in commands


def test_image_verifier_requires_enabled_display_guard():
    script = wuying_image_verify.VERIFY_SCRIPT
    assert "check display_guard_script" in script
    assert "check display_guard_enabled" in script
    assert "check display_guard_unit" in script


def test_image_mode_sanitizes_reused_desktop_state():
    desktop = FakeDesktop()
    wuying_bootstrap.install_image_services(desktop)
    command, timeout = desktop.commands[-1]
    assert timeout == 300
    assert "find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf" in command
    assert "find /data -mindepth 1 -maxdepth 1 -exec rm -rf" in command
    assert "rm -rf /root/.ssh" in command


def test_image_verifier_requires_empty_user_state():
    script = wuying_image_verify.VERIFY_SCRIPT
    assert "check workspace_empty" in script
    assert "check data_has_no_user_files" in script
    assert "OPENBOX_IMAGE_VERIFY_COMPLETE" in script
