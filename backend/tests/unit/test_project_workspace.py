"""Projects: the naming rules that become directory names, and the fallbacks.

A slug is written into a shell command as a path segment, so the validation
here is the only thing between a project name and an arbitrary path.
"""
from types import SimpleNamespace

import pytest

from project.workspace import (
    DEFAULT_SLUG,
    ProjectLocator,
    ProjectError,
    asset_sandbox_path,
    ensure_directory,
    namespaced_project_directory,
    project_directory,
    slugify,
    validate_slug,
    workdir_for_session,
)
from session.session import Session, plan_path


# ── slugify ──

@pytest.mark.parametrize("name,expected", [
    ("My Project", "my-project"),
    ("  Padded  ", "padded"),
    ("UPPER", "upper"),
    ("with_underscores", "with-underscores"),
    ("multiple   spaces", "multiple-spaces"),
    ("dots.are.fine", "dots.are.fine"),
    ("trailing---", "trailing"),
    ("a//b\\c", "abc"),
])
def test_slugify_produces_a_directory_name(name, expected):
    assert slugify(name) == expected


def test_slugify_drops_characters_that_cannot_be_a_path_segment():
    assert "/" not in slugify("a/b")
    assert ".." not in slugify("../../etc")
    assert " " not in slugify("a b")


def test_a_name_with_no_ascii_slugifies_to_nothing():
    # Callers fall back to a generated slug rather than writing an empty path.
    assert slugify("中文项目") == ""
    assert slugify("🎉") == ""


def test_slugify_is_length_bounded():
    assert len(slugify("x" * 500)) <= 64


# ── validate_slug ──

def test_a_normal_slug_passes():
    assert validate_slug("my-project") == "my-project"
    assert validate_slug("proj.2") == "proj.2"


@pytest.mark.parametrize("slug", ["", ".", "..", ".openbox", "sessions", "skills", "trash"])
def test_reserved_names_are_refused(slug):
    with pytest.raises(ProjectError):
        validate_slug(slug)


@pytest.mark.parametrize("slug", [
    "../escape",       # path traversal
    "/absolute",
    "with space",
    "UPPER",           # slugify lowercases; a raw slug must already be lower
    "-leading",        # must start alphanumeric
    ".hidden",
    "semi;colon",
    "x" * 65,          # too long for a comfortable path segment
])
def test_unsafe_slugs_are_refused(slug):
    with pytest.raises(ProjectError):
        validate_slug(slug)


# ── directory ──

def test_directory_is_under_the_workspace_root():
    assert project_directory("demo") == "/workspace/demo"


def test_the_default_project_has_a_directory_like_any_other():
    assert project_directory(DEFAULT_SLUG) == "/workspace/default"


def test_same_slug_is_isolated_across_users_and_untrusted_ids_are_not_exposed():
    alice = namespaced_project_directory("alice@example.com", "project/1", "demo")
    bob = namespaced_project_directory("bob@example.com", "project/1", "demo")

    assert alice != bob
    assert alice.startswith("/workspace/openbox/users/u-")
    assert "alice@example.com" not in alice
    assert "project/1" not in alice
    assert alice.endswith("-demo")


@pytest.mark.asyncio
async def test_sessions_in_one_project_share_exactly_one_workdir(monkeypatch):
    locator = ProjectLocator(id="project-1", user_id="alice", slug="shared")

    async def fake_locator(project_id, user_id=None):
        assert project_id == "project-1"
        assert user_id == "alice"
        return locator

    monkeypatch.setattr("project.workspace.locator_for", fake_locator)
    first = SimpleNamespace(user_id="alice", project_id="project-1", id="session-1")
    second = SimpleNamespace(user_id="alice", project_id="project-1", id="session-2")

    assert await workdir_for_session(first) == await workdir_for_session(second)


def test_attachment_paths_are_isolated_by_user_project_and_asset():
    first = asset_sandbox_path("alice", "project-1", "report.pdf", asset_id="asset-1")
    other_user = asset_sandbox_path("bob", "project-1", "report.pdf", asset_id="asset-1")
    other_project = asset_sandbox_path("alice", "project-2", "report.pdf", asset_id="asset-1")
    other_asset = asset_sandbox_path("alice", "project-1", "report.pdf", asset_id="asset-2")

    assert len({first, other_user, other_project, other_asset}) == 4
    assert first.endswith("/report.pdf")
    assert all(raw not in first for raw in ("alice", "project-1", "asset-1"))


def test_session_owner_is_internal_but_drives_namespaced_plan_path():
    session = Session(
        id="session-1",
        user_id="alice",
        project_id="project-1",
        slug="quiet-fox",
        created_at="2026-08-31T00:00:00+00:00",
    )

    path = plan_path(session, "demo")

    assert path.startswith(namespaced_project_directory("alice", "project-1", "demo"))
    assert path.endswith("-quiet-fox.md")
    assert "user_id" not in session.model_dump()


@pytest.mark.asyncio
async def test_ensure_directory_uses_workspace_cwd_and_checks_exit_status():
    calls = []

    class Sandbox:
        async def execute(self, command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    expected = namespaced_project_directory("alice", "project-1", "demo")
    actual = await ensure_directory(
        Sandbox(),
        "demo",
        user_id="alice",
        project_id="project-1",
    )

    assert actual == expected
    assert calls == [
        (
            f"mkdir -p -- {expected}",
            {"timeout": 30, "workdir": "/workspace"},
        )
    ]


@pytest.mark.asyncio
async def test_ensure_directory_fails_closed_when_runner_cannot_create_it():
    class Sandbox:
        async def execute(self, _command, **_kwargs):
            return SimpleNamespace(
                exit_code=1,
                stdout="",
                stderr="Permission denied",
            )

    with pytest.raises(RuntimeError, match="Permission denied"):
        await ensure_directory(
            Sandbox(),
            "demo",
            user_id="alice",
            project_id="project-1",
        )
