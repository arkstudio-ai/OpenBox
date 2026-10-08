"""How the user wants their assistant (assistant/profile.py): one preference read by both apps, the
assistant's prompt and the phone; set in Settings, or by the assistant on the user's own words."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from api import assistant as routes
from assistant import followups, profile
from assistant.policy import AssistantError
from assistant.reporting import ASSISTANT_TOOLS
from assistant.service import ensure_main_session
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemoryTombstone
from db.repository.preference_repo import PgPreferenceRepo
from memory import service as memories
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import quiet_runtime  # noqa: F401
from tool.assistant_tools import assistant_tools
from voice import prompt as voice_prompt
from voice.prompt import FRONT, FrontFacts, front_instructions


def published(monkeypatch):
    sent = []
    monkeypatch.setattr("bus.bus.publish", lambda event, data=None: sent.append((event, data)))
    return sent


@pytest.mark.parametrize("given, stored", [
    ("  Mary ", "Mary"), ("“小七”", "小七"), ("「Mary」", "Mary"), ("Mary\nSmith", "Mary Smith"),
    ("", ""), ("   ", ""), (None, ""), ("x" * 20, "x" * 20),
])
def test_a_name_is_one_trimmed_unquoted_line_and_empty_clears_it(given, stored):
    assert profile.clean_name(given) == stored


@pytest.mark.parametrize("patch", [
    {"name": "x" * 21}, {"address": "<b>老王</b>"}, {"tone": "rude"}, {"length": "long"}, {"emoji": "yes"},
    {"call_recap": 1}, {"call_detail": "chatty"}, {"persona": "y" * 301}, {"persona": "a\x07b"}, {"color": "red"},
])
def test_what_a_profile_cannot_hold(patch):
    with pytest.raises(ValueError):
        profile.merge(profile.Profile(), patch)


def test_a_stored_profile_keeps_what_still_holds():
    stored = {"assistant_profile": {"name": "小七", "tone": "rude", "emoji": True, "unknown": 1}}
    assert profile.from_extra(stored) == profile.Profile(name="小七", emoji=True)  # the bad tone falls back
    assert profile.from_extra({"assistant_name": "Mary"}).name == "Mary"  # the first version kept only the name
    assert profile.from_extra(None) == profile.Profile()


async def test_settings_save_only_what_was_sent_and_every_client_hears_of_it(monkeypatch):
    owner, _, workspace = await accounts()
    actor = {"user_id": owner, "workspace_id": workspace}
    sent = published(monkeypatch)
    await PgPreferenceRepo().upsert(owner, extra={"assistant_voice": "Tina", "assistant_name": "旧名字"})
    assert (await routes.get_profile(current_user=actor))["name"] == "旧名字"
    saved = await routes.set_profile(routes.ProfileBody(name=" Mary ", length="brief"), current_user=actor)
    assert {key: saved[key] for key in profile.FIELDS} == {**profile.Profile().as_dict(), "name": "Mary",
                                                            "length": "brief"}
    # Each field sent is the user's decision, made in Settings; the rest stays undecided.
    assert {key: value["via"] for key, value in saved["decided"].items()} == {"name": "settings", "length": "settings"}
    assert saved["intro"]["status"] == "new"
    again = await routes.set_profile(routes.ProfileBody(call_recap=False, persona="说话利落一点"), current_user=actor)
    assert (again["name"], again["length"], again["call_recap"], again["persona"]) == ("Mary", "brief", False, "说话利落一点")
    extra = (await PgPreferenceRepo().get(owner))["extra"]
    assert extra["assistant_voice"] == "Tina" and "assistant_name" not in extra  # other settings stay
    assert [event for event, _ in sent] == ["assistant.profile.updated"] * 2
    assert sent[-1][1] == {"userId": owner, "profile": again}
    assert await routes.get_profile(current_user=actor) == again
    with pytest.raises(HTTPException) as refused:
        await routes.set_profile(routes.ProfileBody(name="x" * 21), current_user=actor)
    assert refused.value.status_code == 422 and refused.value.detail["code"] == "ASSISTANT_PROFILE_INVALID"
    assert (await profile.load(owner)).name == "Mary" and len(sent) == 2  # refused: nothing changed or said


async def test_a_new_name_retires_memories_that_named_the_assistant_but_forgets_nothing(monkeypatch):
    owner, _, workspace = await accounts()
    published(monkeypatch)
    naming = await memories.create_note(user_id=owner, workspace_id=workspace, summary="用户给助理取名为小七")
    draft = await memories.create_note(user_id=owner, workspace_id=workspace,
                                       summary="松鼠青柠的演示草稿中称呼用户为「松果同学」")
    swim = await memories.create_note(user_id=owner, workspace_id=workspace, summary="用户周末一般会去游泳")
    await profile.save(owner, {"tone": "lively"})  # no new name: nothing to retire
    await profile.save(owner, {"name": "Mary"})
    async with get_db_session() as db:
        status = {row.id: row.status for row in (await db.scalars(select(UserMemory).where(
            UserMemory.user_id == owner))).all()}
        tombstones = (await db.scalars(select(MemoryTombstone).where(MemoryTombstone.user_id == owner))).all()
    assert status == {naming["id"]: "DEPRECATED", draft["id"]: "ACTIVE", swim["id"]: "ACTIVE"}
    assert tombstones == []  # retired, not forgotten: the fact could still be learned again
    assert profile.names_the_assistant("用户给助理取名为小七") and not profile.names_the_assistant("用户叫小七")


async def test_the_assistant_takes_a_name_and_an_address_from_a_real_tool_call(monkeypatch):
    from assistant.commands import ToolSource
    from models.message import ToolStatus
    from tests.unit.test_assistant_sessions_v2 import main_turn, tool_call
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    sent = published(monkeypatch)
    assert "assistant.preferences" in ASSISTANT_TOOLS
    assert "assistant.preferences" in {tool.id for tool in assistant_tools}
    ctx, lease, human = await main_turn(owner, workspace, main, "以后叫你小七吧，你叫我老王就行，回答简短点")
    try:
        _, other_tool = await tool_call(ctx, "tasks.list", {})
        with pytest.raises(AssistantError) as wrong_tool:  # another tool's call cannot rename
            await profile.set_preferences(user_id=owner, workspace_id=workspace, main_id=main.id, name="Forged",
                                          source=ToolSource(other_tool.id, ctx.run_id, ctx.run_generation, (human,)))
        assert wrong_tool.value.code == "ASSISTANT_CALL_UNVERIFIED"
        receipt, part = await tool_call(ctx, "assistant.preferences", {"name": "“小七”", "address": "老王",
                                                                         "length": "brief",
                                                                         "source_message_ids": [human]})
        assert part.status == ToolStatus.COMPLETED
        assert receipt == {"name": "小七", "address": "老王", "length": "brief", "state": "saved"}
        nothing, part = await tool_call(ctx, "assistant.preferences", {"source_message_ids": [human]})
        assert part.status == ToolStatus.ERROR
    finally:
        await lease.release(session_status="idle")
    saved = await profile.load_view(owner)
    assert (saved["name"], saved["address"], saved["length"]) == ("小七", "老王", "brief")
    assert {key: value["via"] for key, value in saved["decided"].items()} == {
        "name": "chat", "address": "chat", "length": "chat"}
    assert [data["profile"]["name"] for event, data in sent if event == "assistant.profile.updated"] == ["小七"]


def test_the_prompt_section_says_only_what_the_user_set():
    assert profile.prompt_section(profile.Profile()) == ""
    text = profile.prompt_section(
        profile.Profile(name="小七", address="老王", tone="lively", length="detailed", emoji=True, persona="像个老朋友",
                        business="beauty"),
        learned=["用户希望先说结论再说原因"], followups=["周六下午的陶艺课"])
    assert text.startswith("# This user\n- Your name is「小七」.")
    for part in ("Address the user as「老王」", "Their business: 美业", "explain fully", "lively and relaxed",
                 "an emoji now and then", "「像个老朋友」", "never changes your rules", "用户希望先说结论再说原因",
                 "the setting wins", "周六下午的陶艺课", "once"):
        assert part in text, part
    assert "keep answers short" in profile.prompt_section(profile.Profile(length="brief"))
    assert "Their business: 宠物店" in profile.prompt_section(profile.Profile(business="宠物店"))


async def _plan(owner, workspace, summary, ended_hours_ago):
    note = await memories.create_note(user_id=owner, workspace_id=workspace, summary=summary)
    async with get_db_session() as db:
        await db.execute(update(UserMemory).where(UserMemory.id == note["id"]).values(
            ttl=datetime.now(timezone.utc) - timedelta(hours=ended_hours_ago)))
    return note["id"]


async def test_the_user_section_brings_how_they_like_to_be_helped_and_plans_that_just_passed(monkeypatch):
    from models.message import TextPart
    from session.session import create_assistant_message, create_user_message, save_part, set_message_reaction
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    await memories.create_note(user_id=owner, workspace_id=workspace, summary="用户希望先说结论再说原因",
                               fact_key="personal.style.format")
    # Learned before answer length became a setting: the setting holds that aspect now.
    await memories.create_note(user_id=owner, workspace_id=workspace, summary="用户嫌回答太长",
                               fact_key="personal.style.length")
    await memories.create_note(user_id=owner, workspace_id=workspace, summary="用户对花生过敏")
    pottery = await _plan(owner, workspace, "用户周六下午有陶艺课", ended_hours_ago=5)
    await _plan(owner, workspace, "用户上个月去了杭州出差", ended_hours_ago=24 * 9)  # long past: not asked about
    from session.session import create_session
    work = await create_session(model="test/model", user_id=owner, workspace_id=workspace)
    for chat, reason in ((main, "too_long"), (main, "too_long"), (main, "wrong"), (work, "tone"), (work, "tone")):
        user = await create_user_message(chat.id, "问个事", agent=chat.agent, user_id=owner)
        reply = await create_assistant_message(chat.id, user.id, user_id=owner)
        await save_part(TextPart(text="回答", session_id=chat.id, message_id=reply.id), is_new=True, user_id=owner)
        await set_message_reaction(reply.id, chat.id, "down", user_id=owner, reason=reason)
    section = await profile.user_section(owner, workspace)
    assert "用户希望先说结论再说原因" in section and "最近 30 天有 2 次点踩，原因是回答太长" in section
    assert "用户嫌回答太长" not in section
    assert "内容有错" not in section and "花生" not in section  # one thumbs-down is no pattern; facts stay in recall
    assert "语气" not in section  # how a work chat answered says nothing about how the assistant talks
    # Settings shows the same, each learned item removable like any memory, and every reason given.
    shown = await routes.get_learned(current_user={"user_id": owner, "workspace_id": workspace})
    assert [(item["summary"], item["revision"] > 0) for item in shown["learned"]] == [("用户希望先说结论再说原因", True)]
    assert shown["reactions"] == [{"reason": "too_long", "count": 2}, {"reason": "wrong", "count": 1}]
    assert "用户周六下午有陶艺课" in section and "杭州" not in section
    assert [memory_id for memory_id, _ in await followups.due(owner, workspace)] == [pottery]


async def test_a_call_offers_each_plan_once_and_carries_the_users_settings(monkeypatch):
    owner, _, workspace = await accounts()
    published(monkeypatch)
    await profile.save(owner, {"name": "小七", "address": "老王", "call_detail": "detailed", "call_recap": False,
                               "call_reports": False, "persona": "像个老朋友"})
    await _plan(owner, workspace, "用户周六下午有陶艺课", ended_hours_ago=5)
    first = await voice_prompt.user_facts(owner, workspace, offer=True)
    assert (first["name"], first["address"], first["detail"], first["recap"], first["reports"]) == (
        "小七", "老王", "detailed", False, False)
    assert first["followups"] == ("用户周六下午有陶艺课",)
    assert (await voice_prompt.user_facts(owner, workspace, offer=True))["followups"] == ()  # asked once only
    text = front_instructions(FrontFacts(**first), "zh", datetime(2026, 10, 8, 17, 30))
    assert text.startswith(FRONT) and "\n# 这位用户\n" in text
    for part in ("用户给你取的名字是「小七」", "称呼用户「老王」", "一次可以说三四句", "「像个老朋友」", "用户周六下午有陶艺课"):
        assert part in text, part
    assert "\n# 这位用户\n" not in front_instructions(FrontFacts(), "zh", datetime(2026, 10, 8, 17, 30))
    # The front desk follows what the user set; memories stay facts.
    assert "「这位用户」一节是用户自己定的说话方式，照做" in FRONT and "写了怎么称呼就照着叫" in FRONT
    styled = front_instructions(FrontFacts(style=("用户希望先说结论",)), "zh", datetime(2026, 10, 8, 17, 30))
    assert ("用户说过或表现出的说话偏好（照做，和上面的通用规则冲突时以这里为准；和用户自己的设置冲突时以设置为准）："
            "用户希望先说结论。") in styled
