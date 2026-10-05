"""Browser supplier isolation; physical ready is not browser control approval."""
from dataclasses import replace

import pytest
from sqlalchemy import select

from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.private_runtime import PrivateRuntimeBinding
from sandbox import private_runtime as runtime
from tests.unit.test_private_runtime import assistant_database, private_world, resolve, validate  # noqa: F401


@pytest.fixture
async def browser_world(private_world):
    world = private_world
    world.config.private_runtime.browser_image = "openbox-browser-resource:fixture"
    world.daemon.image_ids[world.config.private_runtime.browser_image] = "sha256:" + "b" * 64
    return world


async def test_browser_and_sandbox_have_distinct_persistent_physical_authority(browser_world):
    w = browser_world
    sandbox = await resolve(w)
    browser = await resolve(w, kind="browser_profile")
    assert browser.kind == "browser_profile" and browser.resource_id and len(browser.resource_id) == 64
    assert sandbox.resource_id is None
    assert all(getattr(browser, field) != getattr(sandbox, field) for field in
               ("binding_id", "container_id", "route_key", "image_id", "api_key", "port"))
    async with get_db_session() as db:
        ordinary = await db.get(PrivateRuntimeBinding, sandbox.binding_id)
        separate = await db.get(PrivateRuntimeBinding, browser.binding_id)
        assert separate.workspace_volume is None and separate.data_volume not in {ordinary.workspace_volume, ordinary.data_volume}
        assert set(separate.volume_identities) == {"data"}
    attrs = w.daemon.container_rows[browser.container_id]
    assert [mount["Destination"] for mount in attrs["Mounts"]] == ["/data"]
    assert attrs["HostConfig"]["CapAdd"] == ["CHOWN", "KILL", "SETGID", "SETUID"]
    assert set(attrs["HostConfig"]["PortBindings"]) == {"8080/tcp"}
    assert attrs["Config"]["Env"] == ["BROWSER_RESOURCE_API_KEY=" + browser.api_key]
    command = attrs["Config"]["Cmd"]
    assert command[1] == "/opt/browser_resource/browser_resource.py"
    assert command[command.index("--resource-id") + 1] == browser.resource_id
    assert command[command.index("--chromium") + 1] == "/usr/lib/chromium/chromium"
    assert command[command.index("--isolation") + 1] == browser.isolation_mode == "chromium_sandbox"
    assert "--no-sandbox" not in command and not attrs["Config"]["Entrypoint"]
    before = [call for call in w.daemon.calls if call[0].endswith(("create", "start"))]
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    assert await resolve(w, kind="browser_profile", create=False) == browser
    assert await validate(w, browser, kind="browser_profile") == browser
    assert before == [call for call in w.daemon.calls if call[0].endswith(("create", "start"))]
    # A browser route cannot enter the default generic sandbox adapter.
    for candidate, kind in ((browser, "sandbox"), (sandbox, "browser_profile"),
                            (replace(browser, resource_id="f" * 64), "browser_profile")):
        with pytest.raises(runtime.PrivateRuntimeError):
            await validate(w, candidate, kind=kind)


@pytest.mark.parametrize("mode", ["no_image", "same_image", "read_without_binding"])
async def test_browser_requires_explicit_separate_image_and_existing_read_binding(private_world, mode):
    w = private_world
    if mode == "same_image":
        w.config.private_runtime.browser_image = w.config.private_runtime.image
    if mode == "read_without_binding":
        w.config.private_runtime.browser_image = "local-browser-image"
    with pytest.raises(runtime.PrivateRuntimeError):
        await resolve(w, kind="browser_profile", create=mode != "read_without_binding")
    assert not w.daemon.calls
    async with get_db_session() as db:
        assert not (await db.scalars(select(PrivateRuntimeBinding).where(
            PrivateRuntimeBinding.workspace_id == w.workspace))).all()


async def test_distinct_image_names_cannot_alias_the_existing_sandbox_image(browser_world):
    w = browser_world
    await resolve(w)
    w.daemon.image_ids[w.config.private_runtime.browser_image] = w.daemon.image
    before = len(w.daemon.calls)
    with pytest.raises(runtime.PrivateRuntimeError) as error:
        await resolve(w, kind="browser_profile")
    assert error.value.code == "PRIVATE_RUNTIME_IDENTITY_CHANGED"
    assert not any(operation.endswith(("create", "start")) for operation, _ in w.daemon.calls[before:])


@pytest.mark.parametrize("change", ["generic_key", "generic_command", "unsafe_browser", "extra_workspace", "extra_capability"])
async def test_browser_cannot_become_a_generic_or_unsandboxed_runtime(browser_world, change):
    w = browser_world
    route = await resolve(w, kind="browser_profile")
    attrs = w.daemon.container_rows[route.container_id]
    if change == "generic_key": attrs["Config"]["Env"].append("SESSION_API_KEY=not-a-browser-credential")
    if change == "generic_command": attrs["Config"]["Cmd"] = ["python", "/opt/action_server/action_server.py"]
    if change == "unsafe_browser": attrs["Config"]["Cmd"].append("--no-sandbox")
    if change == "extra_workspace": attrs["HostConfig"]["Binds"].append("legacy-shared:/workspace:rw")
    if change == "extra_capability": attrs["HostConfig"]["CapAdd"].append("SYS_ADMIN")
    with pytest.raises(runtime.PrivateRuntimeError) as error:
        await validate(w, route, kind="browser_profile")
    assert error.value.code == "PRIVATE_RUNTIME_IDENTITY_CHANGED"


async def test_browser_lost_create_response_reconciles_only_the_same_one_volume_attempt(browser_world):
    w = browser_world
    w.daemon.fail_after = "container.create"
    with pytest.raises(runtime.PrivateRuntimeError):
        await resolve(w, kind="browser_profile")
    route = await resolve(w, kind="browser_profile")
    assert await validate(w, route, kind="browser_profile") == route
    assert len(w.daemon.container_rows) == len(w.daemon.volume_rows) == 1
    assert sum(operation == "container.create" for operation, _ in w.daemon.calls) == 1


async def test_container_uid_mode_requires_current_explicit_opt_in_and_is_persisted(browser_world):
    w = browser_world
    w.config.private_runtime.browser_isolation = "container_uid"
    route = await resolve(w, kind="browser_profile")
    assert route.isolation_mode == "container_uid"
    command = w.daemon.container_rows[route.container_id]["Config"]["Cmd"]
    assert command[-2:] == ["--isolation", "container_uid"] and "--fixture-no-sandbox" not in command
    async with get_db_session() as db:
        assert (await db.get(PrivateRuntimeBinding, route.binding_id)).isolation_mode == "container_uid"
    assert await validate(w, route, kind="browser_profile") == route
    before = list(w.daemon.calls)
    w.config.private_runtime.browser_isolation = "chromium_sandbox"
    with pytest.raises(runtime.PrivateRuntimeError) as error:
        await validate(w, route, kind="browser_profile")
    assert error.value.code == "PRIVATE_RUNTIME_DISABLED" and w.daemon.calls == before
