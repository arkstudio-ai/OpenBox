"""Request, media, asset, job, cron and tool producers on the spool.

Capture opens no business transaction for recording, downloads nothing,
creates no hidden asset rows and never waits on the recorder.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

import db.base as database
from db.models.cron import CronJob, CronRun
from db.models.file_asset import FileAsset
from db.models.video_job import VideoJob
from question import runtime
from session.session import create_user_message
from tests.unit.test_durable_questions import read, state  # noqa: F401
from tests.unit.test_run_fencing_api import acting_as, supersede_elsewhere
from tests.unit.trajectory_producer_support import business_statements, recording_spool  # noqa: F401
from tool.tool import ToolContext, ToolResult, define_tool
from trajectory import TraceContext


def _chunk(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(index=0, finish_reason=None, delta=SimpleNamespace(
        content=text, reasoning_content=None, tool_calls=[]))], usage=None)


def _tool_ctx(**trace) -> ToolContext:
    return ToolContext(session_id="s1", user_id="u1", workspace_id="w1", message_id="m1",
                       trace_context=TraceContext("u1", "s1", turn_id="turn", run_id="run", step_id="step", **trace))


def _no_trajectory_sql(statements) -> bool:
    return not [statement for statement in statements if "trajectory_" in statement.lower()]


async def _file_asset(asset_id: str, *, status: str = "ready", size: int = 12, key: str | None = None) -> None:
    async with database.get_db_session() as db:
        db.add(FileAsset(id=asset_id, user_id="u1", workspace_id="w1", session_id="s1", name=f"{asset_id}.mp4",
                         oss_key=key or f"assets/u1/{asset_id}/{asset_id}.mp4", mime="video/mp4", size=size,
                         status=status, created_at=runtime.now()))


async def test_request_capture_opens_no_transaction_and_marks_its_last_delta_final(recording_spool,
                                                                                 business_statements):
    from agent.trajectory import RequestCapture, litellm_chunk_blocks
    ctx = _tool_ctx()
    ctx._trajectory_media_sources = {"a" * 64: "asset_frame"}
    capture = await RequestCapture.start(ctx, purpose="chat", model_id="provider/model", capture_level="adapter_input",
                                         payload={"model": "provider/model", "api_key": "never-recorded",
                                                  "messages": [{"role": "user", "content": "hello"}]})

    async def provider():
        # The trailing "htt" could begin a URL, so the redactor withholds it
        # until the stream ends; finish() then emits it as the last delta.
        for text in ("Hello ", "visit htt"):
            yield _chunk(text)
    delivered = [chunk async for chunk in capture.stream_chunks(provider(), litellm_chunk_blocks)]
    await capture.capture_usage({"input": 3, "output": 2})
    await capture.finish("completed", reason="stop")

    assert len(delivered) == 2 and business_statements == []
    events = recording_spool.events()
    assert [item["type"] for item in events] == ["request.prepared", "request.started", "request.delta",
                                                  "request.delta", "request.usage", "request.delta",
                                                  "request.finished"]
    prepared = events[0]["data"]
    assert prepared["media_sources"] == {"a" * 64: "asset_frame"}
    assert "never-recorded" not in json.dumps(prepared) and "api_key" in prepared["input"]["omitted_fields"]
    assert [item["data"].get("final") for item in events if item["type"] == "request.delta"] == [None, None, True]
    final = events[5]
    assert final["event_id"].endswith(":redaction:finalize") and final["data"]["source"] == "recorder_redaction"
    assert final["data"]["blocks"][0]["delta"] == "htt"
    assert {item["request_id"] for item in events} == {capture.context.request_id}
    assert ctx.trace_context == capture.context


async def test_stream_delivery_never_waits_for_the_recorder(recording_spool, monkeypatch):
    import trajectory
    from agent.trajectory import RequestCapture
    never = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(trajectory, "record_stream", lambda context, event: never)
    capture = await RequestCapture.start(_tool_ctx(), purpose="chat", model_id="provider/model",
                                         payload={"model": "provider/model"}, capture_level="adapter_input")

    async def provider():
        for text in ("one", "two", "three"):
            yield _chunk(text)

    def broken_blocks(chunk):
        raise RuntimeError("unexpected provider chunk")
    delivered = await asyncio.wait_for(
        asyncio.ensure_future(_collect(capture.stream_chunks(provider(), broken_blocks))), timeout=2)
    assert len(delivered) == 3 and not never.done()


async def _collect(stream):
    return [chunk async for chunk in stream]


async def test_owned_media_are_asset_references_without_downloads_or_hidden_assets(state, recording_spool,
                                                                                  monkeypatch):
    from agent.trajectory import (capture_service_dispatch, register_owned_media_inputs,
                                  retain_derived_media_inputs, service_scope)
    monkeypatch.setattr("trajectory.artifacts.read_asset_bytes", AsyncMock(side_effect=AssertionError("downloaded")))
    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(host="bucket.oss.example"))
    await _file_asset("asset_video", key="assets/u1/asset_video/clip.mp4")
    ctx = _tool_ctx(call_id="call-1")
    video = "https://bucket.oss.example/assets/u1/asset_video/clip.mp4?Signature=never-recorded"
    frame = "https://bucket.oss.example/analysis/u1/job/frame-1.jpg?Expires=1"
    foreign = "https://bucket.oss.example/assets/u2/other/clip.mp4"
    elsewhere = "https://attacker.example/assets/u1/asset_video/clip.mp4"

    assert await register_owned_media_inputs(ctx, [video, frame, foreign, elsewhere]) == {video: "asset_video"}
    derived = await retain_derived_media_inputs(ctx, [frame, foreign], "asset_video")
    assert list(derived) == [frame]
    assert (derived[frame]["source_asset_id"], derived[frame]["oss_key"], derived[frame]["media_type"]) == (
        "asset_video", "analysis/u1/job/frame-1.jpg", "image/jpeg")
    async with database.get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(FileAsset)) == 1

    async with service_scope(ctx, asset_urls={video: "asset_video"}, retained_media=derived):
        async with capture_service_dispatch(purpose="video_generation", provider="fixture", model="model",
                                            operation="submit", profile="video_generation",
                                            body={"prompt": "clip", "content": [video, frame]}):
            pass
    [prepared] = recording_spool.events("request.prepared")
    business_body = prepared["data"]["input"]["input"]["business_body"]
    assert business_body["content"] == ["trajectory-media:asset_video",
                                        f"trajectory-media:{derived[frame]['media_id']}"]
    assert "never-recorded" not in json.dumps(prepared)
    [artifact] = recording_spool.events("artifact.recorded")
    assert artifact["data"]["role"] == "input" and artifact["data"]["asset_ref"]["oss_key"] == "assets/u1/asset_video/clip.mp4"


async def test_image_and_vision_requests_refuse_a_revoked_run_before_the_provider(state, monkeypatch):
    import openai
    from tool import image_gen, video_analyze
    monkeypatch.setattr(openai, "AsyncOpenAI", Mock(side_effect=AssertionError("provider reached")))
    monkeypatch.setattr("billing.service.UsageMeter.start", AsyncMock(side_effect=AssertionError("meter opened")))
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda _model: {})
    monkeypatch.setattr(runtime, "VERIFIED_REUSE_SECONDS", 0)
    ctx = ToolContext(session_id="s1", user_id="u1", workspace_id="w1")
    target = SimpleNamespace(api_key="key", timeout_seconds=5, base_url=None, model="image", provider="fixture")
    arguments = SimpleNamespace(prompt="a poster", n=1, output_compression=None, background=None)

    ticket = await runtime.start_run("s1", "u1")
    await supersede_elsewhere()
    with acting_as(ticket):
        with pytest.raises(runtime.RunRevoked) as image_refused:
            await image_gen._call_provider(target, arguments, size="1024x1024", quality="low",
                                           output_format="png", images=[], mask=None, ctx=ctx)
        with pytest.raises(runtime.RunRevoked) as vision_refused:
            await video_analyze._complete(ctx, model="vision", frames=[], duration=1.0, transcript="", timeout=5)
    assert image_refused.value.boundary == vision_refused.value.boundary == "request"


async def test_asset_upload_and_delete_record_references_and_revocations_without_downloads(
        state, recording_spool, business_statements, monkeypatch):
    from api import assets
    monkeypatch.setattr("trajectory.artifacts.read_asset_bytes", AsyncMock(side_effect=AssertionError("downloaded")))
    oss = SimpleNamespace(head=AsyncMock(return_value={"size": 12}), delete=AsyncMock(),
                          presign_get=lambda key, **_kwargs: f"https://oss.example/{key}")
    monkeypatch.setattr(assets, "_oss_or_503", lambda: oss)
    user = {"user_id": "u1", "workspace_id": "w1"}
    await _file_asset("asset_upload", status="pending", size=0)

    item = await assets.complete_asset("asset_upload", current_user=user, _workspace={})
    assert item["status"] == "ready"
    [artifact] = recording_spool.events("artifact.recorded")
    assert artifact["data"]["role"] == "input" and artifact["session_id"] == "s1"
    assert artifact["data"]["asset_ref"] == {"asset_id": "asset_upload", "oss_key": "assets/u1/asset_upload/asset_upload.mp4",
                                             "media_type": "video/mp4", "size_bytes": 12,
                                             "name": "asset_upload.mp4"}

    assert await assets.delete_asset("asset_upload", current_user=user, _workspace={}) == {"ok": True}
    await _file_asset("asset_huge", status="pending", size=0)
    oss.head = AsyncMock(return_value={"size": assets._MAX_SIZE + 1})
    with pytest.raises(HTTPException) as refused:
        await assets.complete_asset("asset_huge", current_user=user, _workspace={})
    assert refused.value.status_code == 413
    deletions = recording_spool.controls("asset.deleted")
    assert [(control["asset_id"], control["user_id"]) for control in deletions] == [
        ("asset_upload", "u1"), ("asset_huge", "u1")]
    assert all(control["deleted_at"].endswith("Z") for control in deletions)
    assert _no_trajectory_sql(business_statements)


async def test_job_facts_wait_for_the_job_commit_and_flag_a_late_result(state, recording_spool):
    from trajectory.jobs import CONTEXT_KEY, record_job_in_tx
    await create_user_message("s1", "Make a clip", user_id="u1")
    ticket = await runtime.start_run("s1", "u1")
    submitted = await runtime.get_run_trace(ticket)
    job_id = uuid4().hex
    async with database.get_db_session() as db:
        job = VideoJob(id=job_id, user_id="u1", session_id="s1", kind="segment", idempotency_key=job_id,
                       status="dispatching", model="fixture", attempt=0,
                       request_data={CONTEXT_KEY: submitted.to_dict(), "prompt": "clip"}, result_data={},
                       created_at=runtime.now(), updated_at=runtime.now())
        db.add(job)
        await record_job_in_tx(db, job, submitted=True)
        assert recording_spool.events("job.submitted") == []  # Not before the job row commits.
    [submitted_event] = recording_spool.events("job.submitted")
    assert submitted_event["run_id"] == ticket.run_id and submitted_event["data"]["input"] == {"prompt": "clip"}

    with pytest.raises(RuntimeError):
        async with database.get_db_session() as db:
            job = await db.get(VideoJob, job_id)
            job.status = "in_progress"
            await record_job_in_tx(db, job)
            raise RuntimeError("the job update failed")
    assert recording_spool.events("job.progress") == []

    await runtime.finish_run(ticket, completed=True)
    async with database.get_db_session() as db:
        job = await db.get(VideoJob, job_id)
        job.status = "completed"
        await record_job_in_tx(db, job)
    [finished] = recording_spool.events("job.finished")
    [late] = recording_spool.events("operation.late_result")
    assert finished["data"]["status"] == "completed"
    assert late["data"]["original_run_id"] == ticket.run_id and late["event_id"].startswith(f"job_late:{job_id}:")


async def test_cron_run_entry_takes_no_session_lock_and_its_facts_follow_the_run_row(state, recording_spool,
                                                                                   monkeypatch):
    import session.internal_parts as internal_parts
    from cron import executor
    monkeypatch.setattr(internal_parts, "lock_owned_session", AsyncMock(side_effect=AssertionError("session locked")))
    monkeypatch.setattr(internal_parts, "begin_session_write", AsyncMock(side_effect=AssertionError("session locked")))
    async with database.get_db_session() as db:
        db.add(CronJob(id="job-1", user_id="u1", workspace_id="w1", project_id="p1", session_id="s1", name="Daily",
                       schedule={"kind": "every", "every_seconds": 3600}, task_prompt="Report",
                       created_at=runtime.now(), updated_at=runtime.now()))
    job = {"id": "job-1", "user_id": "u1", "session_id": "s1", "project_id": "p1", "workspace_id": "w1",
           "name": "Daily", "task_prompt": "Report", "schedule": {"kind": "every"}}

    trace = await executor._create_run_entry("run-1", job, runtime.now())
    events = recording_spool.events()
    assert [item["type"] for item in events] == ["baseline.captured", "turn.started", "job.submitted"]
    assert events[0]["event_id"] == "evt_baseline_s1_0"
    assert events[2]["event_id"] == "cron:run-1:submitted" and {item["turn_id"] for item in events} == {"run-1"}
    assert (await read(CronRun, "run-1")).trace_context == trace.to_dict()

    await executor._update_run_entry("run-1", "job-1", None, status="ok", summary_text="All good",
                                     ended_at=runtime.now(), duration_ms=5)
    assert [item["type"] for item in recording_spool.events()][-3:] == ["job.finished", "agent.finished",
                                                                         "turn.finished"]
    assert recording_spool.events("job.finished")[0]["event_id"] == "cron:run-1:finished:ok"


class _Value(BaseModel):
    value: str


async def test_tool_calls_and_file_changes_reach_the_spool_with_their_call_identity(recording_spool,
                                                                                   business_statements):
    from agent.hooks import ToolHooks
    from trajectory.files import record_file_change

    async def execute(arguments, ctx):
        await ctx.update_output("working")
        await record_file_change(ctx, "/workspace/notes.txt", operation="edit", before="old\n",
                                 after="api_key=sk-abcdefghijklmnopqrstuvwx\n")
        return ToolResult(output="done " + arguments.value)
    tool = define_tool("example", description="Example", parameters=_Value, execute=execute, sandbox_required=False)
    hooks = ToolHooks("s1", "u1")

    async def allow(*args):
        return None
    hooks.authorize_tool = allow
    result = await hooks.wrap_execute("example", tool.execute, {"value": "x"}, _tool_ctx(), part_id="call-9",
                                      tool_info=tool)
    assert result.output.startswith("done x")

    async def custom(arguments, ctx):
        return ToolResult(output="custom result")
    await hooks.wrap_execute("custom", custom, {}, _tool_ctx(), part_id="call-10")

    events = recording_spool.events()
    assert [item["type"] for item in events] == [
        "tool.requested", "tool.started", "tool.output", "artifact.recorded", "tool.output", "tool.finished",
        "tool.requested", "tool.started", "tool.output", "tool.finished"]
    assert {item["call_id"] for item in events[:6]} == {"call-9"}
    diff = events[3]
    assert diff["data"]["artifact_type"] == "file_diff" and "sk-abcdefghijklmnopqrstuvwx" not in json.dumps(diff)
    assert events[8]["data"]["stage"] == "executor_result" and events[8]["data"]["final"] is True
    assert business_statements == []
