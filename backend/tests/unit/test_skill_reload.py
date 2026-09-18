"""Skills are read from disk on a long-lived server.

The cache used to be filled once and never invalidated, so adding or editing a
skill did nothing until a restart — and the stale description kept going out to
the model on every request.
"""
import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest

import skill.skill as sk


def write_skill(base: Path, name: str, description: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nbody")
    return d


@pytest.fixture
def skills_dir(monkeypatch):
    """A throwaway project with a .openbox/skills tree, cache reset around it."""
    root = Path(tempfile.mkdtemp())
    base = root / ".openbox" / "skills"
    base.mkdir(parents=True)
    cwd = os.getcwd()
    os.chdir(root)
    monkeypatch.setattr(sk, "_skills", {})
    monkeypatch.setattr(sk, "_loaded", False)
    monkeypatch.setattr(sk, "_fingerprint", ())
    monkeypatch.setattr(sk, "_last_check", 0.0)
    # The throttle exists to bound I/O, not to be exercised here.
    monkeypatch.setattr(sk, "_CHECK_INTERVAL_SECONDS", 0.0)
    yield base
    os.chdir(cwd)
    shutil.rmtree(root, ignore_errors=True)


async def project_skills():
    """Return only the skills written into this test's project tree."""
    return list(await sk.list_skills())


async def names():
    return sorted(s.name for s in await project_skills())


@pytest.mark.asyncio
async def test_an_edited_description_is_picked_up(skills_dir):
    write_skill(skills_dir, "demo", "first")
    assert [s.description for s in await project_skills()] == ["first"]

    write_skill(skills_dir, "demo", "second")
    assert [s.description for s in await project_skills()] == ["second"]


@pytest.mark.asyncio
async def test_a_new_skill_appears(skills_dir):
    write_skill(skills_dir, "demo", "d")
    assert await names() == ["demo"]

    write_skill(skills_dir, "later", "d")
    assert await names() == ["demo", "later"]


@pytest.mark.asyncio
async def test_a_skill_nested_below_the_root_appears(skills_dir):
    # Only the nested directory's mtime changes, not the root's.
    write_skill(skills_dir, "demo", "d")
    await sk.list_skills()
    write_skill(skills_dir / "group", "deep", "d")
    assert "deep" in await names()


@pytest.mark.asyncio
async def test_a_removed_skill_disappears(skills_dir):
    write_skill(skills_dir, "demo", "d")
    write_skill(skills_dir, "gone", "d")
    assert await names() == ["demo", "gone"]

    shutil.rmtree(skills_dir / "gone")
    assert await names() == ["demo"]


@pytest.mark.asyncio
async def test_get_skill_sees_changes_too(skills_dir):
    write_skill(skills_dir, "demo", "first")
    assert (await sk.get_skill("demo")).description == "first"
    write_skill(skills_dir, "demo", "second")
    assert (await sk.get_skill("demo")).description == "second"


@pytest.mark.parametrize("description", ["second", "other"])
async def test_reload_detects_edits_with_preserved_mtime(skills_dir, description):
    skill_md = write_skill(skills_dir, "demo", "first") / "SKILL.md"
    assert (await sk.get_skill("demo")).description == "first"
    before = skill_md.stat()

    write_skill(skills_dir, "demo", description)
    os.utime(skill_md, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert (await sk.get_skill("demo")).description == description
    assert [s.description for s in await project_skills()] == [description]


async def test_edit_during_scan_is_not_cached_as_the_new_revision(skills_dir, monkeypatch):
    write_skill(skills_dir, "demo", "first")
    original = sk._scan_directory
    changed = False

    def scan_then_edit(base, source):
        nonlocal changed
        skills = original(base, source)
        if base == skills_dir and not changed:
            changed = True
            write_skill(skills_dir, "demo", "second")
        return skills

    monkeypatch.setattr(sk, "_scan_directory", scan_then_edit)
    assert (await sk.get_skill("demo")).description == "first"
    assert (await sk.get_skill("demo")).description == "second"


@pytest.mark.asyncio
async def test_skill_tool_declarations_are_parsed_without_loading_a_schema(skills_dir):
    path = write_skill(skills_dir, "demo", "d")
    (path / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\nallowed-tools: [image_gen, image_gen]\n---\nbody"
    )
    skill = await sk.get_skill("demo")
    assert skill.allowed_tools == ("image_gen",)


@pytest.mark.asyncio
async def test_an_unchanged_tree_is_not_rescanned(skills_dir, monkeypatch):
    write_skill(skills_dir, "demo", "d")
    await sk.list_skills()

    calls = []
    real = sk.load_skills

    async def counting():
        calls.append(1)
        await real()

    monkeypatch.setattr(sk, "load_skills", counting)
    for _ in range(5):
        await sk.list_skills()
    assert not calls, "a quiet directory must not trigger a reload"


@pytest.mark.asyncio
async def test_the_throttle_bounds_how_often_disk_is_touched(skills_dir, monkeypatch):
    write_skill(skills_dir, "demo", "d")
    await sk.list_skills()

    monkeypatch.setattr(sk, "_CHECK_INTERVAL_SECONDS", 3600.0)
    monkeypatch.setattr(sk, "_last_check", sk.time.monotonic())
    probes = []
    monkeypatch.setattr(sk, "_current_fingerprint",
                        lambda: probes.append(1) or ())
    for _ in range(10):
        await sk.list_skills()
    assert not probes, "within the interval, disk should not be stat-ed at all"
