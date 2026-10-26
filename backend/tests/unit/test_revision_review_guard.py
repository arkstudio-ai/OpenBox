"""Revision review checks bind real SQL provenance; a model cannot pick its baseline."""
from contextlib import asynccontextmanager
from dataclasses import replace
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from assistant import review_guard as guard
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.question import QuestionCheckpoint
from memory.providers.common import MemoryProviderError
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_revisions import setup
from tests.unit.test_assistant_sessions_v2 import quiet_runtime, tool_call  # noqa: F401
from tool.question_tool import QuestionArgs, execute
from tool.tool import ToolContext


ARGS = QuestionArgs.model_validate({"questions": [{"purpose": "review",
    "review_content": "0–5秒：白色大字‘今天开门啦’；5–10秒：白色大字‘来挑选吧’。",
    "question": "这版脚本可以吗？", "options": [{"label": "还要调整"}]}]})
IMAGE_URI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aFqkAAAAASUVORK5CYII="


@asynccontextmanager
async def revision():
    from agent.driver import reserve_run
    from agent.inbox import claim_inbox_boundary
    from session.session import create_assistant_message
    owner, _, workspace, _, session, main_ctx, lease, args = await setup()
    execution = None
    try:
        receipt, _ = await tool_call(main_ctx, "requests.revise", args)
        assert receipt["state"] == "accepted"
        execution = await reserve_run(session.id, owner)
        batch = await claim_inbox_boundary(execution, step=1, include_next_turn=True)
        message = await create_assistant_message(session.id, batch.messages[0].id,
            model_id="test/model", agent="build", user_id=owner,
            run_fence=(session.id, execution.run_id, execution.generation))
        ctx = ToolContext(session_id=session.id, user_id=owner, workspace_id=workspace,
            project_id=session.project_id, run_id=execution.run_id, run_generation=execution.generation,
            message_id=message.id, _assert_current=execution.assert_current,
            _image_input_uris={aid: IMAGE_URI for aid in args["attachment_ids"]})
        yield ctx, args, receipt
    finally:
        if execution:
            await execution.release(session_status="idle")
        await lease.release(session_status="idle")


class Adapter:
    def __init__(self, value=None, error=None, on_call=None):
        self.value = value or {"allowed": True, "violations": []}
        self.error, self.on_call, self.calls = error, on_call, []

    async def generate_data(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if self.on_call:
            await self.on_call()
        return self.value, {"duration_ms": 2}


async def test_only_current_revision_provenance_reaches_checker(monkeypatch):
    adapter = Adapter()
    monkeypatch.setattr(guard, "ConfiguredWikiModel", lambda _: adapter)
    async with revision() as (ctx, args, receipt):
        assert await guard.guard_review(ARGS, ctx) is None
        [call] = adapter.calls
        from core.config import get_config
        assert call["model"] == get_config().assistant.revision_review_model
        assert call["data"]["accepted_changes"] == args["instructions"]
        assert call["data"]["previous_questions"][0]["question"] == "确认30秒讲稿？"
        assert call["data"]["proposed_questions"][0]["review_content"] == ARGS.questions[0].review_content
        assert set(call["data"]) == {"accepted_changes", "human_requests", "previous_questions", "proposed_questions", "current_assets", "current_video_options"}
        assert call["data"]["human_requests"] == ("讲稿可以，但改用这张水果店照片作为背景重新做。",)
        assert [row["id"] for row in call["data"]["current_assets"]] == args["attachment_ids"]
        assert call["images"] == (IMAGE_URI,)
        assert call["data"]["current_assets"][0]["image_number"] == 1
        assert IMAGE_URI not in str(call["data"])
        assert "oss_key" not in str(call["data"]["current_assets"])
        message = SimpleNamespace(parts=[{"origin_ref": {"revision_request": {"request_id": args["request_id"]}}}])
        fragment = await guard.context_fragment(message, ctx)
        assert args["instructions"] in fragment and "确认30秒讲稿？" in fragment
        assert "Do not search the desktop" in fragment
        # A completed older generation cannot become the baseline of a new run.
        assert await guard.bound_revision(replace(ctx, run_id="another-run")) is None
        assert len(adapter.calls) == 1


async def test_image_not_in_current_request_cannot_use_an_old_description(monkeypatch):
    adapter = Adapter()
    monkeypatch.setattr(guard, "ConfiguredWikiModel", lambda _: adapter)
    async with revision() as (ctx, _, _):
        ctx._image_input_uris = {"unrelated-asset": IMAGE_URI}
        result = await guard.guard_review(ARGS, ctx)
        assert result.metadata["error"] == "REVISION_REVIEW_UNAVAILABLE"
        assert not adapter.calls


@pytest.mark.parametrize("fault", ["foreign_card", "foreign_workspace", "wrong_receipt", "wrong_command", "no_binding", "deleted_asset", "foreign_asset", "changed_human_source"])
async def test_bad_server_binding_never_sends_content_to_model(monkeypatch, fault):
    from db.models.assistant import AssistantCommand
    adapter = Adapter()
    monkeypatch.setattr(guard, "ConfiguredWikiModel", lambda _: adapter)
    async with revision() as (ctx, args, receipt):
        async with get_db_session() as db:
            row = await db.get(QuestionCheckpoint, args["request_id"])
            command = await db.get(AssistantCommand, receipt["command_id"])
            if fault == "foreign_card":
                row.session_id = command.assistant_session_id
            elif fault == "foreign_workspace":
                ctx = replace(ctx, workspace_id="not-this-workspace")
            elif fault == "wrong_receipt":
                command.receipt = {**command.receipt, "inbox_id": "unrelated"}
            elif fault == "wrong_command":
                command.action = "task_input"
            elif fault == "changed_human_source":
                from db.models.part import Part
                part = await db.get(Part, command.source_ref["source_refs"][0]["part_id"])
                part.data = {**part.data, "text": "changed after authorization"}
            elif fault in {"deleted_asset", "foreign_asset"}:
                from db.models.file_asset import FileAsset
                asset = await db.get(FileAsset, args["attachment_ids"][0])
                if fault == "deleted_asset":
                    asset.is_deleted = True
                else:
                    from db.models.user import User
                    asset.user_id = await db.scalar(select(User.id).where(User.id != ctx.user_id).limit(1))
            else:
                row.continuation = {"kind": "question"}
        result = await guard.guard_review(ARGS, ctx)
        assert result.metadata["error"] == "REVISION_REVIEW_UNAVAILABLE"
        assert not adapter.calls


@pytest.mark.parametrize("failure", ["scope", "timeout", "provider", "invalid"])
async def test_failure_prevents_checkpoint_and_notification(monkeypatch, failure):
    adapter = Adapter(value={"allowed": False, "violations": ["字幕时间未获授权却被延长。"]})
    if failure == "timeout": adapter.error = TimeoutError()
    if failure == "provider": adapter.error = MemoryProviderError("unavailable")
    if failure == "invalid": adapter.value = {"allowed": "true", "violations": []}
    monkeypatch.setattr(guard, "ConfiguredWikiModel", lambda _: adapter)
    async def never_ask(**_):
        pytest.fail("A failed review must not reach ask(), create a card or notify a user")
    async with revision() as (ctx, _, _):
        monkeypatch.setattr("question.question.ask", never_ask)
        async with get_db_session() as db:
            before = await db.scalar(select(func.count()).select_from(QuestionCheckpoint))
        result = await execute(ARGS, ctx)
        assert result.metadata["error"] == (
            "REVISION_REVIEW_OUT_OF_SCOPE" if failure == "scope" else "REVISION_REVIEW_UNAVAILABLE")
        if failure == "scope":
            assert "确认30秒讲稿？" in result.output and "<current_revision_baseline>" in result.output
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(QuestionCheckpoint)) == before


async def test_evidence_changed_during_check_is_not_accepted(monkeypatch):
    async with revision() as (ctx, args, _):
        async def change():
            async with get_db_session() as db:
                row = await db.get(QuestionCheckpoint, args["request_id"])
                row.questions = [{**row.questions[0], "question": "Different baseline"}]
        adapter = Adapter(on_call=change)
        monkeypatch.setattr(guard, "ConfiguredWikiModel", lambda _: adapter)
        result = await guard.guard_review(ARGS, ctx)
        assert result.metadata["error"] == "REVISION_REVIEW_UNAVAILABLE"
        assert len(adapter.calls) == 1


async def test_asset_description_is_bound_and_rechecked_after_model_io(monkeypatch):
    from db.models.file_asset import FileAsset
    async with revision() as (ctx, args, _):
        async with get_db_session() as db:
            asset = await db.get(FileAsset, args["attachment_ids"][0])
            asset.description = "门店木质货架、橙子和香蕉，图片中没有人物。"
            asset.description_state = "ready"
        async def change():
            async with get_db_session() as db:
                asset = await db.get(FileAsset, args["attachment_ids"][0])
                asset.description = "用户纠正：是室外摊位。"
        adapter = Adapter(on_call=change)
        monkeypatch.setattr(guard, "ConfiguredWikiModel", lambda _: adapter)
        result = await guard.guard_review(ARGS, ctx)
        assert result.metadata["error"] == "REVISION_REVIEW_UNAVAILABLE"
        [call] = adapter.calls
        assert "description" not in call["data"]["current_assets"][0]
        assert call["data"]["current_assets"][0]["id"] == args["attachment_ids"][0]


async def test_abort_during_check_propagates_without_creating_a_card(monkeypatch):
    monkeypatch.setattr(guard, "ConfiguredWikiModel", lambda _: Adapter(error=asyncio.CancelledError()))
    async with revision() as (ctx, _, _):
        with pytest.raises(asyncio.CancelledError):
            await guard.guard_review(ARGS, ctx)


@pytest.mark.parametrize("value", [
    {"allowed": True, "violations": ["Changed timings"]},
    {"allowed": False, "violations": []},
    {"allowed": True, "violations": [], "extra": "approval"},
    {"allowed": 1, "violations": []},
    {"allowed": False, "violations": [" "]},
])
async def test_invalid_verdict_is_not_permission(value):
    evidence = guard.RevisionEvidence("i", "q", "c", "Only change font size", ({"question": "Before"},))
    with pytest.raises(guard.ReviewUnavailable):
        await guard.check_revision(evidence, [], adapter=Adapter(value))


async def test_large_input_is_rejected_without_truncation_or_provider_call():
    adapter = Adapter()
    evidence = guard.RevisionEvidence("i", "q", "c", "x" * guard.MAX_INPUT_CHARS, ())
    with pytest.raises(guard.ReviewUnavailable, match="too_large"):
        await guard.check_revision(evidence, [], adapter=adapter)
    assert adapter.calls == []


async def test_ordinary_question_has_no_semantic_check(monkeypatch):
    def forbidden(*_):
        pytest.fail("Ordinary questions do not add a review-model call")
    monkeypatch.setattr(guard, "ConfiguredWikiModel", forbidden)
    assert await guard.guard_review(ARGS, ToolContext()) is None
    monkeypatch.setattr(guard, "get_db_session", forbidden)
    assert await guard.context_fragment(SimpleNamespace(parts=[{"text": "ordinary question"}]), ToolContext()) is None


async def test_review_uses_its_configured_model_without_router_fallback(monkeypatch):
    from core.config import OpenBoxConfig
    config = OpenBoxConfig(assistant={"revision_review_model": "test/vision-review"},
                           memory={"read_router_model": "test/short-intent"})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    evidence = guard.RevisionEvidence("i", "q", "c", "Only change font size", ({"question": "Before"},))
    adapter = Adapter()
    await guard.check_revision(evidence, [], adapter=adapter)
    assert adapter.calls[0]["model"] == "test/vision-review"
