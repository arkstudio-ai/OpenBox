"""Retired orchestration surfaces stay absent from the live application."""

from api.sessions import PromptBody
from db.models.message import Message
from main import create_app
from tool.registry import list_tools, register_builtin_tools


def test_retired_routes_are_not_mounted():
    paths = {route.path for route in create_app().routes}

    assert not any(path.startswith("/api/skill-jobs") for path in paths)
    assert "/api/skills/settings" not in paths
    assert "/api/skills/{skill_key}/settings" not in paths


def test_retired_agent_tool_is_not_registered():
    register_builtin_tools()

    assert "skill_job" not in {tool.id for tool in list_tools()}


def test_retired_inbox_marker_namespace_is_released():
    index_names = {index.name for index in Message.__table__.indexes}

    assert "uq_messages_inbox_marker" not in index_names
    assert "uq_messages_receipt_marker" in index_names
    body = PromptBody(text="continue", client_message_id="sji:now-user-owned")
    assert body.client_message_id == "sji:now-user-owned"
