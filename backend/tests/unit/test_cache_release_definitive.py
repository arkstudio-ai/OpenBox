"""A desktop's 409 CACHE_TASK_FENCE_CHANGED on release is a settled answer, not uncertainty."""
import httpx
import pytest

from sandbox.runtime_operation import RuntimePreparationUncertain, definitive_rejection, run_runtime_operation
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import resource, ordinary_running  # noqa: F401
from tests.unit.test_assistant_resource_gateway import gateway  # noqa: F401
from tests.unit.test_assistant_resource_commands import remote, accept  # noqa: F401
from tests.unit.test_assistant_runtime_resource import rows, runtime  # noqa: F401


def _status_error(status, body):
    request = httpx.Request("POST", "http://fixture.invalid/cache/release")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("fixture", request=request, response=response)


@pytest.mark.parametrize("stage,exc,expected", [
    ("cache_release", _status_error(409, {"detail": "CACHE_TASK_FENCE_CHANGED"}), {"released": False, "reason": "CACHE_TASK_FENCE_CHANGED"}),
    ("cache_release", _status_error(409, {"detail": "CACHE_MOUNT_CHANGED"}), None),
    ("cache_release", _status_error(423, {"detail": "CACHE_TASK_FENCE_CHANGED"}), None),
    ("cache_release", httpx.ReadTimeout("lost"), None),
    ("cache_admit", _status_error(409, {"detail": "CACHE_TASK_FENCE_CHANGED"}), None),
    ("project_directory", _status_error(409, {"detail": "RESOURCE_JOURNAL_CHANGED"}), None),
])
def test_only_a_fence_changed_release_is_definitive(stage, exc, expected):
    assert definitive_rejection(stage, exc) == expected


async def _release_through(ctx, transport, detail):
    async def answer(request):
        await transport(request)
        if request.url.path == "/cache/release":
            return httpx.Response(409, json={"detail": detail})
        return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
    ctx.sandbox._transport = httpx.MockTransport(answer)

    async def send():
        async with ctx.sandbox._client(timeout=5) as http:
            response = await http.post("/cache/release", headers=ctx.sandbox._request_headers(), json={})
            response.raise_for_status()
            return response.json()

    return await run_runtime_operation(ctx.sandbox, session_id=ctx.session_id, user_id=ctx.user_id,
        stage="cache_release", key="release-fixture", payload={"action": "release"}, operation=send)


async def test_release_without_a_task_settles_succeeded(runtime):
    ctx, sent, transport, _ = runtime
    result = await _release_through(ctx, transport, "CACHE_TASK_FENCE_CHANGED")
    assert result == {"released": False, "reason": "CACHE_TASK_FENCE_CHANGED"}
    row, = await rows(ctx)
    assert row.operation == "cache_release" and row.state == "succeeded"
    assert row.provider_receipt["result"] == result
    assert len(sent) == 1


async def test_other_release_conflicts_stay_uncertain(runtime):
    ctx, sent, transport, _ = runtime
    with pytest.raises(RuntimePreparationUncertain):
        await _release_through(ctx, transport, "CACHE_MOUNT_CHANGED")
    row, = await rows(ctx)
    assert row.state == "outcome_unknown"
