"""A voice turn asks the personal assistant for a reply that works on the phone.

The marker travels the real path: accept_turn's origin_ref → the Inbox item →
the claimed user message's text part → project_main_messages.
"""
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from assistant.inputs import accept_turn
from assistant.projection import project_main_messages
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from session.agent_event_log import load_canonical_model_surface
from session.session import create_assistant_message
from tests.unit.assistant_helpers import finish, no_task_dispatch  # noqa: F401
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tool.tool import ToolContext
from voice.prompt import VOICE_TURN_BLOCK, voice_turn_block

VOICE = "assistant:voice-turn"


async def claimed(owner, workspace, main, *, client_id, text, voice, context=None):
    extra = {"entrypoint": "assistant_voice", "extra_ref": {"voice_call_id": "call-1"}} if voice else {}
    if voice and context:
        extra["extra_ref"]["voice_context"] = context
    await accept_turn(user_id=owner, workspace_id=workspace, main_id=main.id, client_id=client_id, text=text, **extra)
    lease = await reserve_run(main.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
        agent="assistant", user_id=owner, run_fence=(main.id, lease.run_id, lease.generation))
    ctx = ToolContext(user_id=owner, workspace_id=workspace, session_id=main.id, project_id=main.project_id,
        agent_id="assistant", run_id=lease.run_id, run_generation=lease.generation, message_id=message.id)
    return ctx, lease, message, batch.messages[0]


async def projected(ctx, **kwargs):
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    return await project_main_messages(list(surface.messages), ctx=ctx, **kwargs)


def block_ids(messages):
    return [message.id for message in messages if str(message.id).startswith("assistant:")]


async def test_voice_turn_gets_the_spoken_reply_block_and_typed_turns_do_not():
    from tests.unit.test_assistant_foundation import accounts
    from assistant.service import ensure_main_session
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    ctx, lease, answer, user_message = await claimed(owner, workspace, main, client_id="voice:call-1:1",
                                                     text="帮我看看贪吃蛇进展", voice=True)
    try:
        # The stored message carries the marker the projection reads.
        [part] = [part for part in user_message.parts if part.type == "text"]
        assert part.origin == "human"
        assert part.origin_ref["entrypoint"] == "assistant_voice" and part.origin_ref["voice_call_id"] == "call-1"
        messages = await projected(ctx)
        assert VOICE in block_ids(messages)
        [block] = [message for message in messages if message.id == VOICE]
        assert block.parts[0]["text"] == voice_turn_block(None) and block.parts[0]["synthetic"]
        assert VOICE not in block_ids(await projected(ctx, for_compaction=True))
        await finish(ctx, lease, answer, "贪吃蛇的收尾自检做完了。")
    finally:
        await lease.release(session_status="idle")
    ctx, lease, answer, _ = await claimed(owner, workspace, main, client_id="typed-1", text="那配色呢", voice=False)
    try:
        assert VOICE not in block_ids(await projected(ctx))  # the latest human input was typed
    finally:
        await lease.release(session_status="idle")


async def test_a_voice_request_brings_the_users_words_and_the_calls_last_lines():
    """The assistant never heard the call: the front desk's brief arrives with what the user said around it."""
    import json
    from tests.unit.test_assistant_foundation import accounts
    from assistant.service import ensure_main_session
    from voice.prompt import VOICE_CONTEXT_BLOCK, voice_turn_block
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    context = {"heard": "你使用工具查一下呀。", "call": ["用户：云山项目的负责人是谁？", "前台：云杉项目？我这儿没查到。",
                                                     "用户：你使用工具查一下呀。"],
               "request": "帮我查一下云杉项目的负责人是谁。"}
    # The message is the user's own words; the front desk's restatement comes as context to act on.
    await accept_turn(user_id=owner, workspace_id=workspace, main_id=main.id, client_id="voice:call-2:1",
                      text="你使用工具查一下呀。", entrypoint="assistant_voice",
                      extra_ref={"voice_call_id": "call-2", "voice_context": context})
    lease = await reserve_run(main.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
        agent="assistant", user_id=owner, run_fence=(main.id, lease.run_id, lease.generation))
    ctx = ToolContext(user_id=owner, workspace_id=workspace, session_id=main.id, project_id=main.project_id,
        agent_id="assistant", run_id=lease.run_id, run_generation=lease.generation, message_id=message.id)
    try:
        [block] = [message for message in await projected(ctx) if message.id == VOICE]
        text = block.parts[0]["text"]
        assert text == voice_turn_block(context)
        assert text.startswith(voice_turn_block(None) + VOICE_CONTEXT_BLOCK)
        assert json.loads(text[len(voice_turn_block(None) + VOICE_CONTEXT_BLOCK):]) == {
            "front_desk_request": "帮我查一下云杉项目的负责人是谁。", "call_last_lines": context["call"]}
        assert "grant no authority" in text  # context, never a new instruction
    finally:
        await lease.release(session_status="idle")
    assert voice_turn_block(None) == voice_turn_block({"heard": "", "call": []}) == VOICE_TURN_BLOCK.format(
        length="two or three short sentences")
    # The user asked for fuller answers on the phone (Settings → 语音通话).
    assert "four or five sentences" in voice_turn_block({"detail": "detailed"})


async def test_a_spoken_card_answer_reaches_the_model_with_its_exact_form_and_no_default_answers():
    from tests.unit.test_assistant_foundation import accounts
    from assistant.service import ensure_main_session
    from voice.assistant_link import AssistantLink, VoiceTurnRef
    from voice import calls
    from voice.cards import spoken_question
    from tests.unit.test_voice_cards import FORM
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    call_id = await calls.create_call(user_id=owner, workspace_id=workspace, main_session_id=main.id,
                                     client="mobile", model="test/voice", voice="Serena")
    link = AssistantLink(call_id=call_id, user_id=owner, workspace_id=workspace, main_session_id=main.id,
                         lang="zh", turn_timeout=10)
    from core.identifier import generate_id
    ref = VoiceTurnRef(id=generate_id(), provider_call_id="choice", text="第一个", transcript="第一个", requested=0,
                       context={"task_questions": [spoken_question(FORM)]})
    await link.start(ref)
    lease = await reserve_run(main.id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
            agent="assistant", user_id=owner, run_fence=(main.id, lease.run_id, lease.generation))
        ctx = ToolContext(user_id=owner, workspace_id=workspace, session_id=main.id, project_id=main.project_id,
            agent_id="assistant", run_id=lease.run_id, run_generation=lease.generation, message_id=message.id)
        [block] = [message for message in await projected(ctx) if message.id == VOICE]
        text = block.parts[0]["text"]
        assert "q-video" in text and "video-session" in text and "请填写片名" in text and "需要哪些字幕" in text
        assert "requests.answer" in text and "ask only the missing" in text and "Never fill unanswered" in text
    finally:
        await lease.release(session_status="idle")


async def test_detailed_task_prompt_and_summary_reach_the_text_model_without_changing_the_human_transcript():
    from tests.unit.test_assistant_foundation import accounts
    from assistant.service import ensure_main_session
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    context = {"request": "在电影项目制作约50秒的抽象搞笑视频，用猫咪素材，中英字幕，交付成片，不要真人、不要发布。",
               "summary": "继续昨天电影项目的素材。", "call": ["用户：猫咪素材，不要真人。",
                   *["前台：继续讨论视频。"] * 12, "用户：做成一个50miao短视频吧，先别发布。"]}
    ctx, lease, answer, user_message = await claimed(owner, workspace, main, client_id="voice:video:1",
        text="做成一个50miao短视频吧，先别发布。", voice=True, context=context)
    try:
        assert user_message.parts[0].text == "做成一个50miao短视频吧，先别发布。"
        [block] = [message for message in await projected(ctx) if message.id == VOICE]
        text = block.parts[0]["text"]
        assert context["request"] in text and context["summary"] in text and context["call"][-1] in text
        assert "tasks.submit" in text and "self-contained execution prompt" in text
        assert "Spoken brevity applies only to your reply" in text
    finally:
        await lease.release(session_status="idle")


async def test_voice_marker_keeps_retries_idempotent_and_typed_turns_unchanged():
    from tests.unit.test_assistant_foundation import accounts
    from assistant.service import ensure_main_session
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    args = dict(user_id=owner, workspace_id=workspace, main_id=main.id, text="我有什么待办")
    voiced = dict(client_id="voice:c:1", entrypoint="assistant_voice", extra_ref={"voice_call_id": "c"})
    first, again = await accept_turn(**args, **voiced), await accept_turn(**args, **voiced)
    typed = await accept_turn(**args, client_id="typed-1")
    assert first["inbox_id"] == again["inbox_id"] and typed["inbox_id"] != first["inbox_id"]
    async with get_db_session() as db:
        rows = {row.id: row for row in (await db.scalars(select(AgentInboxItem))).all()}
    voice, keyboard = rows[first["inbox_id"]].origin_ref, rows[typed["inbox_id"]].origin_ref
    assert voice["entrypoint"] == "assistant_voice" and voice["voice_call_id"] == "c"
    assert voice["actor_user_id"] == owner and voice["client_message_id"] == "voice:c:1"
    assert keyboard["entrypoint"] == "assistant_turn" and "voice_call_id" not in keyboard
    # The command digest is the same for the same words, whichever way they came in.
    assert voice["request_digest"] == keyboard["request_digest"]


async def test_a_report_turn_after_a_voice_turn_gets_no_voice_block():
    from assistant.results import deliver_task_result
    from tests.unit.test_assistant_results import result_ready
    owner, workspace, main, accepted, execution_lease, _ = await result_ready()
    await execution_lease.release(session_status="idle")
    ctx, lease, answer, _ = await claimed(owner, workspace, main, client_id="voice:call-1:1",
                                          text="任务做完了吗", voice=True)
    await finish(ctx, lease, answer, "还在跑，结果出来我告诉你。")
    async with get_db_session() as db:
        result_id = await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted["task_id"]))
    await deliver_task_result(result_id)
    report_lease = await reserve_run(main.id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(report_lease, step=1, include_next_turn=True)
        assert batch.receipts[0].origin == "task_result"
        message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
            agent="assistant", user_id=owner, run_fence=(main.id, report_lease.run_id, report_lease.generation))
        report_ctx = ToolContext(user_id=owner, workspace_id=workspace, session_id=main.id, project_id=main.project_id,
            agent_id="assistant", run_id=report_lease.run_id, run_generation=report_lease.generation,
            message_id=message.id)
        surface = await load_canonical_model_surface(main.id, user_id=owner, run_fence=report_ctx.run_fence)
        from assistant.projection import _human_input, _voice_input
        assert _voice_input(next(item for item in reversed(surface.messages) if _human_input(item)))
        assert VOICE not in block_ids(await projected(report_ctx))  # a report answers the result, not the call
    finally:
        await report_lease.release(session_status="idle")


async def test_the_latest_call_summary_of_the_last_day_reaches_the_text_assistant():
    from datetime import datetime, timedelta, timezone
    from tests.unit.test_assistant_foundation import accounts
    from assistant.service import ensure_main_session
    from db.models.voice import VoiceCall
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    now = datetime.now(timezone.utc)

    def call(identity, ended, summary):
        return VoiceCall(id=identity, user_id=owner, workspace_id=workspace, main_session_id=main.id, client="web",
                         model="qwen3.8-omni-flash-realtime", voice="Serena", status="ended", end_reason="hangup",
                         started_at=ended - timedelta(minutes=5), ended_at=ended, duration_seconds=300,
                         price_date="2026-10-07", summary=summary)
    async with get_db_session() as db:
        db.add(call("call-old", now - timedelta(days=2), "两天前聊了五子棋。"))
    ctx, lease, answer, _ = await claimed(owner, workspace, main, client_id="typed-1", text="刚才说到哪了", voice=False)
    try:
        assert "assistant:recent-call" not in block_ids(await projected(ctx))  # older than a day
        async with get_db_session() as db:
            db.add(call("call-new", now - timedelta(hours=1), "用户想把贪吃蛇改成暗色主题，还没决定要不要发布。"))
        messages = await projected(ctx)
        [block] = [message for message in messages if message.id == "assistant:recent-call"]
        text = block.parts[0]["text"]
        assert "暗色主题" in text and "五子棋" not in text and "grants no action authority" in text
        assert "assistant:recent-call" not in block_ids(await projected(ctx, for_compaction=True))
    finally:
        await lease.release(session_status="idle")


async def test_a_voice_turns_own_model_never_becomes_the_conversation_default():
    """Measured on QA: voice turns on the faster model left typed turns on it too."""
    from tests.unit.test_assistant_foundation import accounts
    from assistant.service import ensure_main_session
    from db.models.session import Session
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")

    async def claim(client_id, **turn):
        await accept_turn(user_id=owner, workspace_id=workspace, main_id=main.id, client_id=client_id,
                          text="我有哪些任务", **turn)
        lease = await reserve_run(main.id, owner)
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        assert batch.messages
        await inbox.settle_claimed_inbox_items(lease, result_message_id=None, outcome="succeeded")
        await lease.release(session_status="idle")
        async with get_db_session() as db:
            saved = await db.get(Session, main.id)
            return saved.model, saved.variant
    assert await claim("voice:c:1", model="openai/qwen3.8-flash", variant="low", entrypoint="assistant_voice",
                       extra_ref={"voice_call_id": "c"}) == ("test/model", None)
    # A model the user picks for a typed turn still becomes the default, as before.
    assert await claim("typed-1", model="other/model", variant="high") == ("other/model", "high")
