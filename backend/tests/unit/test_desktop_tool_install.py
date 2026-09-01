"""Desktop helper installation must survive a shared sticky /tmp directory."""

from sandbox.desktop import _install_script


def test_install_script_uses_a_unique_staging_file():
    command = _install_script("obx-display", "#!/bin/sh\necho ok\n")

    assert 'mktemp "${TMPDIR:-/tmp}/.openbox-obx-display.XXXXXX"' in command
    assert '> /tmp/.obx-display' not in command
    assert 'install -m 0755 "$tmp" "$HOME/.local/bin/obx-display"' in command
    assert 'rm -f "$tmp"' in command
