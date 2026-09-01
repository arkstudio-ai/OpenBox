"""The public health probe must fail before additive migrations land."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from db.base import _missing_readiness_schema


_INTERNAL_PART_COLUMNS = (
    ("id", "VARCHAR PRIMARY KEY"),
    ("session_id", "VARCHAR"),
    ("message_id", "VARCHAR"),
    ("user_id", "VARCHAR"),
    ("kind", "VARCHAR"),
    ("capability_key_digest", "VARCHAR"),
    ("response_chain_id", "VARCHAR"),
    ("stream_seq", "INTEGER"),
    ("origin_seq", "INTEGER"),
    ("dedupe_key", "VARCHAR"),
    ("data", "TEXT"),
    ("created_at", "DATETIME"),
)

_SURFACE_EVENT_COLUMNS = (
    ("id", "VARCHAR PRIMARY KEY"),
    ("session_id", "VARCHAR"),
    ("user_id", "VARCHAR"),
    ("sequence", "BIGINT"),
    ("kind", "VARCHAR"),
    ("anchor_message_id", "VARCHAR"),
    ("replacement_run_id", "VARCHAR"),
    ("replacement_generation", "BIGINT"),
    ("hidden_message_ids", "TEXT"),
    ("public_snapshot", "TEXT"),
    ("created_at", "DATETIME"),
)

_AGENT_EVENT_COLUMNS = (
    ("id", "VARCHAR PRIMARY KEY"),
    ("session_id", "VARCHAR"),
    ("user_id", "VARCHAR"),
    ("sequence", "BIGINT"),
    ("event_key", "VARCHAR"),
    ("kind", "VARCHAR"),
    ("run_id", "VARCHAR"),
    ("generation", "BIGINT"),
    ("turn_id", "VARCHAR"),
    ("step_id", "VARCHAR"),
    ("message_id", "VARCHAR"),
    ("part_id", "VARCHAR"),
    ("tool_call_id", "VARCHAR"),
    ("payload", "TEXT"),
    ("created_at", "DATETIME"),
)

_AGENT_DRIVER_COLUMNS = (
    ("session_id", "VARCHAR"),
    ("user_id", "VARCHAR"),
    ("generation", "INTEGER"),
    ("run_id", "VARCHAR"),
    ("owner_id", "VARCHAR"),
    ("phase", "VARCHAR"),
    ("trigger_message_id", "VARCHAR"),
    ("lease_expires_at", "DATETIME"),
    ("abort_requested_at", "DATETIME"),
    ("started_at", "DATETIME"),
    ("updated_at", "DATETIME"),
)

_AGENT_INBOX_COLUMNS = (
    ("id", "VARCHAR PRIMARY KEY"),
    ("user_id", "VARCHAR"),
    ("project_id", "VARCHAR"),
    ("session_id", "VARCHAR"),
    ("client_id", "VARCHAR"),
    ("request_digest", "VARCHAR"),
    ("delivery", "VARCHAR"),
    ("target", "VARCHAR"),
    ("prompt", "TEXT"),
    ("attachments", "TEXT"),
    ("agent", "VARCHAR"),
    ("model", "VARCHAR"),
    ("video_model", "VARCHAR"),
    ("variant", "VARCHAR"),
    ("output_format", "TEXT"),
    ("state", "VARCHAR"),
    ("message_id", "VARCHAR"),
    ("result_message_id", "VARCHAR"),
    ("run_id", "VARCHAR"),
    ("generation", "INTEGER"),
    ("turn_id", "VARCHAR"),
    ("step_id", "VARCHAR"),
    ("claim_token", "VARCHAR"),
    ("claim_owner", "VARCHAR"),
    ("claim_expires_at", "DATETIME"),
    ("outcome", "VARCHAR"),
    ("error", "TEXT"),
    ("delivery_attempts", "INTEGER"),
    ("delivery_last_error", "TEXT"),
    ("accepted_at", "DATETIME"),
    ("claimed_at", "DATETIME"),
    ("canceled_at", "DATETIME"),
    ("settled_at", "DATETIME"),
    ("created_at", "DATETIME"),
    ("updated_at", "DATETIME"),
)

_EXTERNAL_EFFECT_COLUMNS = (
    ("id", "VARCHAR PRIMARY KEY"),
    ("tenant_id", "VARCHAR"),
    ("project_id", "VARCHAR"),
    ("session_id", "VARCHAR"),
    ("run_id", "VARCHAR"),
    ("run_generation", "INTEGER"),
    ("adapter", "VARCHAR"),
    ("provider", "VARCHAR"),
    ("operation", "VARCHAR"),
    ("idempotency_key", "VARCHAR"),
    ("request_hash", "VARCHAR"),
    ("safe_context", "TEXT"),
    ("state", "VARCHAR"),
    ("attempt_count", "INTEGER"),
    ("reconcile_count", "INTEGER"),
    ("claim_generation", "INTEGER"),
    ("claim_kind", "VARCHAR"),
    ("claim_token", "VARCHAR"),
    ("claim_owner", "VARCHAR"),
    ("claim_expires_at", "DATETIME"),
    ("provider_handle", "VARCHAR"),
    ("provider_receipt", "TEXT"),
    ("projection", "TEXT"),
    ("last_error", "TEXT"),
    ("reconcile_after", "DATETIME"),
    ("prepared_at", "DATETIME"),
    ("submitting_at", "DATETIME"),
    ("accepted_at", "DATETIME"),
    ("completed_at", "DATETIME"),
    ("created_at", "DATETIME"),
    ("updated_at", "DATETIME"),
)

_EXTERNAL_EFFECT_EVIDENCE_COLUMNS = (
    ("id", "VARCHAR PRIMARY KEY"),
    ("effect_id", "VARCHAR"),
    ("sequence", "INTEGER"),
    ("claim_generation", "INTEGER"),
    ("phase", "VARCHAR"),
    ("evidence", "TEXT"),
    ("created_at", "DATETIME"),
)

_TASK_HANDOFF_COLUMNS = (
    ("id", "VARCHAR PRIMARY KEY"),
    ("user_id", "VARCHAR"),
    ("parent_session_id", "VARCHAR"),
    ("parent_message_id", "VARCHAR"),
    ("parent_part_id", "VARCHAR"),
    ("parent_run_id", "VARCHAR"),
    ("parent_generation", "INTEGER"),
    ("child_session_id", "VARCHAR"),
    ("child_trigger_message_id", "VARCHAR"),
    ("child_run_id", "VARCHAR"),
    ("child_generation", "INTEGER"),
    ("state", "VARCHAR"),
    ("task_title", "VARCHAR"),
    ("subagent_type", "VARCHAR"),
    ("result_payload", "TEXT"),
    ("completed_at", "DATETIME"),
    ("rejoined_at", "DATETIME"),
    ("created_at", "DATETIME"),
    ("updated_at", "DATETIME"),
)

_SUBAGENT_DESCRIPTOR_COLUMNS = (
    ("id", "VARCHAR PRIMARY KEY"),
    ("user_id", "VARCHAR"),
    ("project_id", "VARCHAR"),
    ("parent_session_id", "VARCHAR"),
    ("child_session_id", "VARCHAR"),
    ("root_session_id", "VARCHAR"),
    ("parent_descriptor_id", "VARCHAR"),
    ("depth", "INTEGER"),
    ("subagent_type", "VARCHAR"),
    ("lifecycle", "VARCHAR"),
    ("state", "VARCHAR"),
    ("generation", "INTEGER"),
    ("active_activation_id", "VARCHAR"),
    ("interrupt_requested_generation", "INTEGER"),
    ("interrupt_applied_generation", "INTEGER"),
    ("created_at", "DATETIME"),
    ("updated_at", "DATETIME"),
    ("settled_at", "DATETIME"),
)

_SUBAGENT_ACTIVATION_COLUMNS = (
    ("id", "VARCHAR PRIMARY KEY"),
    ("descriptor_id", "VARCHAR"),
    ("user_id", "VARCHAR"),
    ("project_id", "VARCHAR"),
    ("parent_session_id", "VARCHAR"),
    ("parent_message_id", "VARCHAR"),
    ("parent_part_id", "VARCHAR"),
    ("parent_run_id", "VARCHAR"),
    ("parent_generation", "INTEGER"),
    ("descriptor_generation", "INTEGER"),
    ("kind", "VARCHAR"),
    ("child_session_id", "VARCHAR"),
    ("child_trigger_message_id", "VARCHAR"),
    ("child_run_id", "VARCHAR"),
    ("child_generation", "INTEGER"),
    ("state", "VARCHAR"),
    ("claim_token", "VARCHAR"),
    ("claim_owner", "VARCHAR"),
    ("claim_expires_at", "DATETIME"),
    ("task_title", "VARCHAR"),
    ("created_at", "DATETIME"),
    ("updated_at", "DATETIME"),
    ("completed_at", "DATETIME"),
)

_SUBAGENT_OUTBOX_COLUMNS = (
    ("activation_id", "VARCHAR PRIMARY KEY"),
    ("descriptor_id", "VARCHAR"),
    ("user_id", "VARCHAR"),
    ("project_id", "VARCHAR"),
    ("parent_session_id", "VARCHAR"),
    ("parent_message_id", "VARCHAR"),
    ("parent_part_id", "VARCHAR"),
    ("state", "VARCHAR"),
    ("outcome", "VARCHAR"),
    ("result_payload", "TEXT"),
    ("created_at", "DATETIME"),
    ("updated_at", "DATETIME"),
    ("ready_at", "DATETIME"),
    ("delivered_at", "DATETIME"),
)

_USER_SKILL_LIFECYCLE_COLUMNS = (
    ("lifecycle_state", "VARCHAR(16)"),
    ("lifecycle_generation", "INTEGER"),
)

_CRON_OUTBOX_COLUMNS = (
    ("id", "VARCHAR PRIMARY KEY"),
    ("run_id", "VARCHAR"),
    ("job_id", "VARCHAR"),
    ("user_id", "VARCHAR"),
    ("project_id", "VARCHAR"),
    ("session_id", "VARCHAR"),
    ("kind", "VARCHAR"),
    ("payload", "TEXT"),
    ("state", "VARCHAR"),
    ("attempts", "INTEGER"),
    ("available_at", "DATETIME"),
    ("claim_token", "VARCHAR"),
    ("claim_owner", "VARCHAR"),
    ("claim_expires_at", "DATETIME"),
    ("delivered_at", "DATETIME"),
    ("last_error", "TEXT"),
    ("created_at", "DATETIME"),
    ("updated_at", "DATETIME"),
)


def _create_current_schema(
    connection,
    *,
    missing_internal_column: str | None = None,
    missing_surface_column: str | None = None,
    missing_agent_event_column: str | None = None,
    missing_agent_driver_column: str | None = None,
    missing_agent_inbox_column: str | None = None,
    missing_external_effect_column: str | None = None,
    missing_external_effect_evidence_column: str | None = None,
    missing_task_handoff_column: str | None = None,
    missing_user_skill_column: str | None = None,
    missing_cron_outbox_column: str | None = None,
    missing_subagent_descriptor_column: str | None = None,
    missing_subagent_activation_column: str | None = None,
    missing_subagent_outbox_column: str | None = None,
):
    connection.exec_driver_sql(
        "CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME)"
    )
    connection.exec_driver_sql(
        "CREATE TABLE cron_jobs ("
        "run_generation BIGINT, run_token VARCHAR, run_owner VARCHAR, "
        "lease_expires_at DATETIME, heartbeat_at DATETIME)"
    )
    connection.exec_driver_sql(
        "CREATE TABLE cron_runs ("
        "claim_token VARCHAR, claim_generation BIGINT, claim_owner VARCHAR)"
    )
    outbox_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _CRON_OUTBOX_COLUMNS
        if name != missing_cron_outbox_column
    )
    connection.exec_driver_sql(f"CREATE TABLE cron_delivery_outbox ({outbox_columns})")
    agent_driver_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _AGENT_DRIVER_COLUMNS
        if name != missing_agent_driver_column
    )
    connection.exec_driver_sql(
        f"CREATE TABLE agent_driver_states ({agent_driver_columns})"
    )
    agent_inbox_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _AGENT_INBOX_COLUMNS
        if name != missing_agent_inbox_column
    )
    connection.exec_driver_sql(
        f"CREATE TABLE agent_inbox_items ({agent_inbox_columns})"
    )
    external_effect_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _EXTERNAL_EFFECT_COLUMNS
        if name != missing_external_effect_column
    )
    connection.exec_driver_sql(
        f"CREATE TABLE external_effects ({external_effect_columns})"
    )
    external_effect_evidence_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _EXTERNAL_EFFECT_EVIDENCE_COLUMNS
        if name != missing_external_effect_evidence_column
    )
    connection.exec_driver_sql(
        f"CREATE TABLE external_effect_evidence ({external_effect_evidence_columns})"
    )
    connection.exec_driver_sql(
        "CREATE TABLE sessions ("
        "id VARCHAR PRIMARY KEY, tool_exposure_state TEXT, variant VARCHAR(32))"
    )
    connection.exec_driver_sql(
        "CREATE TABLE parts ("
        "id VARCHAR PRIMARY KEY, stream_seq INTEGER, canonical_tool_id VARCHAR, "
        "wire_tool_name VARCHAR, provider_binding_digest VARCHAR, provider_dialect VARCHAR)"
    )
    internal_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _INTERNAL_PART_COLUMNS
        if name != missing_internal_column
    )
    connection.exec_driver_sql(f"CREATE TABLE internal_parts ({internal_columns})")
    surface_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _SURFACE_EVENT_COLUMNS
        if name != missing_surface_column
    )
    connection.exec_driver_sql(
        f"CREATE TABLE session_surface_events ({surface_columns})"
    )
    agent_event_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _AGENT_EVENT_COLUMNS
        if name != missing_agent_event_column
    )
    connection.exec_driver_sql(f"CREATE TABLE agent_events ({agent_event_columns})")
    task_handoff_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _TASK_HANDOFF_COLUMNS
        if name != missing_task_handoff_column
    )
    connection.exec_driver_sql(f"CREATE TABLE task_handoffs ({task_handoff_columns})")
    descriptor_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _SUBAGENT_DESCRIPTOR_COLUMNS
        if name != missing_subagent_descriptor_column
    )
    activation_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _SUBAGENT_ACTIVATION_COLUMNS
        if name != missing_subagent_activation_column
    )
    subagent_outbox_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _SUBAGENT_OUTBOX_COLUMNS
        if name != missing_subagent_outbox_column
    )
    connection.exec_driver_sql(
        f"CREATE TABLE subagent_descriptors ({descriptor_columns})"
    )
    connection.exec_driver_sql(
        f"CREATE TABLE subagent_activations ({activation_columns})"
    )
    connection.exec_driver_sql(
        f"CREATE TABLE subagent_outbox ({subagent_outbox_columns})"
    )
    user_skill_columns = ", ".join(
        f"{name} {column_type}"
        for name, column_type in _USER_SKILL_LIFECYCLE_COLUMNS
        if name != missing_user_skill_column
    )
    connection.exec_driver_sql(f"CREATE TABLE user_skills ({user_skill_columns})")


def test_readiness_reports_the_private_exposure_schema_as_required():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE sessions (id VARCHAR PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE parts (id VARCHAR PRIMARY KEY)")
        missing = _missing_readiness_schema(connection)

    assert "sessions.tool_exposure_state" in missing
    assert "parts.canonical_tool_id" in missing
    assert "internal_parts" in missing
    assert "session_surface_events" in missing
    assert "agent_events" in missing
    assert "agent_inbox_items" in missing
    assert "external_effects" in missing
    assert "external_effect_evidence" in missing
    assert "task_handoffs" in missing
    engine.dispose()


@pytest.mark.parametrize(
    "missing_column",
    [name for name, _column_type in _INTERNAL_PART_COLUMNS],
)
def test_readiness_rejects_each_missing_internal_parts_column(missing_column):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_current_schema(
            connection,
            missing_internal_column=missing_column,
        )
        assert _missing_readiness_schema(connection) == (
            f"internal_parts.{missing_column}",
        )
    engine.dispose()


def test_readiness_accepts_the_complete_current_schema():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_current_schema(connection)
        assert _missing_readiness_schema(connection) == ()
    engine.dispose()


@pytest.mark.parametrize(
    "missing_column",
    [name for name, _column_type in _CRON_OUTBOX_COLUMNS],
)
def test_readiness_rejects_each_missing_cron_outbox_column(missing_column):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_current_schema(
            connection,
            missing_cron_outbox_column=missing_column,
        )
        assert _missing_readiness_schema(connection) == (
            f"cron_delivery_outbox.{missing_column}",
        )
    engine.dispose()


@pytest.mark.parametrize(
    "missing_column",
    [name for name, _column_type in _AGENT_EVENT_COLUMNS],
)
def test_readiness_rejects_each_missing_agent_event_column(missing_column):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_current_schema(
            connection,
            missing_agent_event_column=missing_column,
        )
        assert _missing_readiness_schema(connection) == (
            f"agent_events.{missing_column}",
        )
    engine.dispose()


@pytest.mark.parametrize(
    "missing_column",
    [name for name, _column_type in _USER_SKILL_LIFECYCLE_COLUMNS],
)
def test_readiness_rejects_each_missing_user_skill_lifecycle_column(missing_column):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_current_schema(
            connection,
            missing_user_skill_column=missing_column,
        )
        assert _missing_readiness_schema(connection) == (
            f"user_skills.{missing_column}",
        )
    engine.dispose()


@pytest.mark.parametrize(
    "missing_column",
    [name for name, _column_type in _AGENT_DRIVER_COLUMNS],
)
def test_readiness_rejects_each_missing_agent_driver_column(missing_column):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_current_schema(
            connection,
            missing_agent_driver_column=missing_column,
        )
        assert _missing_readiness_schema(connection) == (
            f"agent_driver_states.{missing_column}",
        )
    engine.dispose()


@pytest.mark.parametrize(
    "missing_column",
    [name for name, _column_type in _AGENT_INBOX_COLUMNS],
)
def test_readiness_rejects_each_missing_agent_inbox_column(missing_column):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_current_schema(
            connection,
            missing_agent_inbox_column=missing_column,
        )
        assert _missing_readiness_schema(connection) == (
            f"agent_inbox_items.{missing_column}",
        )
    engine.dispose()


@pytest.mark.parametrize(
    "missing_column",
    [name for name, _column_type in _EXTERNAL_EFFECT_COLUMNS],
)
def test_readiness_rejects_each_missing_external_effect_column(missing_column):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_current_schema(
            connection,
            missing_external_effect_column=missing_column,
        )
        assert _missing_readiness_schema(connection) == (
            f"external_effects.{missing_column}",
        )
    engine.dispose()


@pytest.mark.parametrize(
    "missing_column",
    [name for name, _column_type in _EXTERNAL_EFFECT_EVIDENCE_COLUMNS],
)
def test_readiness_rejects_each_missing_external_effect_evidence_column(
    missing_column,
):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_current_schema(
            connection,
            missing_external_effect_evidence_column=missing_column,
        )
        assert _missing_readiness_schema(connection) == (
            f"external_effect_evidence.{missing_column}",
        )
    engine.dispose()


@pytest.mark.parametrize(
    "missing_column",
    [name for name, _column_type in _SURFACE_EVENT_COLUMNS],
)
def test_readiness_rejects_each_missing_surface_event_column(missing_column):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_current_schema(
            connection,
            missing_surface_column=missing_column,
        )
        assert _missing_readiness_schema(connection) == (
            f"session_surface_events.{missing_column}",
        )
    engine.dispose()


@pytest.mark.parametrize(
    "missing_column",
    [name for name, _column_type in _TASK_HANDOFF_COLUMNS],
)
def test_readiness_rejects_each_missing_task_handoff_column(missing_column):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _create_current_schema(
            connection,
            missing_task_handoff_column=missing_column,
        )
        assert _missing_readiness_schema(connection) == (
            f"task_handoffs.{missing_column}",
        )
    engine.dispose()


@pytest.mark.parametrize(
    "table_name,columns,argument",
    [
        ("subagent_descriptors", _SUBAGENT_DESCRIPTOR_COLUMNS, "descriptor"),
        ("subagent_activations", _SUBAGENT_ACTIVATION_COLUMNS, "activation"),
        ("subagent_outbox", _SUBAGENT_OUTBOX_COLUMNS, "outbox"),
    ],
)
def test_readiness_rejects_each_missing_subagent_column(
    table_name,
    columns,
    argument,
):
    for missing_column, _column_type in columns:
        engine = sa.create_engine("sqlite:///:memory:")
        kwargs = {f"missing_subagent_{argument}_column": missing_column}
        with engine.begin() as connection:
            _create_current_schema(connection, **kwargs)
            assert _missing_readiness_schema(connection) == (
                f"{table_name}.{missing_column}",
            )
        engine.dispose()
