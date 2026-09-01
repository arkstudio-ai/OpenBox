"""Snapshot stores follow the same tenant/project namespace as worktrees."""

import pytest

from project.workspace import ProjectLocator
from snapshot import snapshot


@pytest.mark.asyncio
async def test_snapshot_store_isolated_across_users_with_same_slug(monkeypatch):
    identities = {
        "session-alice": ("alice", "project-alice"),
        "session-bob": ("bob", "project-bob"),
    }

    async def fake_identity(session_id):
        return identities[session_id]

    async def fake_locator(project_id, user_id=None):
        return ProjectLocator(id=project_id, user_id=user_id or "default", slug="demo")

    monkeypatch.setattr("session.session.workspace_identity_for", fake_identity)
    monkeypatch.setattr(snapshot, "locator_for", fake_locator)

    alice = await snapshot._store("session-alice")
    bob = await snapshot._store("session-bob")

    assert alice.gitdir != bob.gitdir
    assert alice.workdir != bob.workdir
    assert alice.gitdir.startswith("/workspace/openbox/users/u-")
    assert alice.workdir.endswith("-demo")


@pytest.mark.asyncio
async def test_sessions_in_same_project_share_snapshot_store(monkeypatch):
    async def fake_identity(_session_id):
        return "alice", "project-1"

    async def fake_locator(project_id, user_id=None):
        return ProjectLocator(id=project_id, user_id=user_id or "default", slug="shared")

    monkeypatch.setattr("session.session.workspace_identity_for", fake_identity)
    monkeypatch.setattr(snapshot, "locator_for", fake_locator)

    first = await snapshot._store("session-1")
    second = await snapshot._store("session-2")

    assert first == second
