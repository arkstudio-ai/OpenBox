"""Desktop/single-user bootstrap owns a stable default workspace."""
import sqlalchemy as sa

from db.base import Base, _seed_single_user_scope


def test_single_user_seed_creates_default_workspace_member_and_project():
    engine = sa.create_engine("sqlite://")
    import db.models  # noqa: F401

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        _seed_single_user_scope(connection)
        _seed_single_user_scope(connection)
        assert connection.execute(
            sa.text("SELECT default_workspace_id FROM users WHERE id = 'default'")
        ).scalar_one() == "ws_default"
        assert connection.execute(
            sa.text("SELECT owner_user_id FROM workspaces WHERE id = 'ws_default'")
        ).scalar_one() == "default"
        assert connection.execute(
            sa.text(
                "SELECT role, status FROM workspace_members "
                "WHERE workspace_id = 'ws_default' AND user_id = 'default'"
            )
        ).one() == ("owner", "active")
        assert connection.execute(
            sa.text("SELECT workspace_id FROM projects WHERE id = 'default'")
        ).scalar_one() == "ws_default"
