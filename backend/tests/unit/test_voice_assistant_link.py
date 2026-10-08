"""assistant_ask's assistant side, on the real Inbox: accept, wait, read the reply, map failures."""
import asyncio

import pytest

from agent import inbox
from agent.driver import reserve_run
from assistant.service import ensure_main_session
from core.identifier import generate_id
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.voice import VoiceTurn
from models.message import TextPart
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from voice import assistant_link, calls, phrases
from voice.assistant_link import AssistantLink, VoiceTurnRef, main_session, reply_text


@pytest.fixture(autouse=True)
def quick_steps(monkeypatch):
    woken = []
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *args: woken.append(args))
    monkeypatch.setattr(assistant_link, "WAIT_STEP_SECONDS", 0.05)
    return woken


async def setup(turn_timeout=30, **choice):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    call_id = await calls.create_call(user_id=owner, workspace_id=workspace, main_session_id=main.id,
                                      client="web", model="qwen3.8-omni-flash-realtime", voice="Serena")
    link = AssistantLink(call_id=call_id, user_id=owner, workspace_id=workspace, main_session_id=main.id,
                         lang="zh", turn_timeout=turn_timeout, **choice)
    return owner, main, link


def new_ref(text="帮我看看贪吃蛇进展"):
    return VoiceTurnRef(id=generate_id(), provider_call_id=f"call-{generate_id()}", text=text, transcript=text,
                        requested=asyncio.get_running_loop().time())


async def answer(owner, main, parts, *, outcome="succeeded", pause=0.0):
    """Run the main turn the way the agent loop settles it."""
    lease = await reserve_run(main.id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        await asyncio.sleep(pause)
        fence = (main.id, lease.run_id, lease.generation)
        message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
                                                 agent="assistant", user_id=owner, run_fence=fence)
        for text, channel in parts:
            await save_part(TextPart(session_id=main.id, message_id=message.id, text=text, channel=channel),
                            is_new=True, user_id=owner, run_fence=fence)
        message.finish = "stop"
        await update_message_info(message, user_id=owner, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome=outcome)
        return message.id, batch.messages[0].id
    finally:
        await lease.release(session_status="idle")


async def turn_row(ref):
    async with get_db_session() as db:
        return await db.get(VoiceTurn, ref.id)


async def test_start_accepts_a_voice_marked_turn_wakes_the_assistant_and_records_it(quick_steps):
    owner, main, link = await setup()
    ref = new_ref()
    await link.start(ref)
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, ref.inbox_id)
    assert item.prompt == "帮我看看贪吃蛇进展" and item.origin == "human"
    assert item.origin_ref["entrypoint"] == "assistant_voice" and item.origin_ref["voice_call_id"] == link.call_id
    assert item.origin_ref["client_message_id"] == f"voice:{link.call_id}:1"
    assert quick_steps == [(main.id, owner)]
    row = await turn_row(ref)
    assert (row.call_id, row.inbox_id, row.outcome, row.transcript) == (link.call_id, ref.inbox_id, "pending", ref.text)


async def test_the_request_goes_with_the_users_words_and_the_call_and_alone_if_that_is_too_much():
    owner, main, link = await setup()
    ref = new_ref(text="帮我查一下云杉项目的负责人是谁。")
    ref.transcript, ref.context = "你使用工具查一下呀。", {"heard": "你使用工具查一下呀。",
                                                       "call": ["用户：云山项目的负责人是谁？", "前台：我这儿没查到。"]}
    await link.start(ref)
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, ref.inbox_id)
    assert item.prompt == "帮我查一下云杉项目的负责人是谁。" and item.origin_ref["voice_context"] == ref.context
    assert (await turn_row(ref)).transcript == "你使用工具查一下呀。"  # the user's own words stay on the record
    # The origin reference has a size bound shared with other context: the request still goes, alone.
    big = new_ref(text="帮我总结一下")
    big.context = {"heard": "长" * 200, "call": ["用户：" + "长" * 2000]}
    await link.start(big)
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, big.inbox_id)
    assert item.prompt == "帮我总结一下" and "voice_context" not in item.origin_ref


async def test_voice_turns_use_the_configured_model_and_variant_else_the_main_sessions():
    owner, main, link = await setup(model="test/fast", variant="low")
    fast = new_ref()
    await link.start(fast)
    _, _, default = await setup(model="", variant=None)  # "" from VOICE_TURN_MODEL= means unset
    kept = new_ref()
    await default.start(kept)
    async with get_db_session() as db:
        chosen, inherited = await db.get(AgentInboxItem, fast.inbox_id), await db.get(AgentInboxItem, kept.inbox_id)
    assert (chosen.model, chosen.variant) == ("test/fast", "low")
    assert inherited.model == "test/model"


async def test_wait_reads_the_final_reply_cleaned_and_reports_the_message():
    owner, main, link = await setup()
    ref = new_ref()
    await link.start(ref)
    seen = []
    waiting = asyncio.create_task(link.wait(ref, on_message=seen.append))
    result_id, message_id = await answer(owner, main, [("我先看一下项目。", "commentary"),
        ("[收尾自检](/app/s/ses_01J9ABCDEFGHJKMNPQRSTVWXYZ) 做完了，一切正常。", "final")], pause=0.3)
    assert await waiting == {"status": "ok", "speech": "收尾自检做完了，一切正常。"}
    assert seen == [ref] and ref.message_id == message_id
    row = await turn_row(ref)
    assert row.result_message_id == result_id and row.message_id == message_id and row.settled_at is not None
    assert row.outcome == "pending"  # delivered is the bridge's to record


async def test_reply_text_prefers_final_parts_and_never_reads_narration():
    owner, main, link = await setup()
    ref = new_ref()
    await link.start(ref)
    result_id, _ = await answer(owner, main, [("旁白", "commentary"), ("第一句。", None), ("第二句。", None)])
    assert await reply_text(main.id, result_id, owner) == "第一句。\n第二句。"


async def test_failures_timeouts_and_hang_up_map_to_their_outputs():
    owner, main, link = await setup()
    failed = new_ref()
    await link.start(failed)
    waiting = asyncio.create_task(link.wait(failed))
    await answer(owner, main, [("出错了", "final")], outcome="error")
    assert await waiting == {"status": "failed", "speech": phrases.speech_text("failed", "zh")}
    assert (await turn_row(failed)).outcome == "failed"

    owner, main, slow = await setup(turn_timeout=0.3)
    late = new_ref()
    await slow.start(late)
    assert await slow.wait(late) == {"status": "timeout", "speech": phrases.speech_text("timeout", "zh")}
    assert (await turn_row(late)).outcome == "late"

    pending = new_ref()
    await link.start(pending)
    waiting = asyncio.create_task(link.wait(pending))
    link.closed = True
    assert await asyncio.wait_for(waiting, 2) is None


async def test_rejected_input_is_recorded_as_a_failed_turn():
    _, _, link = await setup()
    ref = new_ref(text="   ")
    with pytest.raises(ValueError):
        await link.start(ref)
    assert (await turn_row(ref)).outcome == "failed"


async def test_main_session_lookup_distinguishes_missing_entry_and_missing_access():
    owner, other, workspace = await accounts()
    assert await main_session(owner, workspace) == (workspace, None)
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    assert await main_session(owner, workspace) == (workspace, main.id)
    assert await main_session(owner, None) == (workspace, main.id)  # single-user mode: default workspace
    stranger, _, _ = await accounts()
    assert await main_session(stranger, workspace) == (None, None)
