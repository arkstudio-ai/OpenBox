"""In-process guest routing contracts; no kernel/UID isolation claim is made.

Actual ECD kernel/actor evidence is separate. The registry boundary here is
manufactured, while prefix dispatch, scope/attempt refusal and launcher
envelope selection use the production modules.
"""
from dataclasses import replace
import importlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.responses import JSONResponse


@pytest.fixture
def actor_protocol(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "container"))
    actor = importlib.import_module("private_actor")
    execution = importlib.import_module("execution_identity")
    binding = actor.ActorBinding(id="actor_fixture", attempt_id="attempt_fixture", scope_id="a" * 64,
        workspace_id="workspace-fixture", actor_user_id="actor-fixture", desktop_id="ecd-fixture",
        region_id="region-fixture", executor_user="fixture-executor", browser_user="fixture-browser",
        workspace_dir=Path("/var/lib/fixture/workspace"), data_dir=Path("/var/lib/fixture/data"),
        tmp_dir=Path("/var/lib/fixture/tmp"), browser_state=Path("/var/lib/fixture/control"),
        browser_home=Path("/var/lib/fixture/browser"), browser_resource_id="b" * 64,
        config_path=Path("/var/lib/fixture/actors.json"), executor_uid=2200, browser_uid=2201, identity_digest="c" * 64)
    state = SimpleNamespace(binding=binding, calls=[], forwarded=[])
    class Registry:
        def lookup(self, identity, scope_id=None, attempt_id=None):
            state.calls.append((identity, scope_id, attempt_id))
            current = state.binding
            if (identity != current.id or scope_id is not None and scope_id != current.scope_id
                    or attempt_id is not None and attempt_id != current.attempt_id):
                raise actor.PrivateActorError("Original actor unavailable")
            return current
        def resolve(self, scope_id):
            return self.lookup(state.binding.id, scope_id)
        def proof(self, selected):
            if selected != state.binding:
                raise actor.PrivateActorError("Original actor changed")
            return {**selected.public(), "isolation": {"fixture_only": True}}
    monkeypatch.setattr(actor, "registry", Registry)
    async def downstream(scope, receive, send):
        state.forwarded.append((scope["path"], scope.get("openbox.original_path"), execution.current_private()))
        await JSONResponse({"path": scope["path"]})(scope, receive, send)
    state.app = actor.PrivateActorMiddleware(downstream, get_api_key=lambda: "fixture-key")
    state.headers = {"X-API-Key": "fixture-key", actor.SCOPE_HEADER: binding.scope_id,
                     actor.ATTEMPT_HEADER: binding.attempt_id}
    state.actor, state.execution = actor, execution
    return state


@pytest.mark.parametrize("denial", ["key", "scope", "attempt", "replacement", "unsupported", "retired_browser"])
async def test_private_prefix_never_falls_back_when_original_actor_is_unavailable(actor_protocol, denial):
    w = actor_protocol
    headers = dict(w.headers)
    path = "/private-runtime/actor_fixture/execute"
    if denial == "key": headers["X-API-Key"] = "caller-key"
    if denial == "scope": headers[w.actor.SCOPE_HEADER] = "d" * 64
    if denial == "attempt": headers[w.actor.ATTEMPT_HEADER] = "different-attempt"
    if denial == "replacement": w.binding = replace(w.binding, attempt_id="different-attempt")
    if denial == "unsupported": path = "/private-runtime/actor_fixture/mcp/servers"
    # The private browser was removed; its old prefix is no longer routed.
    if denial == "retired_browser": path = "/private-runtime/actor_fixture/browser/v1/operations"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://guest") as client:
        response = await client.post(path, headers=headers, json={"command": "not dispatched"})
    assert response.status_code in {403, 409} and not w.forwarded
    assert w.execution.current_private() is None


async def test_fixed_prefix_selects_only_its_actor_and_preserves_gate_request_identity(actor_protocol):
    w = actor_protocol
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app), base_url="http://guest") as client:
        for suffix in ("/execute", "/read_file", "/upload", "/catalog/version", "/resource-control/status"):
            path = "/private-runtime/actor_fixture" + suffix
            response = await client.post(path, headers=w.headers)
            assert response.status_code == 200
            assert w.forwarded[-1] == (suffix, path, w.binding)
            assert w.execution.current_private() is None
        forwarded = len(w.forwarded)
        path = "/private-runtime/actor_fixture/browser/v1/status"
        assert (await client.get(path, headers=w.headers)).status_code == 409
        assert len(w.forwarded) == forwarded and w.execution.current_private() is None
        assert (await client.get("/alive")).status_code == 200
        assert w.forwarded[-1] == ("/alive", None, None)


def test_launcher_pins_original_config_attempt_and_mount_workdir_without_changing_global_user(actor_protocol, monkeypatch):
    w = actor_protocol
    monkeypatch.setenv("OPENBOX_EXECUTOR_USER", "fixture-shared")
    monkeypatch.setattr(w.execution, "identity", lambda name: SimpleNamespace(pw_name=name, pw_uid=2200, pw_gid=2200))
    class Registry:
        def __init__(self, _path): pass
        def lookup(self, identity, scope, attempt):
            assert (identity, scope, attempt) == (w.binding.id, w.binding.scope_id, w.binding.attempt_id)
            return w.binding
    monkeypatch.setattr(w.actor, "ActorRegistry", Registry)
    with w.execution.private_context(w.binding):
        argv, env = w.execution.prepare_child(["/bin/sh", "-c", "pwd"], {}, workdir="/workspace/project")
        assert argv[argv.index("--private-binding") + 1] == w.binding.id
        assert argv[argv.index("--private-attempt") + 1] == w.binding.attempt_id
        assert argv[argv.index("--private-digest") + 1] == w.binding.identity_digest
        assert argv[argv.index("--workdir") + 1] == "/workspace/project"
        assert env["OPENBOX_EXECUTOR_USER"] == "fixture-shared"
        assert w.execution.child_cwd("/workspace/project") is None
    assert w.execution.configured_user() == "fixture-shared"
    assert w.execution.child_cwd("/workspace/project") == "/workspace/project"


def test_registry_never_uses_a_child_mount_target_for_privileged_binding_storage(actor_protocol):
    for path in ("/workspace/actors.json", "/data/control/actors.json", "/tmp/actors.json"):
        with pytest.raises(actor_protocol.actor.PrivateActorError):
            actor_protocol.actor.ActorRegistry(path)
