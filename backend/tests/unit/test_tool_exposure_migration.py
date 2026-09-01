"""Previous-head to new-head schema smoke for private exposure state."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
import sqlalchemy as sa


PREVIOUS_HEAD = "b6d8f0a2c4e6"
NEW_HEAD = "f0b2d4e6a8c1"


def _previous_head_fixture(database_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE users (id VARCHAR(64) PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE file_assets (id VARCHAR(64) PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE video_material_groups ("
            "id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL, "
            "provider VARCHAR(32) NOT NULL, project_name VARCHAR(128) NOT NULL, "
            "group_type VARCHAR(24) NOT NULL, label VARCHAR(120) NOT NULL, "
            "provider_group_id VARCHAR(160), status VARCHAR(24) NOT NULL, "
            "provider_token TEXT, authorization_url TEXT, qr_code TEXT, error TEXT, "
            "expires_at DATETIME, authorized_at DATETIME, created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_video_material_groups_user_updated "
            "ON video_material_groups(user_id, updated_at)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_video_material_groups_status "
            "ON video_material_groups(status, updated_at)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE video_material_assets ("
            "id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL, "
            "group_id VARCHAR(64) NOT NULL, source_asset_id VARCHAR(64) NOT NULL, "
            "provider_asset_id VARCHAR(160), asset_type VARCHAR(16) NOT NULL, "
            "status VARCHAR(24) NOT NULL, error TEXT, created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_video_material_assets_user_updated "
            "ON video_material_assets(user_id, updated_at)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_video_material_assets_source "
            "ON video_material_assets(source_asset_id)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE video_productions ("
            "id VARCHAR(64) PRIMARY KEY, "
            "character_reference_type VARCHAR(24) NOT NULL DEFAULT 'virtual', "
            "character_identity_id VARCHAR(64))"
        )
        connection.exec_driver_sql(
            "CREATE TABLE video_approvals ("
            "id VARCHAR(64) PRIMARY KEY, production_id VARCHAR(64) NOT NULL, "
            "max_calls INTEGER, used_calls INTEGER NOT NULL DEFAULT 0)"
        )
        connection.exec_driver_sql("CREATE TABLE projects (id VARCHAR(64) PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE sessions ("
            "id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL, "
            "project_id VARCHAR(64) NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE messages ("
            "id VARCHAR(64) PRIMARY KEY, session_id VARCHAR(64) NOT NULL, "
            "user_id VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE parts ("
            "id VARCHAR(64) PRIMARY KEY, message_id VARCHAR(64) NOT NULL, "
            "session_id VARCHAR(64) NOT NULL, user_id VARCHAR(64) NOT NULL, "
            "type VARCHAR(32) NOT NULL, data TEXT NOT NULL, created_at DATETIME NOT NULL)"
        )
        # Both tables predate PREVIOUS_HEAD in the real migration graph. Keep
        # this intentionally-small fixture sufficient for later additive Cron
        # migrations as the graph advances beyond the exposure revision.
        connection.exec_driver_sql(
            "CREATE TABLE cron_jobs (id VARCHAR(64) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE cron_runs (id VARCHAR(64) PRIMARY KEY, job_id VARCHAR(64))"
        )
        connection.exec_driver_sql(
            "CREATE TABLE user_skills ("
            "id VARCHAR(64) PRIMARY KEY, owner_id VARCHAR(64) NOT NULL, "
            "updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (PREVIOUS_HEAD,),
        )
        connection.exec_driver_sql("INSERT INTO users(id) VALUES ('old-user')")
        connection.exec_driver_sql("INSERT INTO projects(id) VALUES ('old-project')")
        connection.exec_driver_sql(
            "INSERT INTO video_approvals(id, production_id, max_calls, used_calls) "
            "VALUES ('old-approval', 'old-production', 2, 1)"
        )
        connection.exec_driver_sql(
            "INSERT INTO sessions(id, user_id, project_id) "
            "VALUES ('old-session', 'old-user', 'old-project')"
        )
        connection.exec_driver_sql(
            "INSERT INTO messages(id, session_id, user_id, created_at) "
            "VALUES ('old-message', 'old-session', 'old-user', CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "INSERT INTO parts(id, message_id, session_id, user_id, type, data, created_at) "
            "VALUES ('old-part', 'old-message', 'old-session', 'old-user', "
            '\'tool\', \'{"type":"tool","tool":"legacy_alias"}\', CURRENT_TIMESTAMP)'
        )
    engine.dispose()


def _config(database_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "db" / "migrations"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    return config


def test_previous_head_upgrade_backfills_state_and_keeps_single_head(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "previous-head.db"
    _previous_head_fixture(database_path)
    config = _config(database_path, monkeypatch)

    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    with engine.connect() as connection:
        state = connection.exec_driver_sql(
            "SELECT tool_exposure_state FROM sessions WHERE id='old-session'"
        ).scalar_one()
        version = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert state == "{}"
    assert version == NEW_HEAD
    assert "internal_parts" in inspector.get_table_names()
    assert "session_surface_events" in inspector.get_table_names()
    assert "task_handoffs" in inspector.get_table_names()
    assert "agent_events" in inspector.get_table_names()
    assert "authority_snapshot" in {
        column["name"] for column in inspector.get_columns("subagent_descriptors")
    }
    assert {
        "lifecycle_state",
        "lifecycle_generation",
    } <= {column["name"] for column in inspector.get_columns("user_skills")}
    assert "tool_exposure_state" in {
        column["name"] for column in inspector.get_columns("sessions")
    }
    assert "variant" in {
        column["name"] for column in inspector.get_columns("sessions")
    }
    part_columns = {column["name"] for column in inspector.get_columns("parts")}
    assert {
        "stream_seq",
        "canonical_tool_id",
        "wire_tool_name",
        "provider_binding_digest",
        "provider_dialect",
    } <= part_columns
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT canonical_tool_id FROM parts WHERE id='old-part'"
            ).scalar_one()
            is None
        )
    assert ScriptDirectory.from_config(config).get_heads() == [NEW_HEAD]
    engine.dispose()

    # An empty/drained deployment can safely roll the schema back.  The
    # separate test below proves live private state is refused instead.
    command.downgrade(config, PREVIOUS_HEAD)
    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            == PREVIOUS_HEAD
        )
    assert "internal_parts" not in inspector.get_table_names()
    assert "video_material_groups" in inspector.get_table_names()
    assert "video_material_assets" in inspector.get_table_names()
    assert {
        "character_reference_type",
        "character_identity_id",
    } <= {column["name"] for column in inspector.get_columns("video_productions")}
    assert {"max_calls", "used_calls"} <= {
        column["name"] for column in inspector.get_columns("video_approvals")
    }
    assert "tool_exposure_state" not in {
        column["name"] for column in inspector.get_columns("sessions")
    }
    downgraded_part_columns = {
        column["name"] for column in inspector.get_columns("parts")
    }
    assert (
        not {
            "stream_seq",
            "canonical_tool_id",
            "wire_tool_name",
            "provider_binding_digest",
            "provider_dialect",
        }
        & downgraded_part_columns
    )
    assert not {
        "lifecycle_state",
        "lifecycle_generation",
    } & {column["name"] for column in inspector.get_columns("user_skills")}
    engine.dispose()


def test_downgrade_preflight_refuses_live_private_state(tmp_path, monkeypatch):
    database_path = tmp_path / "unsafe-downgrade.db"
    _previous_head_fixture(database_path)
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE sessions SET tool_exposure_state = ? WHERE id='old-session'",
            (
                '{"v":1,"next_origin_seq":2,"agents":{"build":{}},"provider_fallback":{}}',
            ),
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="downgrade refused"):
        command.downgrade(config, PREVIOUS_HEAD)


def test_downgrade_preflight_refuses_persisted_tool_identity(tmp_path, monkeypatch):
    database_path = tmp_path / "unsafe-tool-identity-downgrade.db"
    _previous_head_fixture(database_path)
    config = _config(database_path, monkeypatch)
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE parts SET stream_seq=0, canonical_tool_id=?, wire_tool_name=?, "
            "provider_binding_digest=?, provider_dialect=? WHERE id='old-part'",
            (
                "mcp:v2:" + "a" * 52,
                "wire_name",
                "a" * 64,
                "responses",
            ),
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="downgrade refused"):
        command.downgrade(config, PREVIOUS_HEAD)
