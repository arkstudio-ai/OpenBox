"""Project instructions belong to the session sandbox, not the API host."""
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from agent.agent import AgentDef
from agent.loop import _build_system_prompt
from session.instruction import (
    clear_all_claims,
    instruction_resolve,
    instruction_system_with_config,
)
from tool.read import ReadArgs, execute as execute_read
from tool.tool import ToolContext


class FakeSandbox:
    def __init__(self, files: dict[str, str]):
        self.files = files
        self.listed: list[str] = []
        self.read: list[str] = []

    async def list_files(self, path: str) -> list[dict]:
        self.listed.append(path)
        directory = PurePosixPath(path)
        entries: dict[str, bool] = {}
        for filename in self.files:
            candidate = PurePosixPath(filename)
            try:
                relative = candidate.relative_to(directory)
            except ValueError:
                continue
            if not relative.parts:
                continue
            entries[relative.parts[0]] = len(relative.parts) > 1
        return [
            {"name": name, "is_dir": is_dir}
            for name, is_dir in sorted(entries.items())
        ]

    async def read_file_raw(self, path: str) -> str:
        self.read.append(path)
        return self.files[path]

    async def glob(self, pattern: str, path: str = "/workspace") -> list[str]:
        root = PurePosixPath(path)
        return sorted(
            filename
            for filename in self.files
            if PurePosixPath(filename).is_relative_to(root)
            and PurePosixPath(filename).relative_to(root).match(pattern)
        )


@pytest.fixture(autouse=True)
def isolated_host_config(monkeypatch, tmp_path):
    """Keep a developer's real global AGENTS/CLAUDE files out of the tests."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    clear_all_claims()
    yield
    clear_all_claims()


@pytest.mark.asyncio
async def test_live_project_instruction_comes_from_wuying_workdir_not_host_cwd(
    monkeypatch,
    tmp_path,
):
    (tmp_path / "AGENTS.md").write_text("HOST INSTRUCTION", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    sandbox = FakeSandbox({"/workspace/acme/AGENTS.md": "REMOTE INSTRUCTION"})

    result = await instruction_system_with_config(
        SimpleNamespace(instructions=[]),
        sandbox=sandbox,
        workdir="/workspace/acme",
    )

    assert result == [
        "Instructions from: /workspace/acme/AGENTS.md\nREMOTE INSTRUCTION"
    ]
    assert sandbox.listed == ["/workspace/acme"]
    assert sandbox.read == ["/workspace/acme/AGENTS.md"]
    assert all("HOST INSTRUCTION" not in item for item in result)


@pytest.mark.asyncio
async def test_config_instruction_paths_are_also_resolved_in_the_sandbox(
    monkeypatch,
    tmp_path,
):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "extra.md").write_text("HOST EXTRA", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    sandbox = FakeSandbox({
        "/workspace/acme/AGENTS.md": "PROJECT ROOT",
        "/workspace/acme/rules/extra.md": "REMOTE EXTRA",
    })

    result = await instruction_system_with_config(
        SimpleNamespace(instructions=["rules/*.md"]),
        sandbox=sandbox,
        workdir="/workspace/acme",
    )

    assert any("PROJECT ROOT" in item for item in result)
    assert any("REMOTE EXTRA" in item for item in result)
    assert all("HOST EXTRA" not in item for item in result)


@pytest.mark.asyncio
async def test_directory_instructions_are_remote_scoped_and_claimed_once():
    sandbox = FakeSandbox({
        "/workspace/acme/AGENTS.md": "PROJECT ROOT",
        "/workspace/acme/src/AGENTS.md": "SOURCE RULES",
    })

    first = await instruction_resolve(
        "/workspace/acme/src/module.py",
        "message-1",
        sandbox=sandbox,
        workdir="/workspace/acme",
    )
    second = await instruction_resolve(
        "/workspace/acme/src/other.py",
        "message-1",
        sandbox=sandbox,
        workdir="/workspace/acme",
    )

    assert [item["filepath"] for item in first] == [
        "/workspace/acme/src/AGENTS.md"
    ]
    assert "SOURCE RULES" in first[0]["content"]
    assert second == []
    assert "/workspace/acme/AGENTS.md" not in sandbox.read


@pytest.mark.asyncio
async def test_directory_instruction_cannot_escape_session_workdir():
    sandbox = FakeSandbox({"/workspace/other/AGENTS.md": "OTHER PROJECT"})

    result = await instruction_resolve(
        "/workspace/other/secrets.py",
        "message-2",
        sandbox=sandbox,
        workdir="/workspace/acme",
    )

    assert result == []
    assert sandbox.listed == []
    assert sandbox.read == []


@pytest.mark.asyncio
async def test_prompt_builder_forwards_current_sandbox_and_workdir(monkeypatch):
    sandbox = object()

    async def capture(_config, *, sandbox: object, workdir: str):
        assert sandbox is expected_sandbox
        assert workdir == "/workspace/acme"
        return ["sandbox project instruction"]

    expected_sandbox = sandbox
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(sandbox_provider="wuying"),
    )
    monkeypatch.setattr(
        "session.instruction.instruction_system_with_config",
        capture,
    )

    parts = await _build_system_prompt(
        AgentDef(name="explore", description="", prompt="agent"),
        "openai/gpt-5",
        workdir="/workspace/acme",
        sandbox=sandbox,
    )

    assert "sandbox project instruction" in parts


@pytest.mark.asyncio
async def test_read_tool_forwards_its_active_sandbox_and_workdir(monkeypatch):
    class ReadSandbox:
        async def read_file(self, **_kwargs):
            return "1 | source"

        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(exit_code=1, stdout="", stderr="")

    sandbox = ReadSandbox()
    seen = {}

    async def capture(filepath, message_id, *, sandbox, workdir):
        seen.update({
            "filepath": filepath,
            "message_id": message_id,
            "sandbox": sandbox,
            "workdir": workdir,
        })
        return []

    monkeypatch.setattr("session.instruction.instruction_resolve", capture)
    result = await execute_read(
        ReadArgs(file_path="/workspace/acme/source.py"),
        ToolContext(
            sandbox=sandbox,
            workdir="/workspace/acme",
            message_id="message-read",
        ),
    )

    assert result.output == "1 | source"
    assert seen == {
        "filepath": "/workspace/acme/source.py",
        "message_id": "message-read",
        "sandbox": sandbox,
        "workdir": "/workspace/acme",
    }


@pytest.mark.asyncio
async def test_legacy_non_run_caller_can_still_load_host_project(monkeypatch, tmp_path):
    (tmp_path / "AGENTS.md").write_text("LEGACY HOST", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await instruction_system_with_config(SimpleNamespace(instructions=[]))

    assert result == [f"Instructions from: {tmp_path}/AGENTS.md\nLEGACY HOST"]
