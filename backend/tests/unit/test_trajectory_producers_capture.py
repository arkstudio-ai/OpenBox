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


class _Delta(BaseModel):
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list = []


class _Choice(BaseModel):
    index: int = 0
    finish_reason: str | None = None
    delta: _Delta


class _Chunk(BaseModel):
    """A provider chunk the way the SDK delivers it: a model the snapshot can dump."""
    choices: list[_Choice]
    usage: dict | None = None


def _chunk(text: str):
    return _Chunk(choices=[_Choice(delta=_Delta(content=text))])


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


async def test_request_capture_opens_no_transaction_and_records_each_chunk_verbatim(recording_spool,
                                                                                  business_statements):
    from agent.trajectory import RequestCapture, litellm_chunk_blocks
    ctx = _tool_ctx()
    ctx._trajectory_media_sources = {"a" * 64: "asset_frame"}
    capture = await RequestCapture.start(ctx, purpose="chat", model_id="provider/model", capture_level="adapter_input",
                                         payload={"model": "provider/model", "api_key": "never-recorded",
                                                  "messages": [{"role": "user", "content": "hello"}]})

    async def provider():
        for text in ("Hello ", "token sk-FIXTURE_SECRET_VALUE"):
            yield _chunk(text)
    delivered = [chunk async for chunk in capture.stream_chunks(provider(), litellm_chunk_blocks)]
    await capture.capture_usage({"input": 3, "output": 2})
    await capture.finish("completed", reason="stop")

    assert len(delivered) == 2 and business_statements == []
    events = recording_spool.events()
    assert [item["type"] for item in events] == ["request.prepared", "request.started", "request.delta",
                                                  "request.delta", "request.usage", "request.finished"]
    prepared = events[0]["data"]
    assert prepared["media_sources"] == {"a" * 64: "asset_frame"}
    assert "never-recorded" not in json.dumps(prepared) and "api_key" in prepared["input"]["omitted_fields"]
    deltas = [item["data"] for item in events if item["type"] == "request.delta"]
    assert [item["chunk_index"] for item in deltas] == [1, 2]
    assert [item["blocks"] for item in deltas] == [[{"type": "text", "block_id": "0:text", "delta": "Hello "}],
                                                   [{"type": "text", "block_id": "0:text",
                                                     "delta": "token sk-FIXTURE_SECRET_VALUE"}]]
    # Each raw chunk is the provider's chunk as delivered, its text included; nothing is masked.
    assert [item["raw"] for item in deltas] == [_chunk(text).model_dump(mode="json")
                                                for text in ("Hello ", "token sk-FIXTURE_SECRET_VALUE")]
    assert not [item for item in deltas if "raw_content_mode" in item]
    assert "REDACTED" not in json.dumps(events)
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
                                            body={"prompt": "clip", "content": [video, frame], "seed": 7,
                                                  "callback_url": "https://fixture.invalid/hook",
                                                  "api_key": "never-recorded"}):
            pass
    [prepared] = recording_spool.events("request.prepared")
    business_body = prepared["data"]["input"]["input"]["business_body"]
    assert business_body["content"] == ["trajectory-media:asset_video",
                                        f"trajectory-media:{derived[frame]['media_id']}"]
    # The body is recorded verbatim apart from its transport settings and credentials.
    assert business_body["seed"] == 7 and business_body["callback_url"] == "https://fixture.invalid/hook"
    assert prepared["data"]["input"]["input"]["omitted_fields"] == ["api_key"]
    assert "never-recorded" not in json.dumps(prepared)
    [artifact] = recording_spool.events("artifact.recorded")
    assert artifact["data"]["role"] == "input" and artifact["data"]["asset_ref"]["oss_key"] == "assets/u1/asset_video/clip.mp4"


async def test_large_file_versions_are_hashed_off_the_loop_and_too_large_pairs_get_no_diff(recording_spool,
                                                                                         monkeypatch):
    import hashlib
    from trajectory import files
    offloaded = []
    to_thread = asyncio.to_thread

    async def spy(function, *args):
        offloaded.append(function.__name__)
        return await to_thread(function, *args)
    monkeypatch.setattr(files.asyncio, "to_thread", spy)
    ctx = _tool_ctx(call_id="call-files")
    large_before = "".join(f"line {index}\n" for index in range(20_000))
    large_after = large_before.replace("line 7\n", "line seven\n", 1)
    huge = "z" * (2 * 1024 * 1024)
    await files.record_file_change(ctx, "/workspace/notes.txt", operation="edit", before="状态：草稿\n",
                                   after="状态：完成\n")
    await files.record_file_change(ctx, "/workspace/large.txt", operation="edit", before=large_before,
                                   after=large_after)
    await files.record_file_change(ctx, "/workspace/huge.txt", operation="edit", before=huge, after=huge + "\n")

    small, large, skipped = (item["data"] for item in recording_spool.events("artifact.recorded"))
    assert offloaded == ["_change", "_change"]
    assert small["diff"] == "--- /workspace/notes.txt\n+++ /workspace/notes.txt\n@@ -1 +1 @@\n-状态：草稿\n+状态：完成\n"
    assert small["after"]["sha256"] == hashlib.sha256("状态：完成\n".encode()).hexdigest()
    assert small["after"]["size_bytes"] == len("状态：完成\n".encode())
    assert "-line 7\n+line seven\n" in large["diff"] and "diff_skipped" not in large
    assert large["after"]["sha256"] == hashlib.sha256(large_after.encode()).hexdigest()
    # Both versions stay recorded; only the diff of a pair over 4 MiB is skipped.
    assert skipped["diff"] is None and skipped["diff_skipped"] == "too_large"
    assert (skipped["before"]["size_bytes"], skipped["after"]["size_bytes"]) == (len(huge), len(huge) + 1)


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


async def test_asset_uses_without_the_session_lock_record_nothing_until_a_locked_write_resumes(
        state, recording_spool, monkeypatch):
    from api import assets
    from db.models.part import Part
    from trajectory.artifacts import capture_result_asset_in_tx
    oss = SimpleNamespace(head=AsyncMock(return_value={"size": 12}),
                          presign_get=lambda key, **_kwargs: f"https://oss.example/{key}")
    monkeypatch.setattr(assets, "_oss_or_503", lambda: oss)
    prompt = await create_user_message("s1", "First", user_id="u1")
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    await create_user_message("s1", "Off", user_id="u1")

    # Recording is on again, but no write holding the session lock has resumed the period yet.
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    await _file_asset("asset_upload", status="pending", size=0)
    await _file_asset("asset_output")
    await assets.complete_asset("asset_upload", current_user={"user_id": "u1", "workspace_id": "w1"}, _workspace={})
    async with database.get_db_session() as db:
        assert await capture_result_asset_in_tx(db, _tool_ctx(), await db.get(FileAsset, "asset_output")) is None
    assert recording_spool.events("artifact.recorded") == []

    async with database.get_db_session() as db:
        db.add(Part(id="p-upload", session_id="s1", message_id=prompt.id, user_id="u1", type="file",
                    data={"id": "p-upload", "type": "file", "asset_id": "asset_upload", "path": "upload.mp4"},
                    created_at=runtime.now()))
    await create_user_message("s1", "On", user_id="u1")
    async with database.get_db_session() as db:
        await capture_result_asset_in_tx(db, _tool_ctx(), await db.get(FileAsset, "asset_output"))
    # The resume baseline captures its assets while the pause flag is still stored, so the
    # pause check stays with the writers that hold no session lock, not in capture_asset_in_tx.
    artifacts = recording_spool.events("artifact.recorded")
    assert [(item["data"]["artifact_id"], item["data"]["role"]) for item in artifacts] == [
        ("asset_upload", "baseline_input"), ("asset_output", "result")]


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


async def test_job_callbacks_record_nothing_while_the_session_recording_is_paused(state, recording_spool, monkeypatch):
    from trajectory.jobs import record_job_in_tx
    await create_user_message("s1", "First", user_id="u1")
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    await create_user_message("s1", "Off", user_id="u1")

    # Recording is on again, but no write holding the session lock has resumed the period yet.
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    job_id = uuid4().hex
    async with database.get_db_session() as db:
        job = VideoJob(id=job_id, user_id="u1", session_id="s1", kind="segment", idempotency_key=job_id,
                       status="in_progress", model="fixture", attempt=0, request_data={"prompt": "clip"},
                       result_data={}, created_at=runtime.now(), updated_at=runtime.now())
        db.add(job)
        assert await record_job_in_tx(db, job) is None
    assert recording_spool.events("job.progress") == []
    assert [event for event in recording_spool.events("baseline.captured")
            if event["event_id"] == f"job_adopt:{job_id}"] == []

    await create_user_message("s1", "On", user_id="u1")
    async with database.get_db_session() as db:
        job = await db.get(VideoJob, job_id)
        job.status = "completed"
        assert await record_job_in_tx(db, job) is not None
    [finished] = recording_spool.events("job.finished")
    assert finished["data"]["job_id"] == job_id


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


async def test_generated_images_are_recorded_as_references_to_their_assets(state, recording_spool, monkeypatch):
    from session.session import create_assistant_message
    from tool import image_gen
    monkeypatch.setattr(image_gen, "_upload_bytes", AsyncMock(return_value=12))
    prompt = await create_user_message("s1", "Draw a cat", user_id="u1")
    assistant = await create_assistant_message("s1", prompt.id, user_id="u1")
    ctx = ToolContext(session_id="s1", user_id="u1", workspace_id="w1", project_id="p1", message_id=assistant.id,
                      part_id="call-image", trace_context=TraceContext("u1", "s1", turn_id="turn",
                                                                       call_id="call-image",
                                                                       message_id=assistant.id))
    ctx._trajectory_image_request_id = "request-image"

    stored = await image_gen._store_output(ctx, SimpleNamespace(), b"\x89PNG\r\n\x1a\n0000", "png", None, "a cat",
                                           "generate", 1, 1)

    result = next(item for item in recording_spool.events("artifact.recorded") if item["data"]["role"] == "result")
    assert (result["request_id"], result["call_id"]) == ("request-image", "call-image")
    assert result["data"]["asset_ref"] == {"asset_id": stored.asset_id,
                                           "oss_key": f"assets/u1/{stored.asset_id}/{stored.name}",
                                           "media_type": "image/png", "size_bytes": 12, "name": stored.name}
    assert stored.attached


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
    assert diff["data"]["artifact_type"] == "file_diff"
    # The versions are recorded as the executor saw them, hashed as recorded.
    assert diff["data"]["after"]["text"] == "api_key=sk-abcdefghijklmnopqrstuvwx\n" and "redacted" not in diff["data"]["after"]
    assert events[2]["data"] == {"tool": "example", "output": "working", "mode": "delta", "stage": "executor_stream",
                                 "chunk_index": 0}
    # Only each call's last output is final: a registered tool's (define_tool) and a direct executor's.
    assert events[2]["data"].get("final") is None
    for result_output in (events[4], events[8]):
        assert result_output["data"]["stage"] == "executor_result" and result_output["data"]["final"] is True
    assert business_statements == []


async def test_later_uses_of_an_asset_reference_its_first_artifact_fact_without_a_second(state, recording_spool):
    from models.message import FilePart
    from session.session import create_assistant_message, save_part
    from trajectory.artifacts import artifact_event_id, capture_asset
    await _file_asset("asset_clip")
    prompt = await create_user_message("s1", "Cut the clip", user_id="u1")
    assistant = await create_assistant_message("s1", prompt.id, user_id="u1")
    for part_id in ("p-first", "p-again"):
        await save_part(FilePart(id=part_id, path="/workspace/clip.mp4", mime_type="video/mp4", asset_id="asset_clip",
                                 oss_key="assets/u1/asset_clip/asset_clip.mp4", session_id="s1",
                                 message_id=assistant.id), is_new=True, user_id="u1")
    dispatched = await capture_asset(TraceContext("u1", "s1", turn_id="later"), await read(FileAsset, "asset_clip"))

    # The worker would keep only the first fact of this id; later uses no longer send conflicting copies.
    [artifact] = recording_spool.events("artifact.recorded")
    assert artifact["event_id"] == artifact_event_id("s1", "asset_clip") and artifact["data"]["role"] == "attachment"
    parts = [item for item in recording_spool.events("part.committed") if item["data"]["part"]["type"] == "file"]
    assert [item["data"]["artifacts"]["asset_clip"] for item in parts] == [dispatched, dispatched]
    assert dispatched["event_id"] == artifact["event_id"]


async def test_an_asset_use_rolled_back_or_refused_by_the_emitter_leaves_the_fact_to_the_next_use(
        state, recording_spool, monkeypatch):
    from trajectory.artifacts import capture_asset_in_tx, capture_asset_reference
    from trajectory.emitter import Emitter
    await _file_asset("asset_clip")
    context = TraceContext("u1", "s1", turn_id="turn")
    with pytest.raises(RuntimeError):
        async with database.get_db_session() as db:
            await capture_asset_in_tx(db, context, await db.get(FileAsset, "asset_clip"), role="input")
            raise RuntimeError("the business write failed")
    assert recording_spool.events() == []

    real = Emitter.emit_bytes
    refusals = {"count": 1}

    def full_queue_once(self, *args, **kwargs):
        if refusals["count"]:
            refusals["count"] -= 1
            return False
        return real(self, *args, **kwargs)
    monkeypatch.setattr(Emitter, "emit_bytes", full_queue_once)
    use = dict(asset_id="asset_clip", oss_key="assets/u1/asset_clip/asset_clip.mp4", media_type="video/mp4",
               size_bytes=12)
    await capture_asset_reference(context, **use)
    assert recording_spool.events() == []
    await capture_asset_reference(context, **use)
    await capture_asset_reference(context, **use)
    [artifact] = recording_spool.events("artifact.recorded")
    assert artifact["data"]["asset_ref"]["asset_id"] == "asset_clip"
