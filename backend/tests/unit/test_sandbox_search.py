"""Search tools must work with both current and legacy Action Servers."""

import asyncio
import json
import shlex

import httpx
import pytest

from sandbox.client import SandboxClient
from tool.glob_tool import GlobArgs, execute as glob_execute
from tool.grep import GrepArgs, execute as grep_execute
from tool.tool import ToolContext


@pytest.fixture
def search_client(monkeypatch):
    def make(*, modern=False):
        requests = []

        async def handle(request):
            requests.append(request)
            if request.url.path == "/openapi.json":
                properties = {"include_sensitive": {"type": "boolean"}} if modern else {}
                return httpx.Response(200, json={"components": {"schemas": {
                    name: {"properties": properties} for name in ("GlobRequest", "GrepRequest")
                }}})
            body = json.loads(request.content)
            if request.url.path == "/execute":
                # Run the actual compatibility command against isolated files;
                # only HTTP transport is replaced, never SandboxClient methods.
                process = await asyncio.create_subprocess_exec(
                    *shlex.split(body["command"]),
                    cwd=body["workdir"],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
                return httpx.Response(200, json={
                    "exit_code": process.returncode,
                    "stdout": stdout.decode(), "stderr": stderr.decode(),
                })
            assert modern, "Legacy search endpoints would silently ignore the sensitive-file policy"
            if request.url.path == "/grep":
                return httpx.Response(200, json={"output": "result", "exit_code": 0})
            if request.url.path == "/glob":
                return httpx.Response(200, json={"files": ["result"]})
            raise AssertionError(request.url.path)

        client = SandboxClient("sandbox", 8000, "test-key")
        transport = httpx.MockTransport(handle)
        monkeypatch.setattr(client, "_client", lambda timeout=30: httpx.AsyncClient(
            base_url="http://sandbox:8000", transport=transport, timeout=timeout,
        ))
        return client, requests

    return make


@pytest.fixture
def search_tree(tmp_path):
    for name in ("notes.txt", ".ENV.local", "service-Credentials.json", ".SSH/id_test"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("search-marker\n", encoding="utf-8")
    (tmp_path / "linked-secret.txt").symlink_to(tmp_path / ".ENV.local")
    return tmp_path


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grep", "glob"])
async def test_search_tools_use_real_client_without_keyword_errors(search_client, search_tree, operation):
    client, requests = search_client()
    ctx = ToolContext(sandbox=client, workdir=str(search_tree))
    if operation == "grep":
        result = await grep_execute(GrepArgs(pattern="search-marker", path="."), ctx)
    else:
        result = await glob_execute(GlobArgs(pattern="**/*", path="."), ctx)
    assert "notes.txt" in result.output
    for hidden in (".ENV", "Credentials", ".SSH", "linked-secret"):
        assert hidden not in result.output
    assert [r.url.path for r in requests] == ["/openapi.json", "/execute"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grep", "glob"])
@pytest.mark.parametrize("include_sensitive", [False, True])
async def test_modern_search_forwards_policy_and_reuses_capability_probe(
    search_client, operation, include_sensitive,
):
    client, requests = search_client(modern=True)
    await client.grep("word", "/workspace", file_type="md", max_results=7,
                      include_sensitive=include_sensitive)
    await client.glob("*.md", "/workspace", include_sensitive=include_sensitive)
    assert sum(r.url.path == "/openapi.json" for r in requests) == 1
    body = json.loads(next(r.content for r in requests if r.url.path == "/" + operation))
    assert body["include_sensitive"] is include_sensitive
    if operation == "grep":
        assert body["type"] == "md" and body["max_results"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("include_sensitive", [False, True])
async def test_legacy_sensitive_results_require_explicit_target_and_opt_in(
    search_client, search_tree, include_sensitive,
):
    client, _ = search_client()
    output = await client.grep("search-marker", str(search_tree / ".ENV.local"),
                               include_sensitive=include_sensitive)
    files = await client.glob(".ENV*", str(search_tree), include_sensitive=include_sensitive)
    assert bool(output) is include_sensitive
    assert bool(files) is include_sensitive
    broad = await client.grep("search-marker", str(search_tree), include_sensitive=include_sensitive)
    assert ".ENV" not in broad and "Credentials" not in broad and ".SSH" not in broad
    files = await client.glob("**/*", str(search_tree), include_sensitive=include_sensitive)
    assert files == [str(search_tree / "notes.txt")]


@pytest.mark.asyncio
async def test_legacy_single_file_type_no_matches_and_literal_shell_characters(search_client, tmp_path):
    client, _ = search_client()
    target = tmp_path / "a ' quoted.md"
    target.write_text("ordinary\n$(never-run-me); `also-not-a-command`\n", encoding="utf-8")
    assert "ordinary" in await client.grep("ordinary", str(target))
    assert await client.grep("absent", str(target)) == ""
    assert await client.grep("ordinary", str(tmp_path), file_type="py") == ""
    assert "ordinary" in await client.grep("ordinary", str(tmp_path), file_type="md")
    output = await client.grep("$(never-run-me); `also-not-a-command`", str(target))
    assert "never-run-me" in output


@pytest.mark.asyncio
async def test_legacy_invalid_regex_reports_failure_not_no_matches(search_client, tmp_path):
    client, _ = search_client()
    (tmp_path / "notes.txt").write_text("hello\n")
    with pytest.raises(RuntimeError, match="grep"):
        await client.grep("[", str(tmp_path))
