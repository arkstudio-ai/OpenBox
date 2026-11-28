"""A desktop's 507 DESKTOP_LOW_SPACE before preparation starts is a settled failure, not uncertainty."""
import httpx
import pytest

from sandbox.runtime_operation import (DesktopLowSpace, RuntimePreparationUncertain, _rejected_for_low_space,
                                       run_runtime_operation)
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import resource, ordinary_running  # noqa: F401
from tests.unit.test_assistant_resource_gateway import gateway  # noqa: F401
from tests.unit.test_assistant_resource_commands import remote, accept  # noqa: F401
from tests.unit.test_assistant_runtime_resource import rows, runtime  # noqa: F401

LOW_SPACE_MESSAGE = "云电脑磁盘空间不足。释放空间或扩容后，请点「重新生成」。"


def _status_error(path, status, body, method="POST"):
    request = httpx.Request(method, "http://fixture.invalid" + path)
    response = (httpx.Response(status, json=body, request=request) if isinstance(body, dict)
                else httpx.Response(status, content=body, request=request))
    return httpx.HTTPStatusError("fixture", request=request, response=response)


@pytest.mark.parametrize("stage,exc,expected", [
    ("project_directory", _status_error("/execute", 507, {"detail": "DESKTOP_LOW_SPACE"}), True),
    ("cache_admit", _status_error("/cache/admit", 507, {"detail": "DESKTOP_LOW_SPACE"}), True),
    ("project_directory", _status_error("/execute", 507, {"detail": "INSUFFICIENT_STORAGE"}), False),
    ("project_directory", _status_error("/execute", 500, {"detail": "DESKTOP_LOW_SPACE"}), False),
    ("project_directory", _status_error("/execute", 507, b"<html>Insufficient Storage</html>"), False),
    ("project_directory", _status_error("/cache/admit", 507, {"detail": "DESKTOP_LOW_SPACE"}), False),
    ("cache_admit", _status_error("/cache/admit", 507, {"detail": "DESKTOP_LOW_SPACE"}, method="GET"), False),
    # Asset claims and fetches answer the same code after earlier cache work.
    ("cache_asset_claim", _status_error("/cache/asset-claim", 507, {"detail": "DESKTOP_LOW_SPACE"}), False),
    ("cache_asset_fetch", _status_error("/cache/asset-fetch", 507, {"detail": "DESKTOP_LOW_SPACE"}), False),
    ("project_directory", httpx.ReadTimeout("lost"), False),
])
def test_only_an_upfront_low_space_refusal_counts(stage, exc, expected):
    assert bool(_rejected_for_low_space(stage, exc)) is expected


def _answer_with(ctx, transport, path, status, detail):
    async def answer(request):
        await transport(request)
        if request.url.path == path:
            return httpx.Response(status, json={"detail": detail})
        return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
    ctx.sandbox._transport = httpx.MockTransport(answer)


async def _cache_command(ctx, action):
    async def send():
        async with ctx.sandbox._client(timeout=5) as http:
            response = await http.post("/cache/" + action, headers=ctx.sandbox._request_headers(), json={})
            response.raise_for_status()
            return response.json()

    return await run_runtime_operation(ctx.sandbox, session_id=ctx.session_id, user_id=ctx.user_id,
        stage="cache_" + action.replace("-", "_"), key=f"{action}-fixture", payload={"action": action},
        operation=send)


async def test_low_space_project_directory_settles_failed_with_the_user_message(runtime):
    ctx, sent, transport, manager = runtime
    _answer_with(ctx, transport, "/execute", 507, "DESKTOP_LOW_SPACE")
    with pytest.raises(DesktopLowSpace) as raised:
        await manager._ensure_session_dir(ctx.sandbox, ctx.session_id)
    assert raised.value.code == "DESKTOP_LOW_SPACE" and raised.value.user_message == LOW_SPACE_MESSAGE
    row, = await rows(ctx)
    assert row.operation == "project_directory" and row.state == "failed"
    assert row.provider_receipt == {"code": "DESKTOP_LOW_SPACE"}
    assert row.last_error == {"code": "DESKTOP_LOW_SPACE"}
    assert len(sent) == 1
    # The same run reads the settled answer back instead of an uncertain send.
    with pytest.raises(DesktopLowSpace):
        await manager._ensure_session_dir(ctx.sandbox, ctx.session_id)
    assert len(sent) == 1 and (await rows(ctx))[0].id == row.id


async def test_low_space_cache_admission_settles_failed(runtime):
    ctx, sent, transport, _ = runtime
    _answer_with(ctx, transport, "/cache/admit", 507, "DESKTOP_LOW_SPACE")
    with pytest.raises(DesktopLowSpace) as raised:
        await _cache_command(ctx, "admit")
    assert raised.value.user_message == LOW_SPACE_MESSAGE
    row, = await rows(ctx)
    assert row.operation == "cache_admit" and row.state == "failed"
    assert row.provider_receipt == {"code": "DESKTOP_LOW_SPACE"}
    assert len(sent) == 1


async def test_another_insufficient_storage_answer_stays_uncertain(runtime):
    ctx, _, transport, manager = runtime
    _answer_with(ctx, transport, "/execute", 507, "INSUFFICIENT_STORAGE")
    with pytest.raises(RuntimePreparationUncertain):
        await manager._ensure_session_dir(ctx.sandbox, ctx.session_id)
    row, = await rows(ctx)
    assert row.state == "outcome_unknown" and row.provider_receipt != {"code": "DESKTOP_LOW_SPACE"}


async def test_low_space_on_another_cache_step_stays_uncertain(runtime):
    ctx, _, transport, _ = runtime
    _answer_with(ctx, transport, "/cache/asset-claim", 507, "DESKTOP_LOW_SPACE")
    with pytest.raises(RuntimePreparationUncertain):
        await _cache_command(ctx, "asset-claim")
    row, = await rows(ctx)
    assert row.operation == "cache_asset_claim" and row.state == "outcome_unknown"
