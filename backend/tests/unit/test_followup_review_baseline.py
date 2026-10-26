"""Photo sent before voice handoff must not lose the original displayed draft."""
import pytest

from agent import inbox
from agent.driver import reserve_run
from assistant import review_guard as guard
from db.base import get_db_session
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_requests_v2 import ask_in
from tests.unit.test_assistant_sessions_v2 import assistant, conversation, quiet_runtime  # noqa: F401
from tests.unit.test_revision_review_guard import Adapter, ARGS
from tool.tool import ToolContext


async def displayed_review():
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "原任务", visibility="private")
    card_id, part_id = await ask_in(session, owner, question="15秒，字幕：今天开门啦；原声吉他。")
    async with get_db_session() as db:
        part = await db.get(Part, part_id)
        part.data = {**part.data, "input": {"questions": [{"purpose": "review",
            "review_content": "15秒，字幕：今天开门啦；原声吉他。", "question": "可以吗？"}]}}
    return owner, workspace, session, card_id


@pytest.mark.parametrize("mutate", [False, True])
async def test_followup_binds_old_displayed_draft_after_card_was_superseded(monkeypatch, mutate):
    owner, workspace, session, card_id = await displayed_review()
    # Mirrors sending a photo before voice hands off an edit: the visible card
    # is no longer pending, but its script remains the task's prior proposal.
    async with get_db_session() as db:
        (await db.get(QuestionCheckpoint, card_id)).status = "superseded"
    receipt = await inbox.accept_inbox_item(session_id=session.id, user_id=owner, delivery="followup",
        prompt="只换成店里的照片，字幕和音乐别改，先让我看看。", client_id="photo-followup",
        origin="human", origin_ref={"actor_user_id": owner})
    assert receipt.origin_ref["review_baseline"]["request_id"] == card_id
    duplicate = await inbox.accept_inbox_item(session_id=session.id, user_id=owner, delivery="followup",
        prompt="只换成店里的照片，字幕和音乐别改，先让我看看。", client_id="photo-followup",
        origin="human", origin_ref={"actor_user_id": owner})
    assert duplicate.id == receipt.id and duplicate.origin_ref == receipt.origin_ref
    lease = await reserve_run(session.id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        ctx = ToolContext(session_id=session.id, user_id=owner, workspace_id=workspace,
            project_id=session.project_id, run_id=lease.run_id, run_generation=lease.generation,
            _assert_current=lease.assert_current)
        evidence = await guard.bound_revision(ctx)
        assert evidence.previous_questions[0]["question"].startswith("15秒，字幕：今天开门啦")
        assert evidence.human_requests == ("只换成店里的照片，字幕和音乐别改，先让我看看。",)
        fragment = await guard.context_fragment(batch.messages[0], ctx)
        assert "今天开门啦" in fragment
        adapter = Adapter(value={"allowed": False, "violations": ["字幕被擅自改写，必须保持今天开门啦。"]})
        monkeypatch.setattr(guard, "ConfiguredWikiModel", lambda _: adapter)
        if mutate:
            async with get_db_session() as db:
                (await db.get(QuestionCheckpoint, card_id)).questions = [{"question": "另一个方案"}]
        result = await guard.guard_review(ARGS, ctx)
        assert result.metadata["error"] == ("REVISION_REVIEW_UNAVAILABLE" if mutate else "REVISION_REVIEW_OUT_OF_SCOPE")
        assert len(adapter.calls) == (0 if mutate else 1)
    finally:
        await lease.release(session_status="idle")


async def test_clarifications_and_foreign_cards_do_not_become_review_baselines():
    owner, workspace, session, card_id = await displayed_review()
    async with get_db_session() as db:
        card = await db.get(QuestionCheckpoint, card_id)
        part = await db.get(Part, card.part_id)
        part.data = {**part.data, "input": {"questions": [{"purpose": "clarification", "question": "请发图片"}]}}
        from assistant.review_baseline import bind_locked
        ref = {"actor_user_id": owner}
        assert await bind_locked(db, session, "human", ref) == ref
        part.data = {**part.data, "input": {"questions": [{"purpose": "review", "review_content": "另一账号方案"}]}}
        from db.models.user import User
        from sqlalchemy import select
        other = await db.scalar(select(User.id).where(User.id != owner).limit(1))
        card.user_id = other
        await db.flush()
        assert await bind_locked(db, session, "human", ref) == ref
