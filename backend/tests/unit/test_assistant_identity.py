"""The name the user gives their assistant (assistant/identity.py): a preference, in both prompts, set by
Settings or by the assistant itself on the user's words."""
from datetime import datetime

import pytest
from fastapi import HTTPException

from api import assistant as routes
from assistant import identity
from assistant.policy import AssistantError
from assistant.reporting import ASSISTANT_TOOLS
from assistant.service import ensure_main_session
from db.repository.preference_repo import PgPreferenceRepo
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import quiet_runtime  # noqa: F401
from tool.assistant_tools import assistant_tools
from voice.prompt import FRONT, FrontFacts, front_instructions


@pytest.mark.parametrize("given, stored", [
    ("  Mary ", "Mary"), ("“小七”", "小七"), ("「Mary」", "Mary"), ("Mary\nSmith", "Mary Smith"),
    ("", ""), ("   ", ""), (None, ""), ("x" * 20, "x" * 20),
])
def test_a_name_is_one_trimmed_unquoted_line_and_empty_clears_it(given, stored):
    assert identity.clean_name(given) == stored


@pytest.mark.parametrize("bad", ["x" * 21, "<b>Mary</b>", "{Mary}", "[x]", "a\\b", "Mary\x07"])
def test_what_is_not_a_name(bad):
    with pytest.raises(ValueError):
        identity.clean_name(bad)


async def test_the_name_is_a_preference_beside_the_others_read_by_settings_and_the_app():
    owner, _, workspace = await accounts()
    actor = {"user_id": owner, "workspace_id": workspace}
    await PgPreferenceRepo().upsert(owner, extra={"voice": "Tina"})
    assert await identity.assistant_name(owner) == "" and await identity.assistant_name("nobody") == ""
    assert await routes.set_name(routes.NameBody(name=" Mary "), current_user=actor) == {"name": "Mary"}
    assert await routes.get_name(current_user=actor) == {"name": "Mary"}
    assert (await PgPreferenceRepo().get(owner))["extra"] == {"voice": "Tina", "assistant_name": "Mary"}
    with pytest.raises(HTTPException) as refused:
        await routes.set_name(routes.NameBody(name="x" * 21), current_user=actor)
    assert refused.value.status_code == 422 and refused.value.detail["code"] == "ASSISTANT_NAME_INVALID"
    assert await identity.assistant_name(owner) == "Mary"  # a refused name changes nothing
    assert await routes.set_name(routes.NameBody(name=""), current_user=actor) == {"name": ""}
    assert await identity.assistant_name(owner) == ""


def test_both_prompts_carry_the_name_only_when_there_is_one():
    assert identity.prompt_section("") == ""
    assert identity.prompt_section("Mary").startswith("# Your name\nThe user calls you「Mary」.")
    now = datetime(2026, 10, 8, 17, 30)
    bare = front_instructions(FrontFacts(), "zh", now)
    assert "取的名字" not in bare
    named = front_instructions(FrontFacts(name="Mary"), "zh", now)
    assert named.startswith(FRONT) and ("用户给你取的名字是「Mary」：自我介绍、用户问你是谁或怎么称呼你时，就用这个名字；"
                                        "记得的事里如果有别的名字，以这个为准。") in named
    # A memory from an earlier name ("用户给助理取名为小七") never outranks the current one.
    assert identity.prompt_section("Mary").endswith("a memory that names you otherwise is out of date.")
    assert named.index("现在是") < named.index("取的名字")


async def test_a_saved_name_is_pushed_to_every_open_client(monkeypatch):
    owner, _, workspace = await accounts()
    published = []
    monkeypatch.setattr("bus.bus.publish", lambda event, data=None: published.append((event, data)))
    await identity.save_name(owner, "  小七 ")
    await identity.save_name(owner, "")
    assert published == [("assistant.renamed", {"userId": owner, "name": "小七"}),
                         ("assistant.renamed", {"userId": owner, "name": ""})]
    with pytest.raises(ValueError):
        await identity.save_name(owner, "x" * 21)
    assert len(published) == 2  # a refused name is never announced


async def test_the_rename_tool_runs_from_a_real_tool_call(monkeypatch):
    """Measured: the first live call failed (its action was missing from the tool-call check) and the
    assistant fell back to a memory, so the app kept showing the old name."""
    from assistant.commands import ToolSource
    from models.message import ToolStatus
    from tests.unit.test_assistant_sessions_v2 import main_turn, tool_call
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    renamed = []
    publish = __import__("bus").bus.publish
    monkeypatch.setattr("bus.bus.publish", lambda event, data=None: (
        renamed.append(data["name"]) if event == "assistant.renamed" else None, publish(event, data))[1])
    ctx, lease, human = await main_turn(owner, workspace, main, "以后叫你小七吧")
    try:
        _, other_tool = await tool_call(ctx, "tasks.list", {})
        with pytest.raises(AssistantError) as wrong_tool:  # another tool's call cannot rename
            await identity.rename(user_id=owner, workspace_id=workspace, main_id=main.id, name="Forged",
                                  source=ToolSource(other_tool.id, ctx.run_id, ctx.run_generation, (human,)))
        assert wrong_tool.value.code == "ASSISTANT_CALL_UNVERIFIED"
        receipt, part = await tool_call(ctx, "assistant.rename", {"name": "“小七”", "source_message_ids": [human]})
        assert part.status == ToolStatus.COMPLETED and receipt == {"name": "小七", "state": "renamed"}
    finally:
        await lease.release(session_status="idle")
    assert await identity.assistant_name(owner) == "小七" and renamed == ["小七"]


async def test_the_assistant_renames_itself_on_the_users_words_and_never_for_another_member():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id)
    assert "assistant.rename" in ASSISTANT_TOOLS and "assistant.rename" in {tool.id for tool in assistant_tools}
    assert await identity.rename(**scope, name="“Mary”") == {"name": "Mary", "state": "renamed"}
    assert await identity.assistant_name(owner) == "Mary"
    with pytest.raises(ValueError):
        await identity.rename(**scope, name="x" * 21)
    with pytest.raises(AssistantError):
        await identity.rename(user_id=other, workspace_id=workspace, main_id=main.id, name="Hijacked")
    assert await identity.assistant_name(owner) == "Mary"
    assert await identity.rename(**scope, name="") == {"name": "", "state": "default"}
    assert await identity.assistant_name(owner) == ""
