"""Server-driven cards (question.cards) end to end, with the video model switch card as the first kind.

A card is filed for a running tool call, shows the App its kind, presentation
and option roles, and its answer is applied in the same transaction that
resumes the call: the conversation's video model changes with the answer.
"""
from types import SimpleNamespace

import pytest

from agent import inbox
from agent.driver import reserve_run
from db.base import get_db_session
from db.models.part import Part
from db.models.session import Session
from models.message import ToolPartData
from question import cards, question as q, runtime
from question.continuation import apply_answers
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import MAIN_MESSAGES, assistant, card, conversation
from tool import video_model_card as vmc


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "card-pipeline-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    MAIN_MESSAGES.clear()


CURRENT = vmc.VideoChoice("wan3.0-video", "720p")
WANTED = vmc.VideoChoice("MiniMax-H3", "768p")


async def file_switch(session, owner, *, picked=False):
    """A video_generate call in `session` asks to use another model and waits on the card."""
    await inbox.accept_inbox_item(session_id=session.id, user_id=owner, delivery="followup", prompt="用海螺做",
                                  origin="human", origin_ref={"actor_user_id": owner})
    lease = await reserve_run(session.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    ticket = await runtime.start_run(session.id, owner, driver_lease=lease)
    token = runtime.current_run.set(ticket)
    try:
        fence = (session.id, lease.run_id, lease.generation)
        message = await create_assistant_message(session.id, batch.messages[0].id, agent="build",
            model_id="test/model", user_id=owner, run_fence=fence)
        part = ToolPartData(session_id=session.id, message_id=message.id, tool="video_generate", status="running")
        await save_part(part, is_new=True, user_id=owner, run_fence=fence)
        ctx = SimpleNamespace(session_id=session.id, user_id=owner, message_id=message.id, part_id=part.id)
        from core.config import get_config
        with pytest.raises(q.QuestionSuspended) as suspended:
            await vmc.ask_switch(ctx, get_config(), CURRENT, WANTED, picked=picked)
        message.finish = "waiting_input"
        await update_message_info(message, user_id=owner, run_fence=fence)
        await runtime.finish_run(ticket)
    finally:
        runtime.current_run.reset(token)
        await lease.release(session_status="waiting_input")
    return suspended.value.request_id, ctx


async def video_of(session_id):
    async with get_db_session() as db:
        row = await db.get(Session, session_id)
        return row.video_model, row.video_resolution


async def test_card_shows_its_kind_presentation_and_option_roles():
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "净水器宣传片", visibility="private")
    request_id, _ = await file_switch(session, owner)
    row = await card(request_id)
    [question] = row.questions
    detail = question["detail"]
    assert detail["kind"] == vmc.KIND and detail["present"] == "sheet"
    assert [option["id"] for option in detail["options"]] == ["switch", "keep"]
    assert [option["role"] for option in detail["options"]] == ["choose", "cancel"]
    assert detail["options"][1]["recommended"] is True
    assert question["custom"] is False and question["multiple"] is False
    assert [option["label"] for option in question["options"]] == [
        option["label"] for option in row.continuation["card"]["options"]]
    assert row.continuation["card"]["payload"] == {"current": ["wan3.0-video", "720p"],
                                                   "requested": ["MiniMax-H3", "768p"]}


async def test_switch_answer_sets_the_conversations_model_and_resumes_the_call():
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "净水器宣传片", visibility="private")
    request_id, _ = await file_switch(session, owner)
    row = await card(request_id)
    switch = row.continuation["card"]["options"][0]["label"]
    await q.reply(request_id, [[switch]], owner)
    assert await apply_answers(session.id, owner) is not None
    assert await video_of(session.id) == ("MiniMax-H3", "768p")
    async with get_db_session() as db:
        part = await db.get(Part, row.part_id)
    assert part.data["status"] == "completed" and part.data["title"] == "视频模型已切换"
    assert part.data["metadata"]["card"] == {"kind": vmc.KIND, "option": "switch"}
    assert "WITHOUT model and resolution" in part.data["output"]


async def test_dismissing_the_card_keeps_and_locks_the_current_model():
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "净水器宣传片", visibility="private")
    request_id, _ = await file_switch(session, owner)
    await q.reject(request_id, owner)
    assert await apply_answers(session.id, owner) is not None
    assert await video_of(session.id) == ("wan3.0-video", "720p")
    row = await card(request_id)
    async with get_db_session() as db:
        part = await db.get(Part, row.part_id)
    assert part.data["title"] == "保持原视频模型"
    assert part.data["metadata"]["card"] == {"kind": vmc.KIND, "option": None}


async def test_assistant_and_voice_may_answer_a_registered_card():
    from assistant.request_answers import answerable
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "净水器宣传片", visibility="private")
    request_id, _ = await file_switch(session, owner)
    async with get_db_session() as db:
        row = await db.get(type(await card(request_id)), request_id)
        target = await db.get(Session, session.id)
        reason, high_risk = await answerable(db, row, target)
    assert reason is None and high_risk is False
    assert cards.voice_label(row.continuation) == "换视频模型"


async def test_a_second_switch_waits_for_the_first_card():
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "净水器宣传片", visibility="private")
    _, ctx = await file_switch(session, owner)
    from core.config import get_config
    from tool.video_production import _RequestError
    with pytest.raises(_RequestError, match="already waiting"):
        await vmc.ask_switch(ctx, get_config(), CURRENT, WANTED, picked=False)


def test_unknown_kinds_and_bad_options_are_refused():
    assert cards.get("no_such_card") is None
    assert cards.chosen({"options": [{"id": "a", "label": "A", "role": "choose"},
                                     {"id": "b", "label": "B", "role": "cancel"}]}, [["B"]]) is None
    assert cards.chosen({"options": [{"id": "a", "label": "A", "role": "choose"}]}, [["A"]])["id"] == "a"
    with pytest.raises(ValueError):
        cards.register(cards.CardKind(kind=vmc.KIND, voice_label="x", apply=lambda *_: None))
