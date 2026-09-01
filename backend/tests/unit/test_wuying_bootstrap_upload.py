"""WUYING deployment must preserve the last-known-good remote artifact."""

from scripts import wuying_bootstrap


def test_desktop_put_stages_and_atomically_renames(tmp_path, monkeypatch) -> None:
    source = tmp_path / "action_server.py"
    source.write_text("print('ready')\n", encoding="utf-8")
    commands: list[str] = []
    desktop = wuying_bootstrap.Desktop("ecd-test", "cn-test")

    monkeypatch.setattr(wuying_bootstrap.secrets, "token_hex", lambda _n: "abc123")
    monkeypatch.setattr(
        desktop,
        "run",
        lambda command, timeout=300, check=True: commands.append(command) or "",
    )

    desktop.put(source, "/opt/action_server/action_server.py", mode="640")

    assert len(commands) >= 2
    assert all(
        "/opt/action_server/action_server.py" not in command
        for command in commands[:-1]
    )
    final = commands[-1]
    assert "set -e" in final and "trap " in final
    assert "> /opt/action_server/action_server.py.upload-abc123" in final
    assert (
        "mv -f -- /opt/action_server/action_server.py.upload-abc123 "
        "/opt/action_server/action_server.py"
    ) in final
    assert "chmod 640 /opt/action_server/action_server.py.upload-abc123" in final


def test_desktop_put_rejects_non_numeric_mode(tmp_path) -> None:
    source = tmp_path / "payload"
    source.write_bytes(b"payload")
    desktop = wuying_bootstrap.Desktop("ecd-test", "cn-test")
    desktop.run = lambda *_args, **_kwargs: ""  # type: ignore[method-assign]

    try:
        desktop.put(source, "/opt/openbox/payload", mode="644; id")
    except ValueError as exc:
        assert "numeric" in str(exc)
    else:  # pragma: no cover - invariant guard
        raise AssertionError("non-numeric remote mode was accepted")
