from sandbox.ownership import owner_for


async def test_a1_owner_is_user_id():
    assert await owner_for("usr-a1") == "usr-a1"
