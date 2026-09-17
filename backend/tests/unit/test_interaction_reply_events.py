"""Reply events retain `id` while exposing the clients' `request_id` alias."""

import pytest

from bus.events import PERMISSION_REPLIED, QUESTION_REJECTED, QUESTION_REPLIED
from permission import permission as permission_mod
from question import question as question_mod
from tests.unit.test_durable_questions import state  # noqa: F401


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


@pytest.mark.parametrize("decision", ["reply", "reject"])
async def test_durable_question_reply_publishes_both_identifiers(state, decision):
    from tests.unit.test_durable_questions import checkpoint
    request_id = await checkpoint()
    if decision == "reply":
        await question_mod.reply(request_id, [["Yes"]], user_id="u1")
        kind = QUESTION_REPLIED
    else:
        await question_mod.reject(request_id, user_id="u1")
        kind = QUESTION_REJECTED
    _assert_reply_event([event for event in state if event[0] == kind], kind, request_id)
