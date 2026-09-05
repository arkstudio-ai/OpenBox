"""Fleet ECD wrappers use the proven INSTANCE tag namespace and safe shapes."""
import asyncio
from types import SimpleNamespace

import pytest

from core.config import OpenBoxConfig
from sandbox import wuying_ecd


def _config(**overrides):
    values = dict(
        wuying_region_id="cn-shanghai",
        wuying_env_tag="prod",
        wuying_period=1,
        wuying_period_unit="Month",
    )
    values.update(overrides)
    return OpenBoxConfig(**values)


class FakeClient:
    def __init__(self):
        self.calls = []

    async def modify_entitlement_async(self, request):
        self.calls.append(("entitlement", request))
        return SimpleNamespace(body=SimpleNamespace(request_id="req-entitlement"))

    async def rebuild_desktops_async(self, request):
        self.calls.append(("rebuild", request))
        result = SimpleNamespace(to_map=lambda: {"DesktopId": "ecd-1", "Code": "Success"})
        return SimpleNamespace(body=SimpleNamespace(
            request_id="req-rebuild", rebuild_results=[result]
        ))

    async def tag_resources_async(self, request):
        self.calls.append(("tag", request))
        return SimpleNamespace(body=SimpleNamespace(request_id="req-tag"))

    async def untag_resources_async(self, request):
        self.calls.append(("untag", request))
        return SimpleNamespace(body=SimpleNamespace(request_id="req-untag"))

    async def modify_desktop_charge_type_async(self, request):
        self.calls.append(("charge", request))
        return SimpleNamespace(body=SimpleNamespace(
            request_id="req-charge", order_id="order-1", task_id="task-1"
        ))

    async def modify_desktops_policy_group_async(self, request):
        self.calls.append(("policy", request))
        return SimpleNamespace(body=SimpleNamespace(request_id="req-policy"))


async def test_mutating_wrappers_have_exact_request_shapes(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(wuying_ecd, "get_config", lambda: _config())
    monkeypatch.setattr(wuying_ecd, "ecd_client", lambda: client)

    assert await wuying_ecd.modify_entitlement("ecd-1", ["eu-1"]) == "req-entitlement"
    rebuilt = await wuying_ecd.rebuild_desktop("ecd-1", "img-v3")
    assert rebuilt["request_id"] == "req-rebuild"
    assert await wuying_ecd.tag_desktop("ecd-1", {"b": "2", "a": "1"}) == "req-tag"
    assert await wuying_ecd.untag_desktop("ecd-1", ["old"]) == "req-untag"
    charge = await wuying_ecd.modify_charge_type("ecd-1")
    assert charge == {"request_id": "req-charge", "order_id": "order-1", "task_id": "task-1"}
    assert await wuying_ecd.modify_policy_group("ecd-1", "pg-1") == "req-policy"

    calls = dict(client.calls)
    assert calls["entitlement"].desktop_id == "ecd-1"
    assert calls["entitlement"].end_user_id == ["eu-1"]
    assert calls["rebuild"].desktop_id == ["ecd-1"]
    assert calls["rebuild"].image_id == "img-v3"
    assert calls["rebuild"].after_status == "Running"
    assert calls["tag"].resource_type == "ALIYUN::GWS::INSTANCE"
    assert [(item.key, item.value) for item in calls["tag"].tag] == [("a", "1"), ("b", "2")]
    assert calls["untag"].resource_type == "ALIYUN::GWS::INSTANCE"
    assert calls["charge"].desktop_id == ["ecd-1"]
    assert calls["charge"].charge_type == "PrePaid"
    assert calls["charge"].auto_pay is True
    assert calls["policy"].policy_group_id == "pg-1"


async def test_rebuild_rejects_per_desktop_failure(monkeypatch):
    class Client:
        async def rebuild_desktops_async(self, _request):
            result = SimpleNamespace(
                code="DesktopStatusNotSupport",
                desktop_id="ecd-1",
                message="desktop must be stopped",
                to_map=lambda: {},
            )
            return SimpleNamespace(
                body=SimpleNamespace(request_id="req-failed", rebuild_results=[result])
            )

    monkeypatch.setattr(wuying_ecd, "get_config", lambda: _config())
    monkeypatch.setattr(wuying_ecd, "ecd_client", lambda: Client())
    with pytest.raises(RuntimeError, match="DesktopStatusNotSupport.*must be stopped"):
        await wuying_ecd.rebuild_desktop("ecd-1", "img-v3")


async def test_wait_ready_requires_target_image_after_rebuild(monkeypatch):
    states = iter([
        {"status": "Running", "progress": "17%", "image_id": "img-v2"},
        {"status": "Rebuilding", "progress": "50%", "image_id": "img-v3"},
        {"status": "Running", "progress": "100%", "image_id": "img-v3"},
    ])
    calls = 0

    async def describe(_desktop_id):
        nonlocal calls
        calls += 1
        return next(states)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(wuying_ecd, "describe_desktop", describe)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    await wuying_ecd.wait_desktop_ready(
        "ecd-1", timeout_sec=30, poll_interval=1, expected_image_id="img-v3"
    )
    assert calls == 3


async def test_list_fleet_is_paginated_and_rechecks_authoritative_tags(monkeypatch):
    first = SimpleNamespace(
        desktop_id="ecd-in", desktop_name="in", desktop_status="Running",
        end_user_ids=[], charge_type="PrePaid", expired_time="2026-10-01T00:00Z",
        image_id="img-v3", desktop_type="type-1", policy_group_id="pg-1",
        system_disk_size=50,
    )
    second = SimpleNamespace(
        desktop_id="ecd-out", desktop_name="out", desktop_status="Running",
        end_user_ids=[], charge_type="PrePaid", expired_time="2026-10-01T00:00Z",
        image_id="img-v3", desktop_type="type-1", policy_group_id="pg-1",
        system_disk_size=50,
    )

    class Pages:
        def __init__(self):
            self.requests = []

        async def describe_desktops_async(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                return SimpleNamespace(body=SimpleNamespace(desktops=[first], next_token="next"))
            return SimpleNamespace(body=SimpleNamespace(desktops=[second], next_token=""))

    pages = Pages()

    async def tags(_ids):
        return {
            "ecd-in": {wuying_ecd.TAG_ENV: "prod"},
            "ecd-out": {wuying_ecd.TAG_ENV: "other"},
        }

    monkeypatch.setattr(wuying_ecd, "get_config", lambda: _config())
    monkeypatch.setattr(wuying_ecd, "ecd_client", lambda: pages)
    monkeypatch.setattr(wuying_ecd, "list_desktop_tags", tags)
    rows = await wuying_ecd.list_fleet_desktops()
    assert [row["desktop_id"] for row in rows] == ["ecd-in"]
    assert len(pages.requests) == 2
    assert pages.requests[0].tag[0].key == wuying_ecd.TAG_ENV
    assert pages.requests[1].next_token == "next"


async def test_existing_list_desktops_still_returns_workspace_filtered_rows(monkeypatch):
    desktop = SimpleNamespace(
        desktop_id="ecd-workspace", desktop_name="workspace", desktop_status="Running",
        end_user_ids=["eu-1"], charge_type="PrePaid",
        expired_time="2026-10-01T00:00Z", host_name="host-1",
        network_interface_ip="10.0.0.8",
    )

    class Client:
        def __init__(self):
            self.request = None

        async def describe_desktops_async(self, request):
            self.request = request
            return SimpleNamespace(body=SimpleNamespace(desktops=[desktop]))

    client = Client()

    async def tags(_ids):
        return {"ecd-workspace": {
            wuying_ecd.TAG_ENV: "prod",
            wuying_ecd.TAG_USER: "ws-1",
        }}

    monkeypatch.setattr(wuying_ecd, "get_config", lambda: _config())
    monkeypatch.setattr(wuying_ecd, "ecd_client", lambda: client)
    monkeypatch.setattr(wuying_ecd, "list_desktop_tags", tags)

    rows = await wuying_ecd.list_desktops(user_id="ws-1")
    assert [row["desktop_id"] for row in rows] == ["ecd-workspace"]
    assert [(tag.key, tag.value) for tag in client.request.tag] == [
        (wuying_ecd.TAG_ENV, "prod"),
        (wuying_ecd.TAG_USER, "ws-1"),
    ]
