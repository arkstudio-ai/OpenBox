"""Recreate persisted send cards from releases before direct task handoff.

Only the initial suspension is simulated. Replies, continuation admission,
scope checks, receipt consumption and dispatch use the current real runtime.
"""
from contextlib import contextmanager
from unittest.mock import patch

from assistant.confirmations import CONFIRM, CANCEL, KIND, EXPIRES_AFTER
from db.base import get_db_session
from db.models.part import Part
from question import question as q, runtime


async def _ask_old_send(ctx, *, digest, kind=KIND):
    assert kind == KIND
    async with get_db_session() as db:
        part = await db.get(Part, ctx.part_id)
        saved = part.data["input"]
    target = ({"request_id": saved["request_id"]} if "request_id" in saved
              else {"task_id": saved["task_id"]})
    await q.ask(ctx.session_id, [q.Question(
        question=saved.get("text") or saved["instructions"], header="确认发送", custom=False,
        options=[q.QuestionOption(label=CONFIRM), q.QuestionOption(label=CANCEL)])],
        tool={"callID": ctx.part_id, "messageID": ctx.message_id}, user_id=ctx.user_id,
        continuation={"kind": "question", KIND: {**target, "digest": digest, "confirm": CONFIRM}},
        expires_at=runtime.now() + EXPIRES_AFTER)


@contextmanager
def legacy_send_cards():
    with patch("assistant.confirmations.consume_card", _ask_old_send):
        yield
