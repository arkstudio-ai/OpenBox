"""Visible review cards remain readable after supersession without technical logs."""
from datetime import datetime, timezone
import json

from sqlalchemy import select

from assistant.history import read_history
from core.identifier import ascending
from db.base import get_db_session
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from models.message import ToolPartData, ToolStatus
from session.session import save_part
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import read_turn


async def test_superseded_review_is_source_content_but_not_approval():
    ctx, lease, _, accepted, report = await read_turn()
    try:
        part = ToolPartData(tool="question", status=ToolStatus.ERROR, error="superseded",
            input={"questions": [{"review_content": "unshown model input must not win"}]},
            session_id=accepted["execution_session_id"], message_id=report.id)
        await save_part(part, is_new=True, user_id=ctx.user_id)
        now = datetime.now(timezone.utc)
        async with get_db_session() as db:
            q = QuestionCheckpoint(id=ascending("q"), session_id=part.session_id, user_id=ctx.user_id,
                message_id=part.message_id, part_id=part.id, generation=1, status="superseded",
                questions=[{"question": "这版可以吗？", "review_content": "十五秒竖屏；字幕原句：巷口鲜果，周末到货。",
                            "purpose": "review"}], answers=None, draft=[], continuation={},
                applied=False, detached=False, created_at=now, updated_at=now)
            db.add(q)
            db.add(Part(id=ascending("part"), user_id=ctx.user_id, session_id=part.session_id,
                message_id=part.message_id, type="tool", data={"tool": "bash", "output": "PRIVATE_TECHNICAL_LOG"}, created_at=now))
        value = await read_history(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            session_id=part.session_id, include_tools=False)
        text = json.dumps(value, ensure_ascii=False)
        assert "巷口鲜果，周末到货" in text and 'superseded' in text and 'not current execution approval' in text
        assert "unshown model input" not in text and "PRIVATE_TECHNICAL_LOG" not in text
        async with get_db_session() as db:
            row = await db.scalar(select(QuestionCheckpoint).where(QuestionCheckpoint.part_id == part.id))
            row.user_id = "other-owner"
        hidden = await read_history(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            session_id=part.session_id, include_tools=False)
        assert "巷口鲜果，周末到货" not in json.dumps(hidden, ensure_ascii=False)
    finally:
        await lease.release(session_status="idle")
