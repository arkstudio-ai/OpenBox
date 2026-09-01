"""Filesystem-capable tools must share one non-bypassable permission boundary."""

import importlib.util
import json
from pathlib import Path
import sys
import types

import httpx
import pytest
from fastapi import HTTPException

import permission.permission as permission_mod
from agent.hooks import ToolHooks
from agent.loop import _get_permission_rules
from permission.permission import Rule
from sandbox.client import (
    PathResolveTarget,
    ResolvedPath,
    SandboxClient,
)
from tool.apply_patch import ApplyPatchArgs, execute as execute_patch, parse_patch
from tool.batch import batch_tool
from tool.glob_tool import GlobArgs, execute as execute_glob
from tool.grep import GrepArgs, execute as execute_grep, grep_tool
from tool.tool import ToolContext, ToolResult


ACTION_SERVER = Path(__file__).resolve().parents[3] / "container" / "action_server.py"
sys.modules.setdefault("psutil", types.SimpleNamespace())
if "sse_starlette.sse" not in sys.modules:
    sse_package = types.ModuleType("sse_starlette")
    sse_module = types.ModuleType("sse_starlette.sse")
    sse_module.EventSourceResponse = type("EventSourceResponse", (), {})
    sys.modules["sse_starlette"] = sse_package
    sys.modules["sse_starlette.sse"] = sse_module
_SPEC = importlib.util.spec_from_file_location(
    "openbox_filesystem_guard_action_server_test", ACTION_SERVER
)
assert _SPEC and _SPEC.loader
action_server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(action_server)


@pytest.fixture(autouse=True)
def isolated_permission_state(monkeypatch):
    permission_mod._approved.clear()
    permission_mod._loaded_users.clear()
    permission_mod._pending.clear()
    monkeypatch.setattr(permission_mod, "_get_redis_client", lambda: None)
    yield
    permission_mod._approved.clear()
    permission_mod._loaded_users.clear()
    permission_mod._pending.clear()


@pytest.fixture
def workspace_server(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(action_server, "WORKSPACE_ROOT", workspace)
    return action_server, workspace, tmp_path


class ActionServerResolver:
    """Small in-process adapter for canonical preflight integration tests."""

    def __init__(self, server):
        self.server = server
        self.calls = []

    async def resolve_paths(self, targets):
        self.calls.append(list(targets))
        response = await self.server.resolve_paths(
            self.server.ResolvePathsRequest(
                targets=[
                    self.server.ResolvePathTargetRequest(
                        path=target.path,
                        allow_missing=target.allow_missing,
                        allow_scoped_skills=target.allow_scoped_skills,
                    )
                    for target in targets
                ]
            )
        )
        return [ResolvedPath(**item) for item in response["targets"]]


@pytest.mark.asyncio
async def test_action_server_advertises_canonical_resolver_v10():
    health = await action_server.alive()

    assert health["version"] == "2026.08.31-run-lease-receipt-v12"
    assert "terminal_project_cwd_v1" in health["capabilities"]
    assert "run_lease_receipt_v2" in health["capabilities"]
    assert "confined_path_resolve_v1" in health["capabilities"]


def test_patch_parser_is_shared_and_returns_every_target_once():
    quote_path = "notes/it's-safe.txt"
    operations = parse_patch(
        "\n".join(
            [
                "*** Begin Patch",
                "*** Add File: safe.txt",
                "+safe",
                "*** Update File: later.txt",
                "-old",
                "+new",
                f"*** Delete File: {quote_path}",
                "*** End Patch",
            ]
        )
    )

    assert [(operation.type, operation.path) for operation in operations] == [
        ("add", "safe.txt"),
        ("update", "later.txt"),
        ("delete", quote_path),
    ]


def test_runner_directory_chowns_every_new_parent(monkeypatch, tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    target = existing / "tenant" / "projects" / "project"
    chowned = []
    monkeypatch.setattr(
        action_server,
        "_chown_runner_path",
        lambda path: chowned.append(Path(path)),
    )

    action_server._ensure_runner_directory(target)

    assert target.is_dir()
    assert chowned == [
        existing / "tenant",
        existing / "tenant" / "projects",
        target,
    ]


@pytest.mark.asyncio
async def test_apply_patch_preflights_a_later_denied_target_before_execution():
    hooks = ToolHooks(
        "session-a",
        config_rules=[Rule(permission="*", pattern="*", action="allow")],
        agent_rules=[{"permission": "edit", "pattern": "*", "action": "allow"}],
        guard_rules=[
            Rule(permission="edit", pattern="protected/**", action="deny")
        ],
    )
    patch = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: safe.txt",
            "+safe",
            "*** Delete File: protected/key.txt",
            "*** End Patch",
        ]
    )

    blocked = await hooks.authorize_tool("apply_patch", {"patch": patch})

    assert blocked is not None
    assert blocked.metadata["blocked"] is True


@pytest.mark.asyncio
async def test_static_symlink_canonical_target_is_denied_before_read_execution(
    workspace_server,
):
    server, workspace, tmp_path = workspace_server
    protected = workspace / "protected"
    protected.mkdir()
    secret = protected / "token.txt"
    secret.write_text("secret", encoding="utf-8")
    alias = workspace / "apparently-safe.txt"
    alias.symlink_to(secret)
    resolver = ActionServerResolver(server)
    hooks = ToolHooks(
        "session-a",
        workdir=str(workspace),
        config_rules=[
            Rule(permission="*", pattern="*", action="allow"),
            Rule(permission="read", pattern="protected/**", action="deny"),
        ],
    )
    executed = []

    async def execute_fn(_args, _ctx):
        executed.append(True)
        return ToolResult(title="unexpected")

    result = await hooks.wrap_execute(
        "read",
        execute_fn,
        {"file_path": str(alias)},
        ToolContext(sandbox=resolver, workdir=str(workspace)),
    )

    assert result.metadata["blocked"] is True
    assert executed == []
    assert resolver.calls[0] == [
        PathResolveTarget(
            path=str(alias),
            allow_missing=False,
            allow_scoped_skills=True,
        )
    ]

    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    escaping_alias = workspace / "escaping.txt"
    escaping_alias.symlink_to(outside)
    with pytest.raises(HTTPException) as error:
        await server.resolve_paths(
            server.ResolvePathsRequest(
                targets=[server.ResolvePathTargetRequest(path=str(escaping_alias))]
            )
        )
    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", ["write", "edit", "multiedit"])
async def test_static_symlink_canonical_target_denies_direct_mutators(
    workspace_server,
    tool_id,
):
    server, workspace, _ = workspace_server
    protected = workspace / "protected"
    protected.mkdir()
    target = protected / "target.txt"
    target.write_text("original", encoding="utf-8")
    alias = workspace / "safe-name.txt"
    alias.symlink_to(target)
    resolver = ActionServerResolver(server)
    hooks = ToolHooks(
        "session-a",
        workdir=str(workspace),
        config_rules=[
            Rule(permission="*", pattern="*", action="allow"),
            Rule(permission="edit", pattern="protected/**", action="deny"),
        ],
    )

    blocked = await hooks.authorize_tool(
        tool_id,
        {"file_path": str(alias)},
        ctx=ToolContext(sandbox=resolver, workdir=str(workspace)),
    )

    assert blocked is not None and blocked.metadata["blocked"] is True
    assert resolver.calls[0][0].allow_missing is (tool_id == "write")


@pytest.mark.asyncio
async def test_permission_preflight_rebases_relative_path_to_runtime_workdir(
    workspace_server,
):
    server, workspace, _ = workspace_server
    target = workspace / "中文目录" / "你好.txt"
    target.parent.mkdir()
    target.write_text("你好", encoding="utf-8")
    resolver = ActionServerResolver(server)
    hooks = ToolHooks(
        "session-a",
        workdir=str(workspace),
        config_rules=[Rule(permission="*", pattern="*", action="allow")],
    )

    blocked = await hooks.authorize_tool(
        "read",
        {"file_path": "中文目录/你好.txt"},
        ctx=ToolContext(sandbox=resolver, workdir=str(workspace)),
    )

    assert blocked is None
    assert resolver.calls[0] == [
        PathResolveTarget(
            path=str(target),
            allow_missing=False,
            allow_scoped_skills=True,
        )
    ]


@pytest.mark.asyncio
async def test_static_symlink_canonical_target_denies_glob_root(workspace_server):
    server, workspace, _ = workspace_server
    protected = workspace / "protected"
    protected.mkdir()
    alias = workspace / "safe-search"
    alias.symlink_to(protected, target_is_directory=True)
    resolver = ActionServerResolver(server)
    hooks = ToolHooks(
        "session-a",
        workdir=str(workspace),
        config_rules=[
            Rule(permission="*", pattern="*", action="allow"),
            Rule(permission="read", pattern="protected/**", action="deny"),
        ],
    )

    blocked = await hooks.authorize_tool(
        "glob",
        {"pattern": "**/*", "path": str(alias)},
        ctx=ToolContext(sandbox=resolver, workdir=str(workspace)),
    )

    assert blocked is not None and blocked.metadata["blocked"] is True


@pytest.mark.asyncio
async def test_patch_preflights_every_canonical_target_including_missing_adds(
    workspace_server,
):
    server, workspace, _ = workspace_server
    safe = workspace / "safe"
    protected = workspace / "protected"
    safe.mkdir()
    protected.mkdir()
    alias_parent = workspace / "innocent"
    alias_parent.symlink_to(protected, target_is_directory=True)
    resolver = ActionServerResolver(server)
    hooks = ToolHooks(
        "session-a",
        workdir=str(workspace),
        config_rules=[
            Rule(permission="*", pattern="*", action="allow"),
            Rule(permission="edit", pattern="protected/**", action="deny"),
        ],
    )
    patch = "\n".join([
        "*** Begin Patch",
        f"*** Add File: {safe / 'new.txt'}",
        "+safe",
        f"*** Add File: {alias_parent / 'hidden.txt'}",
        "+blocked",
        "*** End Patch",
    ])

    blocked = await hooks.authorize_tool(
        "apply_patch",
        {"patch": patch},
        ctx=ToolContext(sandbox=resolver, workdir=str(workspace)),
    )

    assert blocked is not None and blocked.metadata["blocked"] is True
    assert len(resolver.calls) == 1
    assert [target.path for target in resolver.calls[0]] == [
        str(safe / "new.txt"),
        str(alias_parent / "hidden.txt"),
    ]
    assert all(target.allow_missing for target in resolver.calls[0])


@pytest.mark.asyncio
async def test_batch_nested_search_inherits_canonical_target_preflight(
    workspace_server,
    monkeypatch,
):
    import tool.registry as registry

    monkeypatch.setattr(
        registry, "get_tool", lambda tool_id: grep_tool if tool_id == "grep" else None
    )
    server, workspace, _ = workspace_server
    protected = workspace / "protected"
    protected.mkdir()
    alias = workspace / "search-here"
    alias.symlink_to(protected, target_is_directory=True)
    resolver = ActionServerResolver(server)
    hooks = ToolHooks(
        "session-a",
        workdir=str(workspace),
        config_rules=[
            Rule(permission="*", pattern="*", action="allow"),
            Rule(permission="read", pattern="protected/**", action="deny"),
        ],
    )
    ctx = ToolContext(
        sandbox=resolver,
        workdir=str(workspace),
        available_tools=frozenset({"grep"}),
        _authorize_tool=hooks.authorize_tool,
    )

    result = await hooks.wrap_execute(
        "batch",
        batch_tool.execute,
        {
            "invocations": [
                {
                    "tool": "grep",
                    "parameters": {"pattern": ".", "path": str(alias)},
                }
            ]
        },
        ctx,
    )

    assert "Permission denied" in result.output
    assert len(resolver.calls) == 1


@pytest.mark.asyncio
async def test_apply_patch_delete_uses_confined_api_and_never_shell():
    calls = []

    class Sandbox:
        async def delete_file(self, path):
            calls.append(("delete_file", path))

        async def execute(self, _command):
            raise AssertionError("patch deletion must not invoke a shell")

    quoted = "notes/a'; touch escaped; #.txt"
    result = await execute_patch(
        ApplyPatchArgs(
            patch=f"*** Begin Patch\n*** Delete File: {quoted}\n*** End Patch"
        ),
        ToolContext(sandbox=Sandbox()),
    )

    assert calls == [("delete_file", f"/workspace/{quoted}")]
    assert result.output == f"Deleted {quoted}"


@pytest.mark.asyncio
async def test_search_requires_both_original_tool_and_read_policy():
    original_policy = ToolHooks(
        "session-a",
        config_rules=[
            Rule(permission="*", pattern="*", action="allow"),
            Rule(permission="grep", pattern="TOKEN", action="deny"),
        ],
    )
    read_policy = ToolHooks(
        "session-b",
        config_rules=[
            Rule(permission="*", pattern="*", action="allow"),
            Rule(permission="read", pattern="private/**", action="deny"),
        ],
    )

    denied_by_tool = await original_policy.authorize_tool(
        "grep", {"pattern": "TOKEN", "path": "src"}
    )
    denied_by_read = await read_policy.authorize_tool(
        "grep", {"pattern": ".", "path": "private"}
    )

    assert denied_by_tool is not None and denied_by_tool.metadata["blocked"]
    assert denied_by_read is not None and denied_by_read.metadata["blocked"]


def test_search_authorization_uses_the_runtime_workdir():
    hooks = ToolHooks("session-a", workdir="/workspace/projects/p-1")

    assert hooks._extract_patterns(
        "glob", {"pattern": "**/.env", "path": "/workspace"}
    ) == ["/workspace/projects/p-1/**/.env"]
    assert hooks._extract_patterns(
        "grep", {"pattern": "TODO", "path": "/workspace"}
    )[:2] == ["/workspace/projects/p-1", "/workspace/projects/p-1/**"]


@pytest.mark.parametrize(
    ("tool_id", "args", "canonical"),
    [
        ("read", {"file_path": ".ENV"}, ".env"),
        ("read", {"file_path": "/workspace/project/.SSH"}, "/workspace/project/.ssh"),
        ("read", {"file_path": ".SSH/id_ed25519"}, ".ssh/id_ed25519"),
        (
            "read",
            {"file_path": "Config/Credentials.json"},
            "config/credentials.json",
        ),
        ("bash", {"command": "cat backend/.ENV"}, "cat backend/.env"),
        ("bash", {"command": "cat .SSH/id_ed25519"}, "cat .ssh/id_ed25519"),
        (
            "bash",
            {"command": "cat Config/Credentials.json"},
            "cat config/credentials.json",
        ),
    ],
)
def test_direct_read_and_bash_add_casefolded_sensitive_subjects(
    tool_id, args, canonical
):
    from types import SimpleNamespace

    rules = _get_permission_rules(SimpleNamespace(permission={}))
    hooks = ToolHooks("session-a", config_rules=rules)
    checks = hooks._permission_checks(tool_id, args)
    permission_name, subjects = checks[-1]

    assert canonical in subjects
    assert any(
        permission_mod.evaluate(permission_name, subject, rules).action == "ask"
        for subject in subjects
    )


@pytest.mark.asyncio
async def test_tools_only_opt_into_sensitive_results_for_explicit_targets():
    calls = []

    class Sandbox:
        async def grep(self, **kwargs):
            calls.append(("grep", kwargs))
            return ""

        async def glob(self, **kwargs):
            calls.append(("glob", kwargs))
            return []

    ctx = ToolContext(sandbox=Sandbox(), workdir="/workspace/project")
    await execute_grep(GrepArgs(pattern=".", path="/workspace"), ctx)
    await execute_grep(GrepArgs(pattern=".", path="/workspace/project/.env"), ctx)
    await execute_glob(GlobArgs(pattern="**/*", path="/workspace"), ctx)
    await execute_glob(GlobArgs(pattern="**/.env*", path="/workspace"), ctx)

    assert [call[1]["include_sensitive"] for call in calls] == [
        False,
        True,
        False,
        True,
    ]


@pytest.mark.asyncio
async def test_action_server_broad_searches_exclude_sensitive_descendants(
    workspace_server,
):
    server, workspace, _ = workspace_server
    project = workspace / "project"
    (project / ".SSH").mkdir(parents=True)
    (project / ".ENV.d").mkdir(parents=True)
    (project / "normal.txt").write_text("SAFE_LINE\n", encoding="utf-8")
    (project / ".ENV").write_text("ENV_SECRET\n", encoding="utf-8")
    (project / ".SSH" / "id_ed25519").write_text("SSH_SECRET\n", encoding="utf-8")
    (project / ".ENV.d" / "nested.txt").write_text(
        "NESTED_ENV_SECRET\n", encoding="utf-8"
    )
    (project / "Service-Credentials.json").write_text(
        "CREDENTIAL_SECRET\n", encoding="utf-8"
    )

    grep_result = await server.grep_files(
        server.GrepRequest(pattern=".", path=str(project))
    )
    glob_result = await server.glob_files(
        server.GlobRequest(pattern="**/*", path=str(project))
    )

    assert "SAFE_LINE" in grep_result["output"]
    assert "ENV_SECRET" not in grep_result["output"]
    assert "SSH_SECRET" not in grep_result["output"]
    assert "NESTED_ENV_SECRET" not in grep_result["output"]
    assert "CREDENTIAL_SECRET" not in grep_result["output"]
    assert str(project / "normal.txt") in glob_result["files"]
    assert str(project / ".ENV") not in glob_result["files"]
    assert str(project / ".SSH" / "id_ed25519") not in glob_result["files"]
    assert str(project / ".ENV.d" / "nested.txt") not in glob_result["files"]
    assert str(project / "Service-Credentials.json") not in glob_result["files"]

    explicit = await server.grep_files(
        server.GrepRequest(
            pattern=".",
            path=str(project / ".ENV"),
            include_sensitive=True,
        )
    )
    assert "ENV_SECRET" in explicit["output"]

    # A raw path that merely contains a sensitive-looking component must not
    # turn into a blanket opt-in after canonical resolution.
    normalized_broad = await server.grep_files(
        server.GrepRequest(
            pattern=".",
            path=str(project / ".ENV.d" / ".."),
            include_sensitive=True,
        )
    )
    assert "SAFE_LINE" in normalized_broad["output"]
    assert "ENV_SECRET" not in normalized_broad["output"]
    assert "SSH_SECRET" not in normalized_broad["output"]


@pytest.mark.asyncio
async def test_action_server_delete_treats_quotes_literally_and_rejects_escapes(
    workspace_server,
):
    server, workspace, tmp_path = workspace_server
    quoted = workspace / "a'; touch injected; #.txt"
    quoted.write_text("delete me", encoding="utf-8")
    injected = workspace / "injected"

    await server.delete_file(server.DeleteFileRequest(path=str(quoted)))

    assert not quoted.exists()
    assert not injected.exists()

    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    for escaped in (str(outside), "../outside.txt"):
        with pytest.raises(HTTPException) as error:
            await server.delete_file(server.DeleteFileRequest(path=escaped))
        assert error.value.status_code == 403
    assert outside.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_sandbox_client_fails_closed_against_unfiltered_old_search_server(
    monkeypatch,
):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/alive":
            return httpx.Response(200, json={"capabilities": []})
        raise AssertionError("unsafe search request reached an old Action Server")

    transport = httpx.MockTransport(handler)
    client = SandboxClient("sandbox", 8000, "test-key")
    monkeypatch.setattr(
        client,
        "_client",
        lambda timeout=30.0: httpx.AsyncClient(
            transport=transport, base_url="http://sandbox:8000", timeout=timeout
        ),
    )

    with pytest.raises(RuntimeError, match="safe filesystem search filtering"):
        await client.grep(".", "/workspace")

    assert requests == ["/alive"]


@pytest.mark.asyncio
async def test_sandbox_client_fails_closed_against_server_without_path_resolver(
    monkeypatch,
):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/alive":
            return httpx.Response(200, json={"capabilities": []})
        raise AssertionError("canonical target request reached an old Action Server")

    transport = httpx.MockTransport(handler)
    client = SandboxClient("sandbox", 8000, "test-key")
    monkeypatch.setattr(
        client,
        "_client",
        lambda timeout=30.0: httpx.AsyncClient(
            transport=transport, base_url="http://sandbox:8000", timeout=timeout
        ),
    )

    with pytest.raises(RuntimeError, match="canonical path resolution"):
        await client.resolve_paths([PathResolveTarget("/workspace/file.txt")])

    assert requests == ["/alive"]


@pytest.mark.asyncio
async def test_sandbox_client_uses_capable_confined_path_resolver(monkeypatch):
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/alive":
            return httpx.Response(
                200, json={"capabilities": ["confined_path_resolve_v1"]}
            )
        if request.url.path == "/resolve_paths":
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={
                "targets": [{
                    "canonical_path": "/workspace/protected/new.txt",
                    "workspace_relative": "protected/new.txt",
                }]
            })
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)
    client = SandboxClient("sandbox", 8000, "test-key")
    monkeypatch.setattr(
        client,
        "_client",
        lambda timeout=30.0: httpx.AsyncClient(
            transport=transport, base_url="http://sandbox:8000", timeout=timeout
        ),
    )

    resolved = await client.resolve_paths([
        PathResolveTarget("innocent/new.txt", allow_missing=True)
    ])

    assert resolved == [
        ResolvedPath(
            canonical_path="/workspace/protected/new.txt",
            workspace_relative="protected/new.txt",
        )
    ]
    assert payloads == [{
        "targets": [{
            "path": "innocent/new.txt",
            "allow_missing": True,
            "allow_scoped_skills": False,
        }]
    }]


@pytest.mark.asyncio
async def test_sandbox_client_sends_sensitive_opt_in_only_to_capable_server(
    monkeypatch,
):
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/alive":
            return httpx.Response(
                200, json={"capabilities": ["sensitive_search_filter_v1"]}
            )
        if request.url.path == "/grep":
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json={"output": "secret"})
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)
    client = SandboxClient("sandbox", 8000, "test-key")
    monkeypatch.setattr(
        client,
        "_client",
        lambda timeout=30.0: httpx.AsyncClient(
            transport=transport, base_url="http://sandbox:8000", timeout=timeout
        ),
    )

    output = await client.grep(
        ".", "/workspace/.env", include_sensitive=True
    )

    assert output == "secret"
    assert payloads == [
        {
            "pattern": ".",
            "path": "/workspace/.env",
            "type": None,
            "max_results": 100,
            "include_sensitive": True,
        }
    ]
