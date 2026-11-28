"""Whose directories the project reclaim may move, checked against a real query.

The reclaim runs unattended on one cloud desktop and moves directories into
.openbox/trash, from which a later pass removes them for good. Slugs are unique
only per (workspace, user), and the desktop belongs to one workspace, so the
comparison has to be made against that workspace's projects: another tenant
having deleted a project of the same name says nothing about this directory.

Everything here is offline: an in-memory SQLite database (tests/conftest.py)
and a fake desktop filesystem that answers the reclaim's own shell commands.
See docs/kernel-refactor/project-reaper.md.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

import project.reclaim as reclaim
from db.base import get_db_session
from db.models.project import Project
from project.workspace import TRASH_ROOT, WORKSPACE_ROOT
from sandbox.manager import SandboxInfo, SandboxManager


NOW = datetime.now(timezone.utc)
#: A deletion well past the grace period.
OLD = NOW - timedelta(days=reclaim.DELETED_GRACE_DAYS + 23)
RECENT = NOW - timedelta(days=1)
#: A project directory last changed before its project was deleted.
LEFTOVER = OLD - timedelta(days=2)


@dataclass
class _Result:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


@dataclass
class FakeDesktop:
    """A desktop's /workspace: top-level entries with their own mtime.

    Only the command shapes the reclaim sends are understood; anything else
    fails the test, so a new command cannot slip past these checks unseen.
    `mv` keeps the moved directory's mtime, as rename(2) does on a real disk.
    """

    entries: dict[str, datetime] = field(default_factory=dict)
    trash: dict[str, datetime] = field(default_factory=dict)
    unreadable: set[str] = field(default_factory=set)
    commands: list[str] = field(default_factory=list)

    async def execute(self, command, timeout=None, workdir=None):
        self.commands.append(command)
        if command == f"ls -1 {WORKSPACE_ROOT} 2>/dev/null || true":
            return _Result("\n".join([*sorted(self.entries), ".openbox"]))
        stat = re.fullmatch(
            rf"stat -c %Y -- {re.escape(WORKSPACE_ROOT)}/(\S+) 2>/dev/null \|\| true", command)
        if stat:
            name = stat.group(1)
            if name in self.entries and name not in self.unreadable:
                return _Result(f"{int(self.entries[name].timestamp())}\n")
            return _Result("")
        move = re.fullmatch(
            rf"mkdir -p {re.escape(TRASH_ROOT)} && "
            rf"mv {re.escape(WORKSPACE_ROOT)}/(\S+) {re.escape(TRASH_ROOT)}/(\S+) \|\| true",
            command,
        )
        if move:
            name, target = move.groups()
            if name in self.entries:
                self.trash[target] = self.entries.pop(name)
            return _Result()
        find = re.fullmatch(
            rf"find {re.escape(TRASH_ROOT)} -maxdepth 1 -mindepth 1 -type d "
            rf"-mtime \+(\d+) 2>/dev/null \|\| true",
            command,
        )
        if find:
            days = int(find.group(1))
            # find -mtime +N: more than N whole days since the entry's own mtime.
            stale = [f"{TRASH_ROOT}/{n}" for n, m in sorted(self.trash.items())
                     if (NOW - m).days > days]
            return _Result("\n".join(stale))
        remove = re.fullmatch(rf"rm -rf {re.escape(TRASH_ROOT)}/(\S+)", command)
        if remove:
            self.trash.pop(remove.group(1), None)
            return _Result()
        raise AssertionError(f"unexpected command on the fake desktop: {command}")

    def moved(self) -> list[str]:
        return [c for c in self.commands if " mv " in c]

    def removed(self) -> list[str]:
        return [c for c in self.commands if c.startswith("rm -rf")]


def _ids():
    suffix = uuid4().hex[:10]
    return f"ws_a_{suffix}", f"ws_b_{suffix}", f"slug-{suffix}"


async def _project(*, workspace_id: str, user_id: str, slug: str,
                   deleted_at: datetime | None = None, deleted: bool | None = None):
    is_deleted = deleted if deleted is not None else deleted_at is not None
    async with get_db_session() as db:
        db.add(Project(
            id=f"prj_{uuid4().hex[:16]}",
            user_id=user_id,
            workspace_id=workspace_id,
            name=slug,
            slug=slug,
            is_deleted=is_deleted,
            deleted_at=deleted_at,
            created_at=OLD - timedelta(days=30),
            updated_at=deleted_at or OLD,
        ))


def _bin_name(slug: str, at: datetime) -> str:
    return f"{slug}-{at.strftime('%Y%m%d-%H%M%S')}"


# ── which directories are compared with which projects ──


@pytest.mark.asyncio
async def test_another_workspaces_deleted_project_does_not_bin_this_desktops_directory():
    # The defect this file exists for. Before the fix the expired set was taken
    # across every user, and this directory was binned.
    ws_a, ws_b, slug = _ids()
    await _project(workspace_id=ws_b, user_id="user_b", slug=slug, deleted_at=OLD)
    # Workspace A's desktop has a directory of that name the database does not
    # know for A (an agent's `git clone`, a hand-made folder).
    desktop = FakeDesktop(entries={slug: LEFTOVER})

    result = await reclaim.reclaim(desktop, workspace_id=ws_a)

    assert result["binned"] == []
    assert result["unknown"] == [slug]
    assert desktop.moved() == []
    assert slug in desktop.entries


@pytest.mark.asyncio
async def test_this_workspaces_long_deleted_project_is_binned_and_stays_recoverable():
    ws_a, _ws_b, slug = _ids()
    await _project(workspace_id=ws_a, user_id="user_a", slug=slug, deleted_at=OLD)
    # Nothing was added to the project's top level for a month: its own mtime
    # is old even if files below it were edited since.
    desktop = FakeDesktop(entries={slug: LEFTOVER})

    result = await reclaim.reclaim(desktop, workspace_id=ws_a)

    assert result["binned"] == [slug]
    assert slug not in desktop.entries
    # Before the fix the same pass found the moved directory "older than 14
    # days" by its kept mtime and removed it for good.
    assert desktop.removed() == []
    assert len(desktop.trash) == 1


@pytest.mark.asyncio
async def test_a_co_members_live_project_protects_the_directory():
    # Two members of one workspace may each own the slug; they share the
    # desktop and so the directory.
    ws_a, _ws_b, slug = _ids()
    await _project(workspace_id=ws_a, user_id="user_a1", slug=slug, deleted_at=OLD)
    await _project(workspace_id=ws_a, user_id="user_a2", slug=slug)
    desktop = FakeDesktop(entries={slug: LEFTOVER})

    result = await reclaim.reclaim(desktop, workspace_id=ws_a)

    assert result["binned"] == []
    assert desktop.moved() == []


@pytest.mark.asyncio
async def test_a_live_project_anywhere_still_protects_the_name():
    # Kept from the old rule on purpose: narrowing the protected set to one
    # workspace could only add moves, and this change may only remove them.
    ws_a, ws_b, slug = _ids()
    await _project(workspace_id=ws_a, user_id="user_a", slug=slug, deleted_at=OLD)
    await _project(workspace_id=ws_b, user_id="user_b", slug=slug)
    desktop = FakeDesktop(entries={slug: LEFTOVER})

    result = await reclaim.reclaim(desktop, workspace_id=ws_a)

    assert result["binned"] == []


@pytest.mark.asyncio
async def test_a_later_deletion_keeps_its_grace_period():
    # The slug was deleted long ago, recreated, and deleted again yesterday:
    # the directory holds the second project, still inside its grace period.
    # (Two members here only because SQLite ignores the index's
    # `is_deleted = false` condition; on Postgres one user can do this.)
    ws_a, _ws_b, slug = _ids()
    await _project(workspace_id=ws_a, user_id="user_a1", slug=slug, deleted_at=OLD)
    await _project(workspace_id=ws_a, user_id="user_a2", slug=slug, deleted_at=RECENT)
    desktop = FakeDesktop(entries={slug: LEFTOVER})

    result = await reclaim.reclaim(desktop, workspace_id=ws_a)

    assert result["binned"] == []


@pytest.mark.asyncio
async def test_a_deletion_without_a_time_is_not_reclaimed():
    ws_a, _ws_b, slug = _ids()
    await _project(workspace_id=ws_a, user_id="user_a", slug=slug, deleted=True)
    desktop = FakeDesktop(entries={slug: LEFTOVER})

    result = await reclaim.reclaim(desktop, workspace_id=ws_a)

    assert result["binned"] == []


@pytest.mark.asyncio
async def test_a_directory_changed_after_the_deletion_is_left_alone():
    # Same name, but the top level changed after the project was deleted:
    # someone made or reused the directory since, so it is not the leftover.
    ws_a, _ws_b, slug = _ids()
    await _project(workspace_id=ws_a, user_id="user_a", slug=slug, deleted_at=OLD)
    desktop = FakeDesktop(entries={slug: OLD + timedelta(days=3)})

    result = await reclaim.reclaim(desktop, workspace_id=ws_a)

    assert result["binned"] == []
    assert result["unknown"] == [slug]


@pytest.mark.asyncio
async def test_a_directory_whose_age_cannot_be_read_is_left_alone():
    ws_a, _ws_b, slug = _ids()
    await _project(workspace_id=ws_a, user_id="user_a", slug=slug, deleted_at=OLD)
    desktop = FakeDesktop(entries={slug: LEFTOVER}, unreadable={slug})

    result = await reclaim.reclaim(desktop, workspace_id=ws_a)

    assert result["binned"] == []
    assert desktop.moved() == []


@pytest.mark.asyncio
async def test_without_a_workspace_nothing_is_moved_or_purged():
    ws_a, _ws_b, slug = _ids()
    await _project(workspace_id=ws_a, user_id="user_a", slug=slug, deleted_at=OLD)
    old_bin = _bin_name("gone", NOW - timedelta(days=40))
    desktop = FakeDesktop(entries={slug: LEFTOVER}, trash={old_bin: LEFTOVER})

    result = await reclaim.reclaim(desktop)

    assert result["skipped"] == "no workspace"
    assert desktop.commands == []


# ── how long binned material stays ──


@pytest.mark.asyncio
async def test_trash_is_purged_by_its_time_in_the_bin_not_its_mtime():
    ws_a, _ws_b, _slug = _ids()
    expired = _bin_name("expired", NOW - timedelta(days=reclaim.TRASH_GRACE_DAYS + 6))
    fresh = _bin_name("fresh", NOW - timedelta(days=1))
    desktop = FakeDesktop(trash={
        expired: LEFTOVER,      # binned three weeks ago: goes
        fresh: LEFTOVER,        # binned yesterday, old mtime: stays
        "no-stamp": LEFTOVER,   # not named by us: stays
        "x y-20200101-000000": LEFTOVER,  # not a slug: never reaches a shell word
    })

    result = await reclaim.reclaim(desktop, workspace_id=ws_a)

    assert result["purged"] == [f"{TRASH_ROOT}/{expired}"]
    assert set(desktop.trash) == {fresh, "no-stamp", "x y-20200101-000000"}


# ── which desktop the sweep runs against ──


def _manager_with(*owners: str, client_workspace: str | None = None) -> SandboxManager:
    manager = SandboxManager()
    for owner in owners:
        manager._project_map[owner] = SandboxInfo(
            container_id=f"ecd-{owner}", user_id=owner, host="h", port=1,
            api_key="k", project_id="default")
        manager._clients[owner] = SimpleNamespace(workspace_id=client_workspace)
    return manager


@pytest.fixture
def per_desktop_provider(monkeypatch):
    import sandbox
    monkeypatch.setattr(sandbox, "provider",
                        SimpleNamespace(owns_containers=False, routes_per_user=True))


@pytest.mark.asyncio
async def test_the_only_runtime_names_the_workspace_it_belongs_to(per_desktop_provider):
    manager = _manager_with("ws_a", client_workspace="ws_a")
    owner, client = await manager.get_only_runtime()
    assert owner == "ws_a"
    assert client is manager._clients["ws_a"]


@pytest.mark.asyncio
async def test_no_runtime_with_two_desktops(per_desktop_provider):
    assert await _manager_with("ws_a", "ws_b").get_only_runtime() is None


@pytest.mark.asyncio
async def test_no_runtime_when_the_client_names_another_workspace(per_desktop_provider):
    assert await _manager_with("ws_a", client_workspace="ws_b").get_only_runtime() is None


@pytest.mark.asyncio
async def test_no_runtime_when_the_map_disagrees_about_the_owner(per_desktop_provider):
    manager = _manager_with("ws_a")
    manager._project_map["ws_a"].user_id = "ws_b"
    assert await manager.get_only_runtime() is None


@pytest.mark.asyncio
async def test_no_runtime_on_a_desktop_shared_by_every_workspace(monkeypatch):
    import sandbox
    monkeypatch.setattr(sandbox, "provider",
                        SimpleNamespace(owns_containers=False, routes_per_user=False))
    assert await _manager_with("ws_a").get_only_runtime() is None


@pytest.mark.asyncio
async def test_a_container_per_owner_is_attributable(monkeypatch):
    import sandbox
    monkeypatch.setattr(sandbox, "provider",
                        SimpleNamespace(owns_containers=True, routes_per_user=False))
    owner, _client = await _manager_with("ws_local").get_only_runtime()
    assert owner == "ws_local"


def test_the_runtime_lookup_does_not_borrow_an_identity():
    assert "user_id" not in inspect.signature(SandboxManager.get_only_runtime).parameters


@pytest.mark.asyncio
async def test_the_sweep_hands_reclaim_the_desktops_workspace(monkeypatch):
    import sandbox
    from cron import reaper

    client = object()
    calls = []

    async def only_runtime():
        return "ws_a", client

    async def fake_reclaim(sandbox_client, *, workspace_id=None, dry_run=False):
        calls.append((sandbox_client, workspace_id))
        return {"binned": [], "purged": []}

    monkeypatch.setattr(sandbox, "sandbox_manager", SimpleNamespace(get_only_runtime=only_runtime))
    monkeypatch.setattr(reclaim, "reclaim", fake_reclaim)

    await reaper._sweep_workspace()
    assert calls == [(client, "ws_a")]


@pytest.mark.asyncio
async def test_the_sweep_skips_a_desktop_it_cannot_attribute(monkeypatch):
    import sandbox
    from cron import reaper

    calls = []

    async def no_runtime():
        return None

    async def fake_reclaim(*args, **kwargs):
        calls.append(args)
        return {}

    monkeypatch.setattr(sandbox, "sandbox_manager", SimpleNamespace(get_only_runtime=no_runtime))
    monkeypatch.setattr(reclaim, "reclaim", fake_reclaim)

    await reaper._sweep_workspace()
    assert calls == []
