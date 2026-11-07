"""Pooled desktops still finishing their claim: no patrol probe, no tool admission."""
import pytest

from assistant import resource_control as controls
from sandbox.wuying_desktop_service import patrol_may_probe


@pytest.mark.parametrize("record,expected", [
    ({"status": "running", "tunnel_state": "up"}, True),
    ({"status": "running", "tunnel_state": "down"}, True),
    # An install/verify attempt owns the pending -> up transition.
    ({"status": "running", "tunnel_state": "pending"}, False),
    ({"status": "running", "tunnel_state": "revoked"}, False),
    ({"status": "starting", "tunnel_state": "up"}, False),
])
def test_patrol_probes_only_settled_running_channels(record, expected):
    assert patrol_may_probe(record) is expected


@pytest.mark.parametrize("record,expected", [
    (None, None),
    ({"pool_state": "assigned"}, None),
    ({"pool_state": None}, None),
    ({"pool_state": "assigning"}, "assigning"),
    ({"pool_state": "recycling"}, "recycling"),
])
async def test_unsettled_pool_state_reports_only_blocking_states(monkeypatch, record, expected):
    from db.repository import cloud_desktop_repo as module

    async def get_by_desktop_id(desktop_id):
        assert desktop_id == "ecd-test"
        return record

    monkeypatch.setattr(module.cloud_desktop_repo, "get_by_desktop_id", get_by_desktop_id)
    assert await controls.unsettled_pool_state("ecd-test") == expected


async def test_unsettled_pool_state_ignores_desktops_without_identity():
    assert await controls.unsettled_pool_state("") is None
