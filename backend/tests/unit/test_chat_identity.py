"""Current SQL settings beat recalled greeting identities across chat surfaces."""
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from agent.agent import AgentDef, get_agent
from agent.loop import _build_system_prompt
from api import assistant as routes
from assistant import profile
from core.config import OpenBoxConfig
from db.base import get_db_session
from db.models.memory import UserMemory
from memory import service as memories
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from voice.prompt import FrontFacts, front_instructions, user_facts


@pytest.fixture(autouse=True)
def no_external_context(monkeypatch):
    monkeypatch.setattr("core.config.get_config", lambda: OpenBoxConfig())
    monkeypatch.setattr("session.instruction.instruction_system_with_config", AsyncMock(return_value=[]))
    monkeypatch.setattr("bus.bus.publish", lambda *args, **kwargs: None)


async def normal_prompt(owner, workspace, *, agent="build", kind="normal", isolated=False):
    return "\n".join(await _build_system_prompt(
        get_agent(agent), "test/model", user_id=owner, workspace_id=workspace,
        session_kind=kind, memory_isolated=isolated, include_user_memory=False,
    ))


async def all_prompts(owner, workspace):
    ordinary = await normal_prompt(owner, workspace)
    personal = "\n".join(await _build_system_prompt(
        get_agent("assistant"), "test/model", user_id=owner, workspace_id=workspace,
        session_kind="assistant", memory_isolated=True, include_user_memory=False,
    ))
    facts = FrontFacts(**await user_facts(owner, workspace))
    spoken = front_instructions(facts, "zh", datetime(2026, 10, 20, 15, 14))
    return ordinary, personal, spoken


@pytest.mark.parametrize("agent", ["build", "plan"])
async def test_normal_chat_reads_settings_even_without_memory_recall(monkeypatch, agent):
    owner, _, workspace = await accounts()
    await routes.set_profile(routes.ProfileBody(name="小七", address="老王"),
                             current_user={"user_id": owner, "workspace_id": workspace})
    # A task reply must not load the private assistant's style/follow-up history.
    private_context = AsyncMock(side_effect=AssertionError("private assistant context"))
    monkeypatch.setattr("assistant.profile.user_section", private_context)
    text = await normal_prompt(owner, workspace, agent=agent)
    assert "Your name is「小七」" in text and "Address the user as「老王」" in text
    assert "Do not search memory for an identity already supplied here" in text
    private_context.assert_not_awaited()


async def test_rename_reaches_all_surfaces_without_rewriting_the_saved_template():
    owner, _, workspace = await accounts()
    template = "首次打招呼用这个：老板您好，我是您的专属运营助理 XX，帮您处理日常运营。"
    note = await memories.create_note(user_id=owner, workspace_id=workspace, summary=template,
                                     fact_key="project.workflow.initial_greeting")
    actor = {"user_id": owner, "workspace_id": workspace}
    await routes.set_profile(routes.ProfileBody(name="旧助理甲", address="旧称呼甲"), current_user=actor)
    for text in await all_prompts(owner, workspace):
        assert "旧助理甲" in text and "旧称呼甲" in text
    await routes.set_profile(routes.ProfileBody(name="小八", address="王老师"), current_user=actor)
    for text in await all_prompts(owner, workspace):
        assert "小八" in text and "王老师" in text
        assert "旧助理甲" not in text and "旧称呼甲" not in text
        assert "XX" in text  # the rule explains placeholders; it is not a blanket output replacement
    async with get_db_session() as db:
        row = await db.get(UserMemory, note["id"])
        assert row.status == "ACTIVE" and row.value["summary"] == template
        assert row.revision == note["revision"]


async def test_clearing_identity_is_a_setting_and_never_restores_an_old_name():
    owner, _, workspace = await accounts()
    await profile.save(owner, {"name": "旧助理甲", "address": "旧称呼甲"})
    await profile.save(owner, {"name": "", "address": ""})
    ordinary, personal, spoken = await all_prompts(owner, workspace)
    for text in (ordinary, personal):
        assert "No custom assistant name is set" in text and "Address the user as「老板」" in text
    assert "当前没有自定义助手名字" in spoken and "称呼用户「老板」" in spoken
    assert "找个自然的时候问一次怎么称呼" not in spoken
    assert all("旧助理甲" not in text and "旧称呼甲" not in text for text in (ordinary, personal, spoken))


async def test_unset_address_uses_the_product_default_without_saving_a_custom_name():
    owner, _, workspace = await accounts()
    ordinary, personal, spoken = await all_prompts(owner, workspace)
    assert all("Address the user as「老板」" in text for text in (ordinary, personal))
    assert "称呼用户「老板」" in spoken
    current = await profile.load_view(owner)
    assert current["address"] == "" and "address" not in current["decided"]


async def test_quotes_and_third_party_copy_keep_their_identity_and_literal_names_are_allowed():
    owner, _, workspace = await accounts()
    await profile.save(owner, {"name": "XX", "address": "xxx"})
    ordinary, personal, spoken = await all_prompts(owner, workspace)
    for text in (ordinary, personal):
        assert "Your name is「XX」" in text and "Address the user as「xxx」" in text
        assert "preserve the original and third-party names" in text
        assert "unless that is the user's actual current setting" in text
    assert "保留原文及第三方的名字、称呼和占位符" in spoken
    assert "若用户当前设置本身就是这些字符，则照其设置" in spoken


@pytest.mark.parametrize("kind,isolated,agent", [
    ("cron", False, "build"), ("normal", True, "build"), ("normal", False, "explore"),
])
async def test_non_personal_execution_does_not_gain_account_settings(monkeypatch, kind, isolated, agent):
    owner, _, workspace = await accounts()
    forbidden = AsyncMock(side_effect=AssertionError("account settings leaked"))
    monkeypatch.setattr("assistant.profile.settings_section", forbidden)
    await normal_prompt(owner, workspace, agent=agent, kind=kind, isolated=isolated)
    forbidden.assert_not_awaited()


async def test_frozen_subagent_keeps_its_accepted_identity(monkeypatch):
    owner, _, workspace = await accounts()
    frozen = AgentDef(name="build", description="reviewer", prompt="You are the accepted reviewer.")
    monkeypatch.setattr("agent.subagent_authority.current_frozen_subagent_agent", lambda _: frozen)
    forbidden = AsyncMock(side_effect=AssertionError("account settings changed a frozen persona"))
    monkeypatch.setattr("assistant.profile.settings_section", forbidden)
    assert "You are the accepted reviewer." in await normal_prompt(owner, workspace)
    forbidden.assert_not_awaited()


async def test_settings_are_read_for_the_current_account_only():
    owner, other, workspace = await accounts()
    await profile.save(owner, {"name": "小七", "address": "老王"})
    await profile.save(other, {"name": "小八", "address": "王老师"})
    text = await normal_prompt(other, workspace)
    assert "小八" in text and "王老师" in text and "小七" not in text and "老王" not in text


@pytest.mark.parametrize("address,expected", [("王老师", "王老师"), ("", "老板")])
async def test_personal_projection_binds_current_identity_beside_the_human_input(address, expected):
    from assistant.service import ensure_main_session
    from tests.unit.test_assistant_projection_voice import claimed, projected
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    old_words = "之前的话术原文是：旧称呼你好，我是旧助理。"
    ctx, lease, _, user = await claimed(owner, workspace, main, client_id="identity-current",
                                       text=old_words, voice=False)
    try:
        await profile.save(owner, {"name": "新助理", "address": address})
        messages = await projected(ctx)
        position = next(i for i, message in enumerate(messages) if message.id == user.id)
        block = messages[position - 1]
        assert block.id == "assistant:current-identity"
        assert '"assistant_name": "新助理"' in block.parts[0]["text"]
        assert f'"user_address": "{expected}"' in block.parts[0]["text"]
        assert block.parts[0]["synthetic"] and block.parts[0]["origin"] == "system_recovery"
        assert old_words in messages[position].parts[0]["text"]  # quotation remains untouched
        assert all(message.id != block.id for message in await projected(ctx, for_compaction=True))
    finally:
        await lease.release(session_status="idle")
