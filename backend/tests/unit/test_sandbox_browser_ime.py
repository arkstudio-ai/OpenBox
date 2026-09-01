"""The Wuying browser must join an input-method session, not only X11."""

from sandbox.browser import _chrome_launch_script


def test_chrome_launches_with_an_isolated_ibus_session_when_available():
    script = _chrome_launch_script()

    assert "dbus-run-session" in script
    assert "/usr/libexec/ibus-engine-libpinyin" in script
    assert "GTK_IM_MODULE=ibus" in script
    assert "XMODIFIERS=@im=ibus" in script
    assert "ibus-daemon --replace --xim" in script
    assert "ibus engine libpinyin" in script
    assert "init-chinese false" in script
    assert 'main-switch "<Shift>"' in script


def test_chrome_launch_keeps_a_non_ibus_fallback():
    script = _chrome_launch_script()

    assert "Minimal/headless images may not carry IBus" in script
    # Both the IBus branch and the fallback must expose the automation port.
    assert script.count("--remote-debugging-port=9333") == 2


def test_chrome_launch_does_not_require_sudo_in_a_restricted_container():
    script = _chrome_launch_script()

    assert "sudo -n" not in script
    assert "setsid env" in script
