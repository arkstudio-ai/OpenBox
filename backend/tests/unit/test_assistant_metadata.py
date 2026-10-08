"""The assistant composer reads skill metadata without provisioning or restoring a desktop."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from api.metadata import list_skills


async def test_passive_skill_catalogue_never_discovers_or_restores_container(monkeypatch):
    from sandbox.manager import sandbox_manager
    from skill import skill, user_library
    desktop = AsyncMock(side_effect=AssertionError("A passive catalogue must not touch the desktop"))
    monkeypatch.setattr(sandbox_manager, "get_client_any", desktop)
    owned = AsyncMock(return_value=[{"name": "private", "description": "Owner summary", "archive_data": b"private bytes",
                                     "install_dir": "/private/path", "restore_available": True}])
    monkeypatch.setattr(user_library, "list_owned_skills", owned)
    monkeypatch.setattr(skill, "list_skills", AsyncMock(return_value=[
        SimpleNamespace(name="host", description="Platform summary"),
        SimpleNamespace(name="private", description="Overridden by owner"),
    ]))
    result = await list_skills({"user_id": "owner", "workspace_id": "workspace"}, surface="assistant")
    assert result == [{"name": "host", "description": "Platform summary"},
                      {"name": "private", "description": "Owner summary"}]
    owned.assert_awaited_once_with("owner", "workspace")
    desktop.assert_not_awaited()
