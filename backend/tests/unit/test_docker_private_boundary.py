"""Ordinary Docker discovery must never adopt a private physical runtime.

Only the Docker SDK is replaced. These objects do not represent running
containers, and cleanup assertions observe fake method calls only.
"""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from sandbox.docker import DockerManager


class DockerObject:
    def __init__(self, name, identity, labels):
        self.name, self.id, self.short_id = name, identity * 64, identity * 12
        self.status = "running"
        self.image = SimpleNamespace(tags=["fixture-image"])
        self.started = self.removed = self.reloaded = 0
        self.attrs = {
            "Config": {"Labels": labels, "Env": ["SESSION_API_KEY=fixture-private-key"]},
            "State": {"Status": "running"}, "Created": "2026-10-05T00:00:00Z",
            "NetworkSettings": {"Ports": {"8000/tcp": [{"HostPort": "19001"}]}},
        }

    def reload(self):
        self.reloaded += 1

    def start(self):
        self.started += 1

    def remove(self, **_kwargs):
        self.removed += 1


@pytest.fixture
def ordinary_manager():
    ordinary = DockerObject("openbox-user", "a", {
        "openbox.dev/user-id-raw": "workspace", "openbox.dev/display-name": "ordinary",
    })
    labelled = DockerObject("openbox-adoption-collision", "b", {
        **ordinary.attrs["Config"]["Labels"], "openbox.private/runtime-id": "private-binding",
    })
    orphan = DockerObject("openbox-private-missing-sql-row", "c", ordinary.attrs["Config"]["Labels"])
    other_kind = DockerObject("openbox-new-private-kind", "d", {
        **ordinary.attrs["Config"]["Labels"], "openbox.private/future-field": "reserved",
    })
    manager = object.__new__(DockerManager)
    manager.config = SimpleNamespace(container_name_prefix="openbox-", action_server_port=8000,
                                     sandbox_image="fixture-image")
    manager._containers, manager._api_keys = {}, {}
    manager._container_owners, manager._container_projects = {}, {}
    manager._executor = ThreadPoolExecutor(max_workers=1)
    manager.docker_client = SimpleNamespace(containers=SimpleNamespace(list=lambda **_: [ordinary, labelled, orphan, other_kind]))
    try:
        yield manager, ordinary, labelled, orphan, other_kind
    finally:
        manager._executor.shutdown(wait=True)


async def test_reconcile_excludes_private_label_prefix_and_future_namespace_without_sql(ordinary_manager):
    manager, ordinary, *private = ordinary_manager
    await manager.reconcile()
    assert list(manager._containers) == [ordinary.short_id]
    assert list(manager._api_keys) == [ordinary.short_id]
    assert [row.id for row in manager.get_containers_for_user("workspace")] == [ordinary.short_id]
    for item in private:
        with pytest.raises(ValueError):
            await manager.get_container(item.short_id, user_id="workspace")
        assert not item.started and not item.removed


@pytest.mark.parametrize("index", [2, 3, 4])
async def test_named_adoption_cannot_start_or_register_private_runtime(ordinary_manager, index):
    manager, _, *_ = ordinary_manager
    private = ordinary_manager[index]
    with pytest.raises(PermissionError):
        await manager._reuse_named_container(private, display_name="ordinary", user_id="workspace", project_id=None)
    assert manager._containers == manager._api_keys == manager._container_owners == {}
    assert private.started == private.removed == 0


async def test_reconcile_removes_a_previously_misregistered_private_identity(ordinary_manager):
    manager, _, private, *_ = ordinary_manager
    for registry in (manager._containers, manager._api_keys, manager._container_owners, manager._container_projects):
        registry[private.short_id] = "old-shared-registration"
    await manager.reconcile()
    assert all(private.short_id not in registry for registry in
               (manager._containers, manager._api_keys, manager._container_owners, manager._container_projects))


async def test_ordinary_cleanup_never_removes_any_private_namespace_container(ordinary_manager):
    manager, ordinary, *private = ordinary_manager
    await manager.cleanup_all()
    assert ordinary.removed == 1
    assert all(item.removed == 0 for item in private)


@pytest.mark.parametrize("name", ["private-new-binding", "openbox-private-new-binding"])
async def test_ordinary_creation_cannot_claim_the_reserved_private_name_before_registration(ordinary_manager, name):
    manager, *objects = ordinary_manager
    with pytest.raises(PermissionError):
        await manager.create_container(name, user_id="workspace")
    assert manager._containers == manager._api_keys == {}
    assert all(item.started == item.removed == item.reloaded == 0 for item in objects)
