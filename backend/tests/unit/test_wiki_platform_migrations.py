"""Run static migrations on SQLite and an isolated PostgreSQL schema."""
from datetime import datetime, timezone
from importlib import import_module
import os
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from db.models.wiki_workflow import WikiProfile

MODULES = [import_module("db.migrations.versions." + name) for name in (
    "m6e7f8a9b0c1_wiki_organization", "m7f8a9b0c1d2_wiki_exchange_workflows", "m8a9b0c1d2e3_automatic_knowledge",
    "m9b0c1d2e3f4_memory_documents", "ma0c1d2e3f4a5_memory_document_cleanups")]


def migrate(connection):
    connection.execute(sa.text("CREATE TABLE prior_wiki_page (id VARCHAR(64) PRIMARY KEY, body TEXT)"))
    connection.execute(sa.text("INSERT INTO prior_wiki_page VALUES ('preserved', 'existing knowledge')"))
    with Operations.context(MigrationContext.configure(connection)):
        for module in MODULES:
            module.upgrade()
        tables = set(sa.inspect(connection).get_table_names())
        assert {"wiki_concepts", "wiki_organization_runs", "wiki_maintenance_policies", "wiki_exchange_documents",
                "wiki_profiles", "wiki_typed_records", "wiki_workflow_runs", "wiki_workflow_events", "wiki_artifacts",
                "memory_documents", "memory_document_revisions", "memory_document_cleanups"} <= tables
        assert "automatic" in {column["name"] for column in sa.inspect(connection).get_columns("wiki_maintenance_policies")}
        for module in reversed(MODULES):
            module.downgrade()
        for module in MODULES:
            module.upgrade()
        instant = datetime.now(timezone.utc)
        connection.execute(WikiProfile.__table__.insert().values(id="profile", user_id="user", workspace_id="workspace",
            project_id=None, visibility="PERSONAL", revision=1, created_at=instant, updated_at=instant,
            domain="domain", profile_key="handbook", title="保留已有知识", definition={"sample": ["中文", 1]}, definition_hash="a" * 64))
        with pytest.raises(RuntimeError, match="non-empty"):
            MODULES[1].downgrade()
        assert connection.execute(sa.select(WikiProfile.definition)).scalar_one() == {"sample": ["中文", 1]}
        assert connection.scalar(sa.text("SELECT body FROM prior_wiki_page WHERE id = 'preserved'")) == "existing knowledge"


def test_sqlite_upgrade_roundtrip_and_downgrade_refusal(tmp_path):
    engine = sa.create_engine("sqlite:///" + str(tmp_path / "platform.db"))
    with engine.begin() as connection:
        migrate(connection)
    engine.dispose()


@pytest.mark.asyncio
async def test_postgres_upgrade_roundtrip_and_downgrade_refusal():
    url = os.environ.get("MEMORY_WIKI_TEST_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        pytest.skip("isolated PostgreSQL test database not configured")
    schema = "wiki_migration_" + uuid4().hex
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
        await connection.run_sync(migrate)
    async with engine.begin() as connection:
        await connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
    await engine.dispose()
