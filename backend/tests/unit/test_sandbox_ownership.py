import sandbox.ownership as ownership


async def test_owner_is_users_default_workspace(monkeypatch):
    async def get_user(user_id):
        assert user_id == "usr-a1"
        return {"default_workspace_id": "ws-a1"}

    monkeypatch.setattr(ownership._user_repo, "get", get_user)
    assert await ownership.owner_for("usr-a1") == "ws-a1"


async def test_request_owner_prefers_selected_workspace(monkeypatch):
    async def must_not_read_default(_user_id):
        raise AssertionError("selected workspace must win")

    monkeypatch.setattr(ownership._user_repo, "get", must_not_read_default)
    assert await ownership.owner_for_request(
        {"user_id": "usr-a1", "workspace_id": "ws-selected"}
    ) == "ws-selected"
