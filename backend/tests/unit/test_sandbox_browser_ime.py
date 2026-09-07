"""The Wuying browser must join an input-method session, not only X11."""

from sandbox.browser import CHROME_LAUNCH_LOCK, _chrome_launch_script


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
    assert script.count("--remote-debugging-address=127.0.0.1") == 2


def test_chrome_launch_does_not_require_sudo_in_a_restricted_container():
    script = _chrome_launch_script()

    assert 'if sudo -n -u "$U" true' in script
    assert 'U="$CURRENT_U"' in script
    assert "setsid $SUDO env" in script


def test_chrome_launch_does_not_pick_another_users_latest_profile():
    script = _chrome_launch_script()
    assert 'ls -dt /workspace/openbox/users/*' not in script
    assert '"$CURRENT_U" = "$U"' in script


def test_active_managed_profile_is_not_mutated_or_force_closed():
    script = _chrome_launch_script()
    guard = script.index('managed browser profile is in use')
    assert guard < script.index('PREF="$PROF/Default/Preferences"')
    assert guard < script.index('rm -rf "$PROF/Default/Sessions"')
    assert 'kill -0 "$LOCK_PID"' in script
    assert 'kill -TERM "$LOCK_PID"' not in script


def test_unresponsive_automation_browser_is_reconciled_before_launch():
    script = _chrome_launch_script()
    assert f'exec 9>{CHROME_LAUNCH_LOCK}' in script
    assert '--remote-debugging-port=9333' in script
    assert "any(arg.startswith('--type=')" in script
    assert 'google-chrome.bossip-real' in script
    assert "executable == 'runuser'" in script
    assert 'text=shlex.split(text[0])' in script
    assert 'recovering unresponsive OpenBox Chrome pid(s)' in script
    assert script.index('AUTOMATION_ROOTS=') < script.index('PREF="$PROF/Default/Preferences"')
    assert 'unable to stop stale OpenBox Chrome' in script
    assert 'Chrome launched but its renderer did not become healthy' in script
    assert script.count('9>&-') == 2
