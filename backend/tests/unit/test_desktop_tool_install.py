"""Desktop helper installation must survive a shared sticky /tmp directory."""

from sandbox.desktop import _install_script


def test_install_script_uses_a_unique_staging_file():
    command = _install_script("obx-display", "#!/bin/sh\necho ok\n")

    assert 'mktemp "${TMPDIR:-/tmp}/.obx-display.XXXXXX"' in command
    assert '> /tmp/.obx-display' not in command
    assert 'install -m755 "$stage" /usr/local/bin/obx-display' in command
    assert 'rm -f -- "$stage"' in command
