"""The personal assistant knows which interface the user is in and what the Light home offers
(docs/LIGHT_MODE_IMPL_PLAN.md §11): the prompt section, the four Light work types, the light.entries tool."""
import pytest
from sqlalchemy import func, select

from assistant.commands import VIDEO_WORKFLOW_GUIDANCE, VIDEO_WORKFLOWS
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from light import mode as light_mode
from light.entries import light_entries
from light.prompt import assistant_section, entries_directory
from light.work_types import ENTRY_FOR_WORK_TYPE, WORKFLOWS
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import MAIN_MESSAGES, assistant, main_turn, tool_call

CATALOGUE = {"chat": [{"id": "test/model", "name": "Task AI", "vision": True}],
    "video": [{"id": "MiniMax-H3-Max-Turbo", "name": "MiniMax Turbo", "resolutions": ["768p", "480p"]},
              {"id": "wan3.0", "name": "Wan 3.0", "resolutions": ["720p", "1080p"]}],
    "video_tiers": [
        {"id": "low", "name": "省钱档", "model": "MiniMax-H3-Max-Turbo", "resolution": "768p",
         "resolutions": ["480p", "768p"], "credits_per_second": {"768p": "0.3"}, "description": ""},
        {"id": "high", "name": "质量档", "model": "wan3.0", "resolution": "1080p",
         "resolutions": ["720p", "1080p"], "credits_per_second": {"1080p": "2"}, "description": ""}]}


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "light-awareness-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    monkeypatch.setattr("assistant.model_choices.catalogue", lambda: CATALOGUE)
    MAIN_MESSAGES.clear()


@pytest.fixture
def free_workspace(monkeypatch):
    async def facts(workspace_id):
        return {"plan_id": "free", "paid_plan": False, "desktop": False, "bound": set()}
    monkeypatch.setattr(light_mode, "_workspace_facts", facts)


# --- 1. the prompt section ------------------------------------------------------------------------------

async def test_pro_mode_gets_only_the_mode_line(free_workspace):
    owner, _, workspace, _ = await assistant()
    section = await assistant_section(owner, workspace)
    assert section == "界面模式：专业。"
    assert "首页" not in section and "light.entries" not in section


async def test_light_mode_lists_the_home_directory_with_availability_and_links(free_workspace):
    owner, _, workspace, _ = await assistant()
    await light_mode.set_ui_mode(owner, "light")
    section = await assistant_section(owner, workspace)
    assert section.startswith("界面模式：Light（功能引导）")
    assert "- 做一条视频｜店内环境 / 餐品展示 / 探店｜可用｜/light/topic/video" in section
    assert "- 做菜单｜拍旧菜单，出新菜单｜可用｜/light/topic/menu" in section
    assert "- 发到抖音｜选成片、写标题、定时间｜需要云电脑｜/light/topic/douyin_publish" in section
    assert "- 你拍我剪｜自己拍的素材，剪成一条｜可用｜/light/topic/footage_edit" in section
    # the three rules and the tool to re-check the directory
    assert "默认引导老板去首页对应入口" in section and "「你直接做」" in section
    assert "light_video / menu / poster / footage_edit" in section
    assert "我的 → 云电脑" in section and "不要派帮手去撞" in section
    assert "不出现模型名" in section and "light.entries" in section
    # 2026-11-07: on the phone the owner cannot tap a link, and a redirect must not sound like a handover
    assert "电话里提的这几类活一律直接 tasks.submit 派活" in section
    assert "不能说「已交办」「正在处理」" in section and "用页面上选好的那款做" in section
    # never the internals the Light rules forbid
    for banned in ("MiniMax", "wan3", "low", "credits"):
        assert banned not in section


async def test_light_mode_section_degrades_without_a_workspace(free_workspace):
    owner, _, _, _ = await assistant()
    await light_mode.set_ui_mode(owner, "light")
    section = await assistant_section(owner, None)
    assert section.startswith("界面模式：Light") and "目录暂不可用" in section and "light.entries" in section


async def test_assistant_system_prompt_carries_the_section(free_workspace):
    """agent/loop.py::_build_system_prompt, assistant branch: [ASSISTANT_PROMPT, user_section?, light, date]."""
    from agent.agent import get_agent
    from agent.loop import _build_system_prompt
    owner, _, workspace, _ = await assistant()
    pro = await _build_system_prompt(get_agent("assistant"), "test/model", user_id=owner, workspace_id=workspace)
    assert "界面模式：专业。" in pro and pro[-1].startswith("Today's date:")
    await light_mode.set_ui_mode(owner, "light")
    light = await _build_system_prompt(get_agent("assistant"), "test/model", user_id=owner, workspace_id=workspace)
    assert any("首页目录" in part and "/light/topic/menu" in part for part in light)
    assert light[0] == pro[0] and light[-1].startswith("Today's date:")


async def test_section_failure_never_breaks_the_assistant_prompt(monkeypatch):
    from agent.agent import get_agent
    from agent.loop import _build_system_prompt
    owner, _, workspace, _ = await assistant()

    async def boom(*_):
        raise RuntimeError("db away")
    monkeypatch.setattr("light.prompt.assistant_section", boom)
    parts = await _build_system_prompt(get_agent("assistant"), "test/model", user_id=owner, workspace_id=workspace)
    assert parts[-1].startswith("Today's date:") and not any("界面模式" in part for part in parts)


# --- 2. the four Light work types ---------------------------------------------------------------------------

def test_light_work_types_match_the_home_entries():
    """Workflow names are the entries' skills, so the helper reads the same skill the topic page would."""
    config = light_entries()
    for work_type, entry_id in ENTRY_FOR_WORK_TYPE.items():
        entry = config.entry(entry_id)
        assert entry is not None and WORKFLOWS[work_type] == entry.skill
        assert WORKFLOWS[work_type] in VIDEO_WORKFLOWS
        guidance = VIDEO_WORKFLOW_GUIDANCE[WORKFLOWS[work_type]]
        assert f"`{entry.skill}` skill" in guidance and "do not load video-production" in guidance
        assert "do not use bash, file or desktop tools" in guidance
    assert set(WORKFLOWS) == {"light_video", "menu", "poster", "footage_edit"}
    assert set(VIDEO_WORKFLOW_GUIDANCE) >= set(WORKFLOWS.values()) | {"video", "video-production"}


@pytest.mark.parametrize("work_type", ["light_video", "menu", "poster", "footage_edit"])
async def test_light_work_type_submits_without_format_or_tier_cards(work_type):
    """Even with let_user_choose and a tier question in the arguments, a Light job shows no video card."""
    owner, _, workspace, main = await assistant()
    ctx, lease, human = await main_turn(owner, workspace, main, "你直接做吧，不用去首页")
    args = {"project_id": main.project_id, "title": f"Light {work_type}", "instructions": "按老板给的照片做",
            "source_message_ids": [human], "destination_confirmed": True, "work_type": work_type,
            "model": "test/model", "model_confirmed": True, "let_user_choose": True,
            "video_kind_question": "口播还是展示？", "video_model_question": "要哪个档？"}
    try:
        result, _ = await tool_call(ctx, "tasks.submit", args)
        assert "suspended" not in result and result.get("state") == "accepted", result
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(QuestionCheckpoint).where(
                QuestionCheckpoint.session_id == main.id)) == 0
            task = await db.get(AssistantTask, result["task_id"])
            execution = await db.get(Session, task.execution_session_id)
            item = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.session_id == execution.id))
        assert item.origin_ref["workflow"] == WORKFLOWS[work_type]
        assert item.prompt.startswith(VIDEO_WORKFLOW_GUIDANCE[WORKFLOWS[work_type]])
        assert "video-production" not in item.prompt.split("\n\n", 1)[0].replace("do not load video-production", "")
        if work_type == "light_video":
            # the home entry's default tier, without a card; the receipt says what was arranged
            assert execution.video_model == "MiniMax-H3-Max-Turbo" and execution.video_resolution == "768p"
            assert result["arranged"]["video_tier"] == "low"
        else:
            assert execution.video_model is None and execution.video_resolution is None
            assert "video_tier" not in (result.get("arranged") or {})
    finally:
        await lease.release(session_status="idle")


async def test_light_video_keeps_a_tier_the_user_already_confirmed():
    owner, _, workspace, main = await assistant()
    ctx, lease, human = await main_turn(owner, workspace, main, "你直接做，要质量档")
    args = {"project_id": main.project_id, "title": "Light 视频 质量档", "instructions": "做一条",
            "source_message_ids": [human], "destination_confirmed": True, "work_type": "light_video",
            "model": "test/model", "model_confirmed": True, "video_tier": "high", "video_tier_confirmed": True}
    try:
        result, _ = await tool_call(ctx, "tasks.submit", args)
        assert result.get("state") == "accepted", result
        async with get_db_session() as db:
            task = await db.get(AssistantTask, result["task_id"])
            execution = await db.get(Session, task.execution_session_id)
        assert execution.video_model == "wan3.0" and execution.video_resolution == "1080p"
        assert "video_tier" not in (result.get("arranged") or {})
    finally:
        await lease.release(session_status="idle")


# --- 3. the light.entries tool -------------------------------------------------------------------------------

async def test_light_entries_tool_reads_the_same_directory_as_the_prompt(free_workspace):
    owner, _, workspace, main = await assistant()
    await light_mode.set_ui_mode(owner, "light")
    ctx, lease, _ = await main_turn(owner, workspace, main, "首页都有什么？")
    try:
        value, part = await tool_call(ctx, "light.entries", {})
    finally:
        await lease.release(session_status="idle")
    assert value["ui_mode"] == "light" and value["plan_id"] == "free" and value["desktop"] is False
    rows = {row["id"]: row for row in value["entries"]}
    assert set(rows) == {entry.id for entry in light_entries().entries}
    assert rows["menu"] == {"id": "menu", "title": "做菜单", "subtitle": "拍旧菜单，出新菜单", "availability": "ok",
                            "availability_label": "可用", "url": "/light/topic/menu"}
    assert rows["review"]["availability"] == "needs_desktop" and rows["review"]["availability_label"] == "需要云电脑"
    # same source as the prompt section: every row of the tool is a line of the section
    section = await assistant_section(owner, workspace)
    for row in rows.values():
        assert f"- {row['title']}｜{row['subtitle']}｜{row['availability_label']}｜{row['url']}" in section
    assert (await entries_directory(owner, workspace))["entries"] == value["entries"]
    # read-only: no task, no card, nothing queued
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantTask).where(AssistantTask.user_id == owner)) == 0
        assert await db.scalar(select(func.count()).select_from(QuestionCheckpoint)) == 0


async def test_light_entries_names_the_binding_when_only_that_is_missing(monkeypatch):
    async def facts(workspace_id):
        return {"plan_id": "pro", "paid_plan": True, "desktop": True, "bound": set()}
    monkeypatch.setattr(light_mode, "_workspace_facts", facts)
    owner, _, workspace, _ = await assistant()
    rows = {row["id"]: row for row in (await entries_directory(owner, workspace))["entries"]}
    assert rows["review"]["availability"] == "needs_binding" and rows["review"]["availability_label"] == "需要绑定抖音"
    assert rows["douyin_publish"]["availability_label"] == "可用"
