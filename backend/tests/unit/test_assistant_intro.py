"""The first meeting with the assistant, and what counts as set (assistant/profile.py).

Set or not is a decision, not a value: choosing the default is a decision, skipping a question is
not. The meeting asks only what is still undecided, ends in done, "以后再说" (dismissed) or going
straight to work (bypassed), and the first call asks how to address the user once when nothing else
has.
"""
from datetime import datetime

import pytest
from fastapi import HTTPException

from api import assistant as routes
from assistant import profile
from db.repository.preference_repo import PgPreferenceRepo
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_profile import published
from voice import prompt as voice_prompt
from voice.prompt import FrontFacts, front_instructions


async def owner_and_actor():
    owner, _, workspace = await accounts()
    return owner, workspace, {"user_id": owner, "workspace_id": workspace}


def vias(view: dict) -> dict:
    return {key: value["via"] for key, value in view["decided"].items()}


async def test_choosing_the_default_is_a_decision_and_a_profile_from_before_keeps_its_choices(monkeypatch):
    owner, _, actor = await owner_and_actor()
    published(monkeypatch)
    # Saved before decisions were recorded: what differs from the default was the user's choice.
    await PgPreferenceRepo().upsert(owner, extra={"assistant_profile": {"name": "Mary", "length": "balanced"}})
    before = await routes.get_profile(current_user=actor)
    assert vias(before) == {"name": "settings"} and before["intro"] == {
        "version": 1, "status": "new", "steps": {}, "nudged": False, "call_asked": False}
    # Clearing the name to the default is a decision too; nothing else becomes decided.
    cleared = await routes.set_profile(routes.ProfileBody(name=""), current_user=actor)
    assert cleared["name"] == "" and vias(cleared) == {"name": "settings"}
    assert profile.intro_pending(cleared["decided"], cleared["intro"]) == ["address", "length", "business"]


async def test_a_meeting_asks_what_is_undecided_records_answers_and_skips_and_ends_done(monkeypatch):
    owner, _, actor = await owner_and_actor()
    sent = published(monkeypatch)
    await routes.set_profile(routes.ProfileBody(length="detailed"), current_user=actor)  # decided before meeting
    view = await routes.get_profile(current_user=actor)
    assert profile.intro_pending(view["decided"], view["intro"]) == ["address", "name", "business"]

    answer = routes.IntroBody(event="answer", step="address", value=" 老王 ")
    view = await routes.intro(answer, current_user=actor)
    assert (view["address"], view["intro"]["status"], view["intro"]["steps"]) == ("老王", "started",
                                                                                   {"address": "answered"})
    assert vias(view) == {"length": "settings", "address": "intro"}
    view = await routes.intro(routes.IntroBody(event="skip", step="name"), current_user=actor)
    assert "name" not in view["decided"] and view["intro"]["steps"]["name"] == "skipped"  # skipped: not decided
    view = await routes.intro(routes.IntroBody(event="answer", step="business", value="beauty"), current_user=actor)
    assert view["business"] == "beauty" and view["intro"]["status"] == "done"
    assert profile.intro_pending(view["decided"], view["intro"]) == []
    assert sent[-1][1] == {"userId": owner, "profile": view}  # every open app hears of each step

    # Done stays done; answering what was skipped, from a later entry, makes it decided.
    view = await routes.intro(routes.IntroBody(event="dismiss"), current_user=actor)
    assert view["intro"]["status"] == "done"
    view = await routes.intro(routes.IntroBody(event="answer", step="name", value=""), current_user=actor)
    assert vias(view)["name"] == "intro" and view["name"] == ""  # "就叫个人助理" is a choice


async def test_later_and_going_straight_to_work_end_a_meeting_once_and_the_reminder_is_shown_once(monkeypatch):
    owner, _, actor = await owner_and_actor()
    published(monkeypatch)
    view = await routes.intro(routes.IntroBody(event="bypass"), current_user=actor)
    assert view["intro"]["status"] == "bypassed"
    view = await routes.intro(routes.IntroBody(event="nudged"), current_user=actor)
    assert view["intro"]["nudged"] is True and view["intro"]["status"] == "bypassed"
    view = await routes.intro(routes.IntroBody(event="dismiss"), current_user=actor)  # "不用了" on the reminder
    assert view["intro"]["status"] == "dismissed"
    view = await routes.intro(routes.IntroBody(event="bypass"), current_user=actor)
    assert view["intro"]["status"] == "dismissed"  # only a meeting not yet ended can be bypassed

    other, _, other_actor = await owner_and_actor()
    view = await routes.intro(routes.IntroBody(event="dismiss"), current_user=other_actor)
    assert view["intro"]["status"] == "dismissed"


@pytest.mark.parametrize("body", [
    {"event": "answer", "step": "length", "value": "chatty"},
    {"event": "answer", "step": "address"},
    {"event": "answer", "step": "name", "value": "x" * 21},
    {"event": "answer", "value": "老王"},
    {"event": "skip"},
    {"event": "dismiss", "step": "name"},
])
async def test_what_a_meeting_cannot_record(monkeypatch, body):
    owner, _, actor = await owner_and_actor()
    sent = published(monkeypatch)
    with pytest.raises(HTTPException) as refused:
        await routes.intro(routes.IntroBody(**body), current_user=actor)
    assert refused.value.status_code == 422 and sent == []
    assert (await routes.get_profile(current_user=actor))["intro"]["status"] == "new"


async def test_the_first_call_asks_how_to_address_the_user_once_when_nothing_else_has(monkeypatch):
    owner, workspace, actor = await owner_and_actor()
    published(monkeypatch)
    first = await voice_prompt.user_facts(owner, workspace, offer=True)
    assert first["ask_address"] is True
    assert (await routes.get_profile(current_user=actor))["intro"]["call_asked"] is True
    assert (await voice_prompt.user_facts(owner, workspace, offer=True))["ask_address"] is False  # asked once
    # During the call it keeps asking until the answer is saved, then stops.
    assert (await voice_prompt.user_facts(owner, workspace, asking=True))["ask_address"] is True
    await profile.save(owner, {"address": "老王"}, via="chat")
    assert (await voice_prompt.user_facts(owner, workspace, asking=True))["ask_address"] is False

    text = front_instructions(FrontFacts(ask_address=True, business="food"), "zh", datetime(2026, 10, 8, 17, 30))
    assert "问一次怎么称呼" in text and "assistant_ask" in text and "用户做的生意：餐饮" in text
    named = front_instructions(FrontFacts(address="老王", ask_address=True), "zh", datetime(2026, 10, 8, 17, 30))
    assert "问一次怎么称呼" not in named and "称呼用户「老王」" in named


@pytest.mark.parametrize("decided, intro, asks", [
    ({}, {"status": "new", "steps": {}, "call_asked": False}, True),
    ({}, {"status": "bypassed", "steps": {}, "call_asked": False}, True),        # busy then: ask on the phone
    ({}, {"status": "started", "steps": {"address": "skipped"}, "call_asked": False}, False),  # they passed
    ({}, {"status": "dismissed", "steps": {}, "call_asked": False}, False),      # "以后再说"
    ({"address": {"at": None, "via": "settings"}}, {"status": "new", "steps": {}, "call_asked": False}, False),
    ({}, {"status": "new", "steps": {}, "call_asked": True}, False),
])
def test_when_a_call_asks_how_to_address_the_user(decided, intro, asks):
    assert profile.call_should_ask_address(decided, intro) is asks


async def test_the_assistants_own_tool_never_writes_the_description_or_call_habits():
    from assistant.policy import AssistantError
    owner, workspace, _ = await owner_and_actor()
    for fields in ({"persona": "像个老朋友"}, {"call_recap": False}, {}):
        with pytest.raises(AssistantError) as refused:
            await profile.set_preferences(user_id=owner, workspace_id=workspace, main_id="unused", **fields)
        assert refused.value.code == "ASSISTANT_PROFILE_INVALID"
