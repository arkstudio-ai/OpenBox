"""SQLite smoke test for the personal-Skill lifecycle fence migration."""
from pathlib import Path

from alembic import command
from alembic.config import Config
import sqlalchemy as sa


REVISION = "fc4e6d8b0a2c"
PREVIOUS_REVISION = "fb3d5e7f9a1c"


def _config(database_path: Path, monkeypatch) -> Config:
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(backend_dir / "db" / "migrations"),
    )
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path}")
    return config


def test_lifecycle_migration_preserves_and_backfills_existing_skills(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "skill-lifecycle.db"
    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE user_skills ("
            "id VARCHAR(64) PRIMARY KEY, owner_id VARCHAR(64) NOT NULL, "
            "name VARCHAR(64) NOT NULL, updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            (PREVIOUS_REVISION,),
        )
        connection.exec_driver_sql(
            "INSERT INTO user_skills(id, owner_id, name, updated_at) "
            "VALUES ('skill-1', 'user-1', 'Example', CURRENT_TIMESTAMP)"
        )
    engine.dispose()

    config = _config(database_path, monkeypatch)
    command.upgrade(config, REVISION)

    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("user_skills")}
    indexes = {index["name"] for index in inspector.get_indexes("user_skills")}
    with engine.connect() as connection:
        lifecycle = connection.exec_driver_sql(
            "SELECT lifecycle_state, lifecycle_generation "
            "FROM user_skills WHERE id='skill-1'"
        ).one()
        version = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert {"lifecycle_state", "lifecycle_generation"} <= columns
    assert "ix_user_skills_owner_lifecycle" in indexes
    assert lifecycle == ("active", 1)
    assert version == REVISION
    engine.dispose()

    command.downgrade(config, PREVIOUS_REVISION)
    engine = sa.create_engine(f"sqlite:///{database_path}")
    inspector = sa.inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("user_skills")}
    assert "lifecycle_state" not in columns
    assert "lifecycle_generation" not in columns
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT id FROM user_skills WHERE id='skill-1'"
        ).scalar_one() == "skill-1"
    engine.dispose()
