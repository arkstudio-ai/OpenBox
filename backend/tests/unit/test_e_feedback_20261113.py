"""gw2 2026-11-13, account e: a skipped card that still spent credits, a talking-head brief sent to the clip
helper, a followup that died on a stored JSON string, a status said after every hand-over failed, and cards
that could only be skipped."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db.base import get_db_session
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_requests_v2 import ask_in
from tests.unit.test_assistant_sessions_v2 import assistant, conversation, quiet_runtime  # noqa: F401


# --- review cards -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ({"questions": [{"purpose": "review", "review_content": "方案", "question": "可以吗？"}]}, True),
    ({"questions": '[{"purpose": "review", "review_content": "方案", "question": "可以吗？"}]'}, True),
    ('{"questions": [{"purpose": "review", "review_content": "方案"}]}', True),
    ({"questions": '[{"purpose": "clarification", "question": "发图"}]'}, False),
    ({"questions": "not json"}, False),
    ({"questions": ["x", 3, None]}, False),
    (None, False),
])
def test_a_review_card_is_recognised_whatever_shape_the_model_sent(raw, expected):
    from assistant.review_baseline import is_review
    assert is_review(SimpleNamespace(questions=[{"question": "可以吗？"}]), {"input": raw}) is expected


def test_new_review_cards_carry_their_purpose_and_keep_the_light_step():
    from assistant.review_baseline import is_review
    from tool.question_tool import QuestionArgs, to_questions
    args = QuestionArgs.model_validate({"questions": [{"question": "请确认视频方案：", "purpose": "review",
        "review_content": "画面：老友粉特写", "step_id": "confirm", "options": [{"label": "就这样，开始做"}]}]})
    stored = [item.model_dump() for item in to_questions(args)]
    assert stored[0]["detail"] == {"light": {"step_id": "confirm"}, "purpose": "review"}
    assert is_review(SimpleNamespace(questions=stored), {"input": {"questions": "garbage"}})
    plain = QuestionArgs.model_validate({"questions": [{"question": "发一张照片？"}]})
    assert to_questions(plain)[0].detail is None


async def test_a_followup_survives_a_card_whose_questions_were_stored_as_a_string():
    from agent import inbox
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "餐饮菜单制作", visibility="private")
    _, part_id = await ask_in(session, owner, question="确认菜单内容与价格")
    async with get_db_session() as db:
        part = await db.get(Part, part_id)
        part.data = {**part.data, "input": {"questions": '[{"question": "确认菜单内容与价格", "custom": false}]'}}
    receipt = await inbox.accept_inbox_item(session_id=session.id, user_id=owner, delivery="followup",
        prompt="按这六道菜重新排版", client_id="menu-followup", origin="human", origin_ref={"actor_user_id": owner})
    assert "review_baseline" not in receipt.origin_ref


# --- a skipped card is not a go-ahead -----------------------------------------------------------------------

def test_only_paid_generation_is_gated():
    from question.skip_gate import spends
    assert spends("image_gen", {"prompt": "海报"})
    assert spends("video_generate", {"action": "submit"}) and spends("video_compose", {"action": "submit"})
    assert not spends("video_generate", {"action": "status"}) and not spends("video_generate", {"action": "validate"})
    assert not spends("question", {}) and not spends("video_compose", None)


async def test_a_skipped_card_blocks_spending_until_the_user_speaks_again():
    from question import question as questions
    from question.skip_gate import standing_skip
    from session.session import create_user_message
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "30秒南宁特色美食宣传视频", visibility="private")
    card_id, _ = await ask_in(session, owner, question="请确认视频方案：")
    async with get_db_session() as db:
        assert await standing_skip(db, session.id, owner) is None  # still pending: the pending gate owns it
    await questions.reject(card_id, user_id=owner)
    async with get_db_session() as db:
        assert await standing_skip(db, session.id, owner) == card_id
    # A platform wake-up ("the job finished, carry on") is not the user changing their mind.
    await create_user_message(session_id=session.id, text="系统提示：视频任务已在后台生成完成。", user_id=owner,
                              origin="system_recovery", synthetic=True)
    async with get_db_session() as db:
        assert await standing_skip(db, session.id, owner) == card_id
    await create_user_message(session_id=session.id, text="就按刚才的方案做吧", user_id=owner, origin="human",
                              origin_ref={"actor_user_id": owner})
    async with get_db_session() as db:
        assert await standing_skip(db, session.id, owner) is None


async def test_an_answered_card_and_a_registered_card_do_not_block():
    from question import question as questions
    from question.skip_gate import standing_skip
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "海报", visibility="private")
    card_id, _ = await ask_in(session, owner, question="海报主题")
    async with get_db_session() as db:
        card = await db.get(QuestionCheckpoint, card_id)
        card.status = "rejected"
        card.questions = [{**card.questions[0], "detail": {"kind": "video_model"}}]
    async with get_db_session() as db:
        assert await standing_skip(db, session.id, owner) is None
        card = await db.get(QuestionCheckpoint, card_id)
        card.questions = [{key: value for key, value in card.questions[0].items() if key != "detail"}]
        card.status = "expired"
    async with get_db_session() as db:
        assert await standing_skip(db, session.id, owner) == card_id
        (await db.get(QuestionCheckpoint, card_id)).status = "answered"
    async with get_db_session() as db:
        assert await standing_skip(db, session.id, owner) is None


def test_the_skip_receipt_tells_the_helper_to_stop():
    import inspect
    from question import continuation
    source = inspect.getsource(continuation)
    assert "skipping is not a go-ahead" in source
    from assistant.commands import VIDEO_WORKFLOW_GUIDANCE
    for workflow in ("light-video", "poster-maker", "menu-maker", "footage-edit"):
        assert "skipped or left unanswered approves nothing" in VIDEO_WORKFLOW_GUIDANCE[workflow]


# --- putting a card away ------------------------------------------------------------------------------------

async def test_a_folded_card_expires_in_thirty_minutes_or_when_the_call_ends():
    from assistant.requests import _revision
    from question import question as questions, runtime
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "做海报", visibility="private")
    card_id, _ = await ask_in(session, owner, question="海报主题")
    before = runtime.now()
    view = await questions.fold(card_id, user_id=owner)
    assert view.folded and view.status == "pending"
    async with get_db_session() as db:
        card = await db.get(QuestionCheckpoint, card_id)
        deadline = runtime.utc(card.expires_at)
        assert timedelta(minutes=29) < deadline - before <= timedelta(minutes=30, seconds=5)
        binding = (card.continuation or {}).get("assistant_request")
        if binding:  # the versioned reply path still recognises the card
            assert binding["request_revision"] == _revision(card, binding)
    again = await questions.fold(card_id, user_id=owner)  # folding twice does not push the deadline back
    assert again.expires_at == view.expires_at
    assert await questions.expire_folded(owner) == 1
    async with get_db_session() as db:
        card = await db.get(QuestionCheckpoint, card_id)
        assert runtime.utc(card.expires_at) <= runtime.now()
    from question.continuation import expire_questions
    await expire_questions()
    async with get_db_session() as db:
        assert (await db.get(QuestionCheckpoint, card_id)).status == "expired"
    from question.skip_gate import standing_skip
    async with get_db_session() as db:
        assert await standing_skip(db, session.id, owner) == card_id


async def test_cards_that_were_not_folded_are_left_alone_when_a_call_ends():
    from question import question as questions
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "做菜单", visibility="private")
    card_id, _ = await ask_in(session, owner, question="菜单内容")
    assert await questions.expire_folded(owner) == 0
    async with get_db_session() as db:
        card = await db.get(QuestionCheckpoint, card_id)
        assert card.status == "pending" and card.expires_at is None


# --- which helper gets the work -----------------------------------------------------------------------------

@pytest.mark.parametrize("brief, word", [
    ("制作一条30秒的南宁特色美食宣传视频。交付内容：拟定吸引人的口播讲稿，规划分镜画面。", "口播"),
    ("需要主播出镜介绍招牌菜", "主播"),
    ("A presenter introduces the dishes", "presenter"),
    ("老友粉特写，热气升腾，街头烟火气", None),
    ("美食特写，不要口播，不要人物出镜", None),
    ("No voice-over, close-ups only", None),
    ("", None),
])
def test_a_brief_that_wants_someone_speaking_is_not_a_light_clip(brief, word):
    from light.work_types import asks_for_presenter
    assert asks_for_presenter(brief) == word


def test_the_assistant_is_told_what_the_light_clip_cannot_do():
    from light.prompt import ASSISTANT_LIGHT_RULES
    from tool.assistant_tools import SubmitArgs
    assert "不要选 light_video" in "".join(ASSISTANT_LIGHT_RULES)
    assert "never light_video" in SubmitArgs.model_fields["work_type"].description


def test_a_piece_finished_long_ago_is_not_work_in_progress():
    from assistant.commands import FINISHED_TASK_HOURS, finished_long_ago
    now = datetime(2026, 11, 13, 9, 50, tzinfo=timezone.utc)
    task = lambda **values: SimpleNamespace(**{"desired_state": "running", "observed_state": "completed",
                                               "updated_at": now - timedelta(hours=16), **values})
    assert finished_long_ago(task(), now)
    assert finished_long_ago(task(updated_at=(now - timedelta(hours=16)).replace(tzinfo=None)), now)
    assert not finished_long_ago(task(updated_at=now - timedelta(hours=FINISHED_TASK_HOURS - 1)), now)
    assert not finished_long_ago(task(observed_state="running"), now)
    assert not finished_long_ago(task(observed_state="waiting_input"), now)
    assert not finished_long_ago(task(desired_state="canceled"), now)


def test_on_the_phone_a_small_question_still_keeps_the_dispatch_rule():
    from voice.prompt import DIRECT_ANSWER, PHONE_DISPATCH, voice_turn_block
    block = voice_turn_block({"direct_answer": True})
    assert PHONE_DISPATCH in block and DIRECT_ANSWER in block
    assert block.index(PHONE_DISPATCH) < block.index(DIRECT_ANSWER)
    ordinary = voice_turn_block({})
    assert PHONE_DISPATCH in ordinary and DIRECT_ANSWER not in ordinary


# --- a status said after every hand-over failed -------------------------------------------------------------

@pytest.mark.parametrize("text, claims", [
    ("帮手那边正在按照这六道菜和对应价格做排版出图，做好了我第一时间通知你。", True),
    ("其实帮手早就在做了，你的新要求也已经转进现场。", True),
    ("图片已经转交给「餐饮美食宣传海报」的帮手了", True),
    ("菜单已经做好了，成图在对话里。", False),
    ("这张图是六道家常菜拼图，你想怎么排？", False),
    ("刚才没转过去，帮手没有收到这张图。", False),
])
def test_what_counts_as_saying_work_is_under_way(text, claims):
    from assistant.follow_through import UNDER_WAY
    assert bool(UNDER_WAY.search(text)) is claims


async def test_a_status_after_only_failed_handovers_gets_one_corrective_step(monkeypatch):
    from assistant import follow_through

    def run_with(*parts):
        class Result:
            def all(self):
                return list(parts)

        class Db:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def scalars(self, _):
                return Result()
        monkeypatch.setattr("db.base.get_db_session", lambda: Db())

    said = "帮手那边正在按照这六道菜和对应价格做排版出图"
    check = lambda text=said, kind="assistant": follow_through.handover_failed(
        session_kind=kind, text=text, session_id="s", user_id="u", message_ids=["m"])
    crashed = {"tool": "tasks.followup", "status": "error", "error": "'str' object has no attribute 'get'"}
    refused = {"tool": "assets.attach", "status": "completed",
               "metadata": {"error": True, "failure_code": "ASSISTANT_REVISION_CONFLICT"}}
    accepted = {"tool": "tasks.followup", "status": "completed", "metadata": {}}
    read = {"tool": "tasks.get", "status": "completed", "metadata": {}}
    run_with(read, refused, crashed, crashed)
    assert await check()
    assert not await check(text="这张图没能转过去，帮手没有收到。")
    assert not await check(kind="normal")
    run_with(read, refused, accepted)
    assert not await check()
    run_with(read)  # nothing was handed over this turn: a status about earlier work is not this guard's business
    assert not await check()
    run_with(refused)
    assert not await follow_through.started_background_work("s", "u", ["m"])


# --- the after-turn summary keeps out of the way ------------------------------------------------------------

async def test_the_rolling_summary_waits_after_being_cut_short_and_yields_to_input():
    from agent import inbox
    from agent.compaction import ROLLING_RETRY_MINUTES, rolling_summary_due
    from db.models.message import Message
    from session.session import create_assistant_message
    owner, _, workspace, main = await assistant()
    assert await rolling_summary_due(main.id, owner)
    cut = await create_assistant_message(session_id=main.id, parent_id="", model_id="m", agent="compaction",
                                         user_id=owner)
    async with get_db_session() as db:
        (await db.get(Message, cut.id)).finish = "aborted"
    assert not await rolling_summary_due(main.id, owner)
    async with get_db_session() as db:
        row = await db.get(Message, cut.id)
        row.created_at = row.created_at - timedelta(minutes=ROLLING_RETRY_MINUTES + 1)
    assert await rolling_summary_due(main.id, owner)
    async with get_db_session() as db:
        (await db.get(Message, cut.id)).finish = "stop"
    assert await rolling_summary_due(main.id, owner)
    await inbox.accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup", prompt="菜单上要有价格",
                                  client_id="waiting-words", origin="human", origin_ref={"actor_user_id": owner})
    assert not await rolling_summary_due(main.id, owner)
