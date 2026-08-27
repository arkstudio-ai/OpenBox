"""The creator is a run-scoped, validated package writer rather than raw file access."""
from pathlib import Path
import uuid

import pytest
from db.repository.user_repo import PgUserRepo
from tool.skill_manage import SkillManageArgs, SkillResource, _validation_error
from tool.skill_manage import execute
from tool.tool import ToolContext
from skill.skill import _scan_directory


def package(name: str = "greeting-helper") -> str:
    return f"""---
name: {name}
description: Give the user's approved greeting when they ask to be greeted.
---

# Greeting Helper

When the user asks for a greeting, answer with the configured greeting.
"""


def test_creator_accepts_a_complete_small_package():
    args = SkillManageArgs(
        action="create",
        name="greeting-helper",
        skill_md=package(),
        files=[SkillResource(path="references/examples.md", content="# Examples\n")],
    )
    assert _validation_error(args) is None


def test_creator_rejects_an_unsafe_or_mismatched_name():
    unsafe = SkillManageArgs(action="create", name="../escape", skill_md=package("../escape"))
    mismatch = SkillManageArgs(action="create", name="one", skill_md=package("two"))
    assert "lowercase" in (_validation_error(unsafe) or "")
    assert "exactly match" in (_validation_error(mismatch) or "")


def test_creator_skill_activates_only_the_management_tool():
    root = Path(__file__).parents[2] / ".openbox" / "skills"
    skills = {item.name: item for item in _scan_directory(root, "project")}
    creator = skills["skill-creator"]
    assert creator.allowed_tools == ("skill_manage",)
    assert "natural conversation" in creator.description


@pytest.mark.asyncio
async def test_creator_tool_registers_the_private_snapshot():
    suffix = uuid.uuid4().hex[:8]
    user_id = f"skill_manage_user_{suffix}"
    name = f"greeting-helper-{suffix}"
    await PgUserRepo().create(id=user_id, username=f"creator-{suffix}", password_hash="unused")

    class Sandbox:
        async def create_skill(self, *, name, skill_md, files):
            return {
                "name": name,
                "install_dir": name,
                "description": "Give the approved greeting.",
                "files": [item["path"] for item in files],
            }

        async def download_skill_archive(self, name):
            return b"PK\x03\x04creator-snapshot"

    result = await execute(
        SkillManageArgs(action="create", name=name, skill_md=package(name), files=[]),
        ToolContext(user_id=user_id, sandbox=Sandbox()),
    )
    assert result.metadata["name"] == name
    assert result.metadata["publication_status"] == "unpublished"
    assert "Mine → Personal" in result.output


@pytest.mark.asyncio
async def test_creator_rolls_back_new_directory_when_snapshot_fails(monkeypatch):
    name = "rollback-helper"

    class Sandbox:
        removed: list[str] = []

        async def create_skill(self, *, name, skill_md, files):
            return {
                "name": name,
                "install_dir": name,
                "description": "Temporary package",
                "files": [],
            }

        async def download_skill_archive(self, name):
            return b"PK\x03\x04temporary"

        async def uninstall_skill(self, name):
            self.removed.append(name)

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("skill.user_library.upsert_personal_snapshot", unavailable)
    sandbox = Sandbox()
    result = await execute(
        SkillManageArgs(action="create", name=name, skill_md=package(name), files=[]),
        ToolContext(user_id="creator", sandbox=sandbox),
    )

    assert sandbox.removed == [name]
    assert "rolled back" in result.output
    assert "can be retried" in result.output
