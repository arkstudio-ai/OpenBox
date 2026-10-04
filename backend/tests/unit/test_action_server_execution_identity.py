"""Fail-closed executor setup; kernel enforcement is covered in Linux tests."""
import json
import types

import pytest

from tests.unit.test_action_server_desktop_lease import server
import execution_identity as identity


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("OPENBOX_EXECUTOR_USER", "sandbox")
    monkeypatch.setattr(identity.sys, "platform", "linux")
    monkeypatch.setattr(identity.os, "geteuid", lambda: 0)
    account = types.SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="sandbox", pw_dir="/home/sandbox")
    monkeypatch.setattr(identity.pwd, "getpwnam", lambda _name: account)
    return account


def test_legacy_execution_is_explicit_and_does_not_claim_isolation(monkeypatch):
    monkeypatch.delenv("OPENBOX_EXECUTOR_USER", raising=False)
    assert identity.prepare_child(["printf", "ok"], {"PATH": "/fixture"}) == (["printf", "ok"], {"PATH": "/fixture"})
    identity.validate_configuration()


def test_target_loader_variables_cannot_reach_privileged_interpreter(configured):
    target = {"LD_PRELOAD": "/plugin/loader.so", "PYTHONPATH": "/plugin/site",
              "PATH": "/plugin/bin", "PLUGIN_TOKEN": "fixture-credential"}
    command, env = identity.prepare_child(["python", "plugin.py"], target)
    assert command[1:3] == ["-I", "-S"]
    assert command[-2:] == ["python", "plugin.py"]
    assert "fixture-credential" not in " ".join(command)
    assert "LD_PRELOAD" not in env and "PYTHONPATH" not in env
    assert env["PATH"] != target["PATH"]
    assert json.loads(env["OPENBOX_CHILD_ENV_PAYLOAD"]) == target


@pytest.mark.parametrize("env", [{"PATH": None}, {"BAD=KEY": "a"}, {"TOKEN": "bad\0value"}, {"TOKEN": "a" * 65536}])
def test_invalid_envelope_never_falls_back_to_direct_execution(configured, env):
    with pytest.raises(identity.IsolationError):
        identity.prepare_child(["arbitrary-plugin"], env)


@pytest.mark.parametrize("uid,gid", [(0, 1000), (1000, 0)])
def test_root_identity_or_group_is_rejected(configured, uid, gid):
    configured.pw_uid, configured.pw_gid = uid, gid
    with pytest.raises(identity.IsolationError):
        identity.prepare_child(["arbitrary-plugin"], {})


def test_configured_identity_requires_linux(configured, monkeypatch):
    monkeypatch.setattr(identity.sys, "platform", "darwin")
    with pytest.raises(identity.IsolationError):
        identity.validate_configuration()


def test_kernel_probe_failure_prevents_startup(configured, monkeypatch):
    monkeypatch.setattr(identity.subprocess, "run", lambda *a, **kw: types.SimpleNamespace(returncode=126))
    with pytest.raises(identity.IsolationError, match="probe refused"):
        identity.validate_configuration()


async def test_terminal_identity_failure_closes_without_forking(monkeypatch):
    monkeypatch.setattr(server, "SESSION_API_KEY", "fixture-key")
    monkeypatch.setenv("OPENBOX_EXECUTOR_USER", "missing-fixture-user")
    def refuse(*args, **kwargs):
        raise identity.IsolationError("identity unavailable")
    monkeypatch.setattr(server, "prepare_child", refuse)
    monkeypatch.setattr(server.pty, "openpty", lambda: pytest.fail("invalid setup opened a PTY"))
    monkeypatch.setattr(server.os, "fork", lambda: pytest.fail("invalid setup forked"))
    closed = []
    class Socket:
        async def accept(self):
            pass
        async def close(self, **kwargs):
            closed.append(kwargs)
    await server.terminal_ws(Socket(), "fixture-key")
    assert closed == [{"code": 1011, "reason": "Terminal execution identity unavailable"}]


def test_service_control_configuration_is_not_inherited(monkeypatch):
    for name in ("SESSION_API_KEY", "OPENBOX_RESOURCE_CONTROL_DB", "OPENBOX_EXECUTOR_USER", "OPENBOX_CHILD_ENV_PAYLOAD"):
        monkeypatch.setenv(name, "fixture-service-value")
    env = server._exec_env()
    assert all(name not in env for name in (
        "SESSION_API_KEY", "OPENBOX_RESOURCE_CONTROL_DB", "OPENBOX_EXECUTOR_USER", "OPENBOX_CHILD_ENV_PAYLOAD"))
