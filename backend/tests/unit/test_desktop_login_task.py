"""The 6-hourly desktop login probe: level by local hour, skip busy/recent desktops."""
from datetime import datetime, timedelta, timezone

import pytest

from platforms.desktop import service, tasks
from platforms.errors import PlatformError


def test_level_two_only_in_the_early_morning_window():
    assert tasks.level_for(datetime(2026, 9, 8, 23, 0, tzinfo=timezone.utc)) == 2   # 07:00 Shanghai
    assert tasks.level_for(datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc)) == 1    # 14:00 Shanghai
    assert tasks.level_for(datetime(2026, 9, 9, 1, 30, tzinfo=timezone.utc)) == 1   # 09:30 Shanghai


@pytest.mark.asyncio
async def test_pass_probes_assigned_desktops_and_tolerates_busy_ones(monkeypatch):
    records = [
        {"workspace_id": "ws-a", "desktop_id": "ecd-a", "tunnel_state": "up", "pool_state": "assigned"},
        {"workspace_id": "ws-b", "desktop_id": "ecd-b", "tunnel_state": "up", "pool_state": "assigned"},
        {"workspace_id": "ws-c", "desktop_id": "ecd-c", "tunnel_state": "down", "pool_state": "assigned"},
        {"workspace_id": None, "desktop_id": "ecd-d", "tunnel_state": "up", "pool_state": "assigned"},
    ]

    class Repo:
        async def list_pool_state(self, state):
            assert state == "assigned"
            return records

    import db.repository.cloud_desktop_repo as repo_mod

    monkeypatch.setattr(repo_mod, "cloud_desktop_repo", Repo())
    calls: list[tuple[str, int]] = []

    async def fake_probe(workspace_id, *, level, lease, session_id):
        calls.append((workspace_id, level))
        if workspace_id == "ws-b":
            raise service.DesktopBusy("in use")
        return []

    async def not_recent(workspace_id, now):
        return False

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(service, "probe_workspace", fake_probe)
    monkeypatch.setattr(tasks, "_recently_probed", not_recent)
    counts = await tasks.run_desktop_login_probe(sleep=no_sleep, now=datetime(2026, 9, 8, 23, 0, tzinfo=timezone.utc))
    assert calls == [("ws-a", 2), ("ws-b", 2)]
    assert counts == {"level": 2, "probed": 1, "busy": 1, "unreachable": 0, "skipped": 2}


@pytest.mark.asyncio
async def test_pass_skips_recently_probed_and_counts_unreachable(monkeypatch):
    import db.repository.cloud_desktop_repo as repo_mod

    class Repo:
        async def list_pool_state(self, state):
            return [{"workspace_id": "ws-x", "desktop_id": "ecd-x", "tunnel_state": "up"},
                    {"workspace_id": "ws-y", "desktop_id": "ecd-y", "tunnel_state": "up"}]

    monkeypatch.setattr(repo_mod, "cloud_desktop_repo", Repo())

    async def recent(workspace_id, now):
        return workspace_id == "ws-x"

    async def fake_probe(workspace_id, **kw):
        raise service.DesktopUnavailable("tunnel down")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(tasks, "_recently_probed", recent)
    monkeypatch.setattr(service, "probe_workspace", fake_probe)
    counts = await tasks.run_desktop_login_probe(sleep=no_sleep, now=datetime(2026, 9, 8, 6, 0, tzinfo=timezone.utc))
    assert counts == {"level": 1, "probed": 0, "busy": 0, "unreachable": 1, "skipped": 1}


def test_task_is_registered():
    from cron import internal_tasks

    internal_tasks.register_builtin_tasks()
    names = {t.name for t in internal_tasks._tasks.values()}
    assert "desktop_login_probe" in names and "platform_token_keepalive" in names
