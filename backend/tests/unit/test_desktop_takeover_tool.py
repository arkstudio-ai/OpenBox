"""desktop_takeover: the card says where the page is, and the answer shapes the resume.

The tool exists so a captcha stops the agent cleanly instead of after a loop of
failed clicks. What matters is that the question carries a `detail` the
frontend can turn into a takeover card (with or without a desktop link,
depending on which browser the page is in), and that the three answers come
back as three distinct instructions for the model.
"""
import pytest

from tool.desktop_takeover import (
    ANSWER_ABANDON,
    ANSWER_DONE,
    ANSWER_SKIP,
    DesktopTakeoverArgs,
    desktop_takeover_tool,
    execute,
)
from tool.tool import ToolContext

USER = "01MREALUSER"


@pytest.fixture
def asked(monkeypatch):
    """Answer the question immediately with whatever the test puts in `reply`."""
    seen: dict = {"reply": [[ANSWER_DONE]]}

    async def fake_ask(session_id, questions, tool=None, user_id="default"):
        seen["session_id"] = session_id
        seen["user_id"] = user_id
        seen["tool"] = tool
        seen["question"] = questions[0]
        return seen["reply"]

    monkeypatch.setattr("question.question.ask", fake_ask)
    return seen


def _relay(mode: str | None):
    async def fake_status(client):
        return {"relay": {"mode": mode}} if mode else {"chrome": {"pid": 1}}
    return fake_status


def _ctx(sandbox=object()) -> ToolContext:
    return ToolContext(session_id="s1", user_id=USER, message_id="m1", part_id="p1", sandbox=sandbox)


def test_tool_is_registered_for_the_build_agent_and_the_browser_pack():
    from agent.agent import AGENTS
    from agent.tool_exposure import INTENT_PACKS
    from tool.registry import get_tool, register_builtin_tools

    register_builtin_tools()
    assert get_tool("desktop_takeover") is not None
    assert desktop_takeover_tool.parallel_safe is False
    assert desktop_takeover_tool.sandbox_required is False
    assert "desktop_takeover" in AGENTS["build"].tools
    assert any(
        rule["permission"] == "desktop_takeover" and rule["action"] == "allow"
        for rule in AGENTS["build"].permission
    )
    assert "desktop_takeover" in INTENT_PACKS["browser"]


async def test_local_browser_card_points_at_the_cloud_desktop(asked, monkeypatch):
    monkeypatch.setattr("sandbox.browser.browser_status", _relay("local"))
    result = await execute(
        DesktopTakeoverArgs(
            reason="captcha_slider",
            url="https://login.taobao.com/member/login.jhtml?x=1",
            page="login",
            instructions="拖动滑块完成验证。",
        ),
        _ctx(),
    )
    q = asked["question"]
    assert asked["user_id"] == USER
    assert asked["tool"] == {"messageID": "m1", "callID": "p1"}
    assert q.header == "需要你接管"
    assert q.detail["kind"] == "desktop_takeover"
    assert q.detail["browser"] == "local"
    assert q.detail["host"] == "login.taobao.com"
    assert q.detail["page"] == "login"
    assert q.detail["reason"] == "captcha_slider"
    assert [o.label for o in q.options] == [ANSWER_DONE, ANSWER_SKIP, ANSWER_ABANDON]
    assert "云桌面" in q.question and "login.taobao.com" in q.question
    # The model is told to re-look at the very page it was driving.
    assert "client.page('login')" in result.output
    assert result.metadata["takeover"]["browser"] == "local"
    assert result.metadata["answers"] == [[ANSWER_DONE]]


async def test_extension_browser_card_has_no_desktop_link(asked, monkeypatch):
    monkeypatch.setattr("sandbox.browser.browser_status", _relay("extension"))
    await execute(DesktopTakeoverArgs(reason="sms_code"), _ctx())
    q = asked["question"]
    assert q.detail["browser"] == "extension"
    assert "自己的浏览器" in q.question


async def test_no_sandbox_is_treated_as_the_users_own_browser(asked):
    await execute(DesktopTakeoverArgs(reason="risk_control"), _ctx(sandbox=None))
    assert asked["question"].detail["browser"] == "extension"


async def test_relay_down_means_the_desktops_own_browser(asked, monkeypatch):
    monkeypatch.setattr("sandbox.browser.browser_status", _relay(None))
    await execute(DesktopTakeoverArgs(reason="other"), _ctx())
    assert asked["question"].detail["browser"] == "local"


@pytest.mark.parametrize(
    ("reply", "title_word", "output_word"),
    [
        ([[ANSWER_SKIP]], "跳过", "skip"),
        ([[ANSWER_ABANDON]], "放弃", "abandon"),
        ([["验证过了，但页面跳回首页了"]], "完成", "re-open"),
    ],
)
async def test_each_answer_becomes_a_distinct_instruction(asked, monkeypatch, reply, title_word, output_word):
    monkeypatch.setattr("sandbox.browser.browser_status", _relay("local"))
    asked["reply"] = reply
    result = await execute(DesktopTakeoverArgs(reason="captcha_click", page="shop"), _ctx())
    assert title_word in result.title
    assert output_word in result.output
    if output_word == "re-open":
        # A free-text answer is still "done": it is quoted back so the model
        # can act on what the user actually said.
        assert reply[0][0] in result.output
