"""Reply events retain `id` while exposing the clients' `request_id` alias."""

import pytest

from bus.events import PERMISSION_REPLIED, QUESTION_REJECTED, QUESTION_REPLIED
from permission import permission as permission_mod
from question import question as question_mod


@pytest.fixture
def published(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(permission_mod.bus, "_redis_client", None)
    monkeypatch.setattr(
        permission_mod.bus,
        "publish",
        lambda event_type, data: events.append((event_type, data)),
    )
    return events


def _permission(request_id: str = "perm-1") -> permission_mod.PendingPermission:
    request = permission_mod.PermissionRequest(
        id=request_id,
        user_id="user-1",
        session_id="session-1",
        tool="bash",
    )
    return permission_mod.PendingPermission(request=request)


def _question(request_id: str = "question-1") -> question_mod.PendingQuestion:
    request = question_mod.QuestionRequest(
        id=request_id,
        user_id="user-1",
        session_id="session-1",
        questions=[question_mod.Question(question="Continue?")],
    )
    return question_mod.PendingQuestion(request=request)


def _assert_reply_event(events, event_type: str, request_id: str) -> None:
    assert events[-1][0] == event_type
    assert events[-1][1]["id"] == request_id
    assert events[-1][1]["request_id"] == request_id


async def test_permission_reply_publishes_both_identifiers(published):
    permission_mod._pending["perm-1"] = _permission()
    try:
        await permission_mod.reply("perm-1", "once", user_id="user-1")
        _assert_reply_event(published, PERMISSION_REPLIED, "perm-1")
    finally:
        permission_mod._pending.clear()


async def test_question_reply_publishes_both_identifiers(published):
    question_mod._pending["question-1"] = _question()
    try:
        await question_mod.reply("question-1", [["Yes"]], user_id="user-1")
        _assert_reply_event(published, QUESTION_REPLIED, "question-1")
    finally:
        question_mod._pending.clear()


async def test_question_rejection_publishes_both_identifiers(published):
    question_mod._pending["question-1"] = _question()
    try:
        await question_mod.reject("question-1", user_id="user-1")
        _assert_reply_event(published, QUESTION_REJECTED, "question-1")
    finally:
        question_mod._pending.clear()
