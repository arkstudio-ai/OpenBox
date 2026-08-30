"""Retiring the job tables must not orphan assets in historical receipts."""

import importlib
import json

import sqlalchemy as sa


migration = importlib.import_module(
    "db.migrations.versions.b6d8f0a2c4e6_remove_skill_job_runtime"
)


class _RecordingOp:
    def __init__(self, connection):
        self.connection = connection
        self.dropped: list[str] = []
        self.dropped_indexes: list[tuple[str, str | None]] = []

    def get_bind(self):
        return self.connection

    def drop_table(self, name: str) -> None:
        self.dropped.append(name)

    def drop_index(self, name: str, *, table_name: str | None = None) -> None:
        self.dropped_indexes.append((name, table_name))


class _SchemaOp:
    def __init__(self):
        self.tables: dict[str, tuple] = {}
        self.indexes: list[tuple[str, str, tuple[str, ...]]] = []

    def create_table(self, name: str, *items) -> None:
        self.tables[name] = items

    def create_index(self, name: str, table: str, columns, **_kwargs) -> None:
        self.indexes.append((name, table, tuple(columns)))


def _receipt_data(connection, parts) -> dict[str, dict]:
    rows = connection.execute(sa.select(parts.c.id, parts.c.data)).mappings()
    return {row["id"]: json.loads(row["data"]) for row in rows}


def test_upgrade_embeds_output_artifacts_before_dropping_join_table(monkeypatch):
    metadata = sa.MetaData()
    parts = sa.Table(
        "parts",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
    )
    artifacts = sa.Table(
        "skill_job_artifacts",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("asset_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
    )
    assets = sa.Table(
        "file_assets",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
    )
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            parts.insert(),
            [
                {
                    "id": "part_missing",
                    "user_id": "user_a",
                    "type": "skill_job",
                    "data": json.dumps({"type": "skill_job", "jobId": "job_1"}),
                },
                {
                    "id": "part_empty",
                    "user_id": "user_a",
                    "type": "skill_job",
                    "data": json.dumps(
                        {"type": "skill_job", "jobId": "job_2", "artifacts": []}
                    ),
                },
                {
                    "id": "part_existing",
                    "user_id": "user_a",
                    "type": "skill_job",
                    "data": json.dumps(
                        {
                            "type": "skill_job",
                            "jobId": "job_1",
                            "artifacts": [{"assetId": "keep_me"}],
                        }
                    ),
                },
                {
                    "id": "part_other_user",
                    "user_id": "user_b",
                    "type": "skill_job",
                    "data": json.dumps({"type": "skill_job", "jobId": "job_1"}),
                },
                {
                    "id": "part_other_type",
                    "user_id": "user_a",
                    "type": "text",
                    "data": json.dumps({"type": "text", "jobId": "job_1"}),
                },
            ],
        )
        connection.execute(
            assets.insert(),
            [
                {
                    "id": "asset_later",
                    "user_id": "user_a",
                    "name": "later.mp4",
                    "mime": "video/mp4",
                },
                {
                    "id": "asset_first",
                    "user_id": "user_a",
                    "name": "first.mp4",
                    "mime": "video/mp4",
                },
                {
                    "id": "asset_job_2",
                    "user_id": "user_a",
                    "name": "second-job.mp4",
                    "mime": "video/mp4",
                },
                {
                    "id": "asset_wrong_user",
                    "user_id": "user_b",
                    "name": "private.mp4",
                    "mime": "video/mp4",
                },
            ],
        )
        connection.execute(
            artifacts.insert(),
            [
                {
                    "id": "sjar_later",
                    "job_id": "job_1",
                    "user_id": "user_a",
                    "asset_id": "asset_later",
                    "role": "output",
                    "ordinal": 2,
                },
                {
                    "id": "sjar_first",
                    "job_id": "job_1",
                    "user_id": "user_a",
                    "asset_id": "asset_first",
                    "role": "output",
                    "ordinal": 1,
                },
                {
                    "id": "sjar_input",
                    "job_id": "job_1",
                    "user_id": "user_a",
                    "asset_id": "asset_job_2",
                    "role": "input",
                    "ordinal": 0,
                },
                {
                    "id": "sjar_job_2",
                    "job_id": "job_2",
                    "user_id": "user_a",
                    "asset_id": "asset_job_2",
                    "role": "output",
                    "ordinal": 0,
                },
                {
                    "id": "sjar_cross_tenant",
                    "job_id": "job_1",
                    "user_id": "user_a",
                    "asset_id": "asset_wrong_user",
                    "role": "output",
                    "ordinal": 0,
                },
            ],
        )

        recording_op = _RecordingOp(connection)
        monkeypatch.setattr(migration, "op", recording_op)
        migration.upgrade()

        data = _receipt_data(connection, parts)
        assert data["part_missing"]["artifacts"] == [
            {"assetId": "asset_first", "name": "first.mp4", "mime": "video/mp4"},
            {"assetId": "asset_later", "name": "later.mp4", "mime": "video/mp4"},
        ]
        assert data["part_empty"]["artifacts"] == [
            {
                "assetId": "asset_job_2",
                "name": "second-job.mp4",
                "mime": "video/mp4",
            }
        ]
        assert data["part_existing"]["artifacts"] == [{"assetId": "keep_me"}]
        assert "artifacts" not in data["part_other_user"]
        assert "artifacts" not in data["part_other_type"]
        assert recording_op.dropped_indexes == [
            ("uq_messages_inbox_marker", "messages")
        ]
        assert recording_op.dropped == [
            "session_inbox",
            "skill_job_artifacts",
            "skill_job_inputs",
            "skill_job_events",
            "skill_job_attempts",
            "skill_jobs",
            "user_skill_settings",
        ]

        # A retry before table removal must preserve already embedded arrays.
        migration._backfill_receipt_artifacts()
        assert _receipt_data(connection, parts) == data


def test_downgrade_recreates_the_complete_pre_removal_schema(monkeypatch):
    schema_op = _SchemaOp()
    monkeypatch.setattr(migration, "op", schema_op)

    migration.downgrade()

    assert set(schema_op.tables) == {
        "skill_jobs",
        "skill_job_attempts",
        "skill_job_events",
        "skill_job_inputs",
        "skill_job_artifacts",
        "user_skill_settings",
        "session_inbox",
    }
    job_columns = {
        item.name
        for item in schema_op.tables["skill_jobs"]
        if isinstance(item, sa.Column)
    }
    assert job_columns == {
        "id",
        "user_id",
        "session_id",
        "project_id",
        "skill_key",
        "skill_version",
        "package_sha256",
        "operation",
        "runtime_kind",
        "queue_name",
        "status",
        "phase",
        "input_data",
        "output_schema",
        "checkpoint_data",
        "progress_data",
        "result_data",
        "error_code",
        "error_message",
        "idempotency_key",
        "request_hash",
        "desired_state",
        "attempt_count",
        "retry_count",
        "max_attempts",
        "next_run_at",
        "deadline_at",
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "handler_version",
        "image_digest",
        "invocation_timeout_seconds",
        "max_external_wait_seconds",
        "user_input_timeout_seconds",
        "cancel_requires_handler",
        "continue_agent_on_success",
        "external_wait_seconds",
        "external_wait_started_at",
        "last_event_seq",
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
    }
    inbox_columns = {
        item.name
        for item in schema_op.tables["session_inbox"]
        if isinstance(item, sa.Column)
    }
    assert "claim_token" in inbox_columns
    assert {name for name, _, _ in schema_op.indexes} == {
        "ix_skill_jobs_claim",
        "ix_skill_jobs_user_created",
        "ix_skill_jobs_session_created",
        "ix_skill_jobs_running_lease",
        "ix_skill_job_attempts_job",
        "ix_skill_job_events_outbox",
        "ix_skill_job_inputs_job",
        "ix_skill_job_artifacts_job",
        "ix_session_inbox_session",
        "ix_session_inbox_claim_recovery",
        "uq_messages_inbox_marker",
    }
