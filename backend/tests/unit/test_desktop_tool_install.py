"""Desktop helper installation must survive a shared sticky /tmp directory."""

from sandbox.desktop import _install_script


def test_install_script_uses_a_unique_staging_file():
    command = _install_script("obx-display", "#!/bin/sh\necho ok\n")

    assert 'mktemp "${TMPDIR:-/tmp}/.obx-display.XXXXXX"' in command
    assert '> /tmp/.obx-display' not in command
    assert 'install -m755 "$stage" /usr/local/bin/obx-display' in command
    assert 'rm -f -- "$stage"' in command


def test_guard_install_is_idempotent_and_best_effort():
    from sandbox.desktop import GUARD_UNIT_PATH, _install_guard_service

    command = _install_guard_service()

    # Skips cleanly instead of failing on sandboxes without root or systemd.
    assert "sudo -n true 2>/dev/null || { echo guard-skipped-no-root; exit 0; }" in command
    assert "command -v systemctl" in command
    # Only rewrites and restarts when the content actually changed.
    assert 'cmp -s "$stage/script" /usr/local/bin/obx-display-guard' in command
    assert f'cmp -s "$stage/unit" {GUARD_UNIT_PATH}' in command
    assert "systemctl daemon-reload" in command
    assert "systemctl enable --now obx-display-guard" in command
    assert 'if [ "$changed" = 1 ]; then sudo -n systemctl restart obx-display-guard; fi' in command
    assert 'rm -rf -- "$stage"' in command


def test_guard_script_pins_1080p_as_the_session_owner():
    from sandbox.desktop import OBX_DISPLAY_GUARD_SCRIPT, OBX_DISPLAY_GUARD_UNIT

    assert 'target="1920x1080"' in OBX_DISPLAY_GUARD_SCRIPT
    assert "interval=3" in OBX_DISPLAY_GUARD_SCRIPT
    # Re-discovers DISPLAY/XAUTHORITY every tick: the xauth path is per boot.
    assert "DISPLAY= XAUTHORITY= obx-x" in OBX_DISPLAY_GUARD_SCRIPT
    assert 'runuser -u "$owner" -- env DISPLAY="$DISPLAY" XAUTHORITY="$XAUTHORITY" obx-display' in (
        OBX_DISPLAY_GUARD_SCRIPT
    )
    # Re-reads the size after obx-display and backs off instead of flapping.
    assert 'if [ "$now" = "$target" ]; then' in OBX_DISPLAY_GUARD_SCRIPT
    assert 'sleep "$backoff"' in OBX_DISPLAY_GUARD_SCRIPT
    assert "backoff=60" in OBX_DISPLAY_GUARD_SCRIPT
    assert "ExecStart=/usr/local/bin/obx-display-guard" in OBX_DISPLAY_GUARD_UNIT
    assert "Restart=always" in OBX_DISPLAY_GUARD_UNIT


async def _run_ensure(monkeypatch, outcome: str, exit_code: int = 0):
    from sandbox import desktop

    class Result:
        def __init__(self, stdout, code=0):
            self.exit_code = code
            self.stdout = stdout
            self.stderr = ""

    class Client:
        def __init__(self):
            self.commands: list[str] = []

        async def execute(self, command, timeout=120):
            self.commands.append(command)
            if "obx-display-guard" in command and "systemctl" in command:
                return Result(outcome + "\n", exit_code)
            return Result("yes\n" if "X11-unix" in command else "")

    monkeypatch.setattr(desktop, "_ready", set())
    client = Client()
    await desktop.ensure_desktop_tools(client, "desk-1")
    return client


def test_desktop_setup_installs_the_guard_after_the_helpers(monkeypatch):
    import asyncio

    client = asyncio.run(_run_ensure(monkeypatch, "guard-active-changed-1"))

    guard = [i for i, c in enumerate(client.commands) if "systemctl enable --now obx-display-guard" in c]
    helper = [i for i, c in enumerate(client.commands) if "/usr/local/bin/obx-display " in c]
    assert len(guard) == 1
    assert helper and helper[0] < guard[0]


def test_desktop_setup_survives_a_guard_that_cannot_run(monkeypatch):
    import asyncio

    from sandbox import desktop

    warnings: list[str] = []
    monkeypatch.setattr(desktop.log, "warning", lambda msg, *a, **k: warnings.append(str(msg)))

    # Must not raise: the computer tool pins the mode itself before actions.
    asyncio.run(_run_ensure(monkeypatch, "guard-failed-changed-1", exit_code=1))

    assert any("display guard not running" in w for w in warnings)
