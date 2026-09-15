"""Recording is fail-open (SPEC §0.2): a broken or full emitter never changes a business outcome."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import BaseModel

import db.base as database
from db.models.message import Message
from db.models.part import Part
from db.models.video_job import VideoJob
from models.message import TextPart
from question import runtime
from session.session import create_assistant_message, create_user_message, save_part, update_message_info
from tests.unit.test_durable_questions import read, state  # noqa: F401
from tool.tool import ToolContext, ToolResult, define_tool
from trajectory import TraceContext, bind


@pytest.fixture(params=["broken", "full"])
def failing_emitter(request, tmp_path, monkeypatch):
    """Recording on, with an emitter that raises inside or a queue that holds no event."""
    from trajectory import producers
    from trajectory.emitter import Emitter, get_emitter, reset_emitter_for_tests
    producers.reset_for_tests()
    reset_emitter_for_tests()
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    if request.param == "broken":
        def broken(*args, **kwargs):
            raise RuntimeError("emitter broken")
        monkeypatch.setattr(Emitter, "_enqueue", broken)
        monkeypatch.setattr(Emitter, "encode_control", broken)
    else:
        monkeypatch.setenv("TRAJECTORY_EMIT_QUEUE_BYTES", "1")
    yield request.param
    emitter = get_emitter()
    if request.param == "full":
        assert emitter.stats()["dropped_by_reason"].get("queue_overflow")
    reset_emitter_for_tests()


def _ctx(**fields) -> ToolContext:
    return ToolContext(session_id="s1", user_id="u1", workspace_id="w1", message_id="m1",
                       trace_context=TraceContext("u1", "s1", turn_id="turn", run_id="run", step_id="step"), **fields)


class _Value(BaseModel):
    value: str


async def test_chat_writes_commit(state, failing_emitter):
    prompt = await create_user_message("s1", "Hello", user_id="u1")
    assistant = await create_assistant_message("s1", prompt.id, user_id="u1")
    await save_part(TextPart(id="p-answer", text="Hi", session_id="s1", message_id=assistant.id), is_new=True,
                    user_id="u1")
    assistant.finish = "stop"
    await update_message_info(assistant, user_id="u1")
    assert (await read(Message, assistant.id)).finish == "stop"
    assert (await read(Part, "p-answer")).data["text"] == "Hi"


async def test_permission_decisions_proceed(state, failing_emitter, monkeypatch):
    from permission import permission as permissions
    from permission.permission import Rule
    monkeypatch.setattr(permissions, "_pending", {})
    monkeypatch.setattr(permissions, "_approved", {})
    monkeypatch.setattr(permissions, "_get_redis_client", lambda: None)
    monkeypatch.setattr(permissions, "_push_waiting", AsyncMock())
    monkeypatch.setattr(permissions, "_push_resolved", AsyncMock())
    asked = asyncio.Event()
    monkeypatch.setattr("permission.permission.bus.publish",
                        lambda kind, _data: asked.set() if kind == permissions.PERMISSION_ASKED else None)
    with bind(TraceContext("u1", "s1", call_id="call-1")):
        await permissions.ask("s1", "bash", ["ls"], config_rules=[Rule(permission="bash", pattern="*", action="allow")],
                              user_id="u1")
        waiting = asyncio.create_task(permissions.ask("s1", "bash", ["deploy"], user_id="u1"))
    await asyncio.wait_for(asked.wait(), timeout=2)
    [request_id] = list(permissions._pending)
    await permissions.reply(request_id, "once", user_id="u1")
    await asyncio.wait_for(waiting, timeout=2)


async def test_job_updates_commit(state, failing_emitter):
    from trajectory.jobs import CONTEXT_KEY, record_job_in_tx
    job_id = uuid4().hex
    async with database.get_db_session() as db:
        job = VideoJob(id=job_id, user_id="u1", session_id="s1", kind="segment", idempotency_key=job_id,
                       status="dispatching", model="fixture", attempt=0, result_data={},
                       request_data={CONTEXT_KEY: TraceContext("u1", "s1", run_id="gone").to_dict()},
                       created_at=runtime.now(), updated_at=runtime.now())
        db.add(job)
        await record_job_in_tx(db, job, submitted=True)
    async with database.get_db_session() as db:
        job = await db.get(VideoJob, job_id)
        job.status = "completed"
        await record_job_in_tx(db, job)
    assert (await read(VideoJob, job_id)).status == "completed"


async def test_billing_and_metered_requests_settle(state, failing_emitter, monkeypatch):
    import litellm
    from agent.llm import metered_completion
    from agent.trajectory import capture_billing
    await capture_billing(TraceContext("u1", "s1", request_id="req-1"), SimpleNamespace(event_id="usage-1"),
                          {"input": 1}, "0.01", "chat")
    monkeypatch.setattr("billing.service.UsageMeter.start", AsyncMock(return_value=None))
    response = SimpleNamespace(usage=None, choices=[SimpleNamespace(index=0, finish_reason="stop",
                                                                    message=SimpleNamespace(content="A title"))])
    monkeypatch.setattr(litellm, "acompletion", AsyncMock(return_value=response))
    result = await metered_completion(ctx=_ctx(), billing_kind="title", model="test/model", messages=[])
    assert result is response


async def test_paid_dispatch_and_media_submit_reach_the_provider(state, failing_emitter, monkeypatch):
    import httpx
    from agent.trajectory import register_owned_media_inputs, retain_derived_media_inputs, service_scope
    from db.models.file_asset import FileAsset
    from tool import video_production
    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(host="bucket.oss.example"))
    job_id = uuid4().hex
    async with database.get_db_session() as db:
        db.add(VideoJob(id=job_id, user_id="u1", session_id="s1", kind="segment", idempotency_key=job_id,
                        status="submitting", model="fixture", attempt=0, request_data={}, result_data={},
                        created_at=runtime.now(), updated_at=runtime.now()))
        db.add(FileAsset(id="asset_clip", user_id="u1", workspace_id="w1", session_id="s1", name="clip.mp4",
                         oss_key="assets/u1/asset_clip/clip.mp4", mime="video/mp4", size=12, status="ready",
                         created_at=runtime.now()))
    posted = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **kwargs):
            posted.append((url, kwargs.get("json")))
            return SimpleNamespace(status_code=200, text="", json=lambda: {"id": "provider-task"})
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    target = SimpleNamespace(wire_format="ark", provider="fixture", model="video-model", api_key="key",
                             base_url="https://video.example", submit_timeout_seconds=5)
    async with service_scope(_ctx(), job=SimpleNamespace(id=job_id, request_data={})):
        data = await video_production._provider_submit(target, {"model": "video-model", "prompt": "a clip"})
    assert data == {"id": "provider-task"} and len(posted) == 1

    # A media submit: owned inputs and sampled frames become references while the provider gets the real URLs.
    clip = "https://bucket.oss.example/assets/u1/asset_clip/clip.mp4"
    frame = "https://bucket.oss.example/analysis/u1/job/frame-1.jpg"
    media_ctx = _ctx()
    asset_urls = await register_owned_media_inputs(media_ctx, [clip])
    retained = await retain_derived_media_inputs(media_ctx, [frame], "asset_clip")
    assert asset_urls == {clip: "asset_clip"} and list(retained) == [frame]
    body = {"model": "video-model", "prompt": "a clip", "content": [clip, frame]}
    async with service_scope(media_ctx, job=SimpleNamespace(id=job_id, request_data={}),
                             asset_urls=asset_urls, retained_media=retained):
        media_data = await video_production._provider_submit(target, body)
    assert media_data == {"id": "provider-task"}
    assert posted[-1][1]["content"] == [clip, frame]


async def test_bash_streams_its_output_without_falling_back(state, failing_emitter):
    from tool import bash

    class Sandbox:
        executed = False

        async def execute_stream(self, **kwargs):
            yield SimpleNamespace(content="x" * (bash.MAX_STREAM_OUTPUT + 10))
            yield SimpleNamespace(content="tail")
            yield 0

        async def execute(self, **kwargs):
            Sandbox.executed = True
            raise AssertionError("re-executed without streaming")
    ctx = _ctx(sandbox=Sandbox())
    # The call identity the hooks bind before a tool runs.
    ctx.trace_context = ctx.trace_context.derive(call_id="call-bash")
    result = await bash.execute(bash.BashArgs(command="printf data"), ctx)
    assert result.metadata["exit_code"] == 0 and not Sandbox.executed


async def test_tools_and_file_changes_complete(state, failing_emitter):
    from agent.hooks import ToolHooks
    from trajectory.files import record_file_change

    async def execute(arguments, ctx):
        await ctx.update_output("working")
        await record_file_change(ctx, "/workspace/a.txt", operation="write", before=None, after=arguments.value)
        return ToolResult(output="done")
    tool = define_tool("example", description="Example", parameters=_Value, execute=execute, sandbox_required=False)
    hooks = ToolHooks("s1", "u1")

    async def allow(*args):
        return None
    hooks.authorize_tool = allow
    result = await hooks.wrap_execute("example", tool.execute, {"value": "content"}, _ctx(), part_id="call-1",
                                      tool_info=tool)
    assert result.output == "done" and not result.metadata.get("error")
