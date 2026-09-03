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

_CLOUD_DESKTOP_COLUMNS = (
    "channel_kind VARCHAR",
    "private_ip VARCHAR",
    "tunnel_port INTEGER",
    "tunnel_bind VARCHAR",
    "tunnel_pubkey TEXT",
    "tunnel_fingerprint VARCHAR",
    "action_api_key_hash VARCHAR",
    "action_api_key_ciphertext TEXT",
    "tunnel_state VARCHAR",
    "last_seen_at DATETIME",
    "channel_error TEXT",
)


def _create_current_schema(connection, *, missing_internal_column: str | None = None):
    connection.exec_driver_sql(
        "CREATE TABLE sessions (id VARCHAR PRIMARY KEY, tool_exposure_state TEXT)"
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
    connection.exec_driver_sql(
        "CREATE TABLE cloud_desktops (id VARCHAR PRIMARY KEY, "
        + ", ".join(_CLOUD_DESKTOP_COLUMNS)
        + ")"
    )


def test_readiness_reports_the_private_exposure_schema_as_required():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE sessions (id VARCHAR PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE parts (id VARCHAR PRIMARY KEY)")
        missing = _missing_readiness_schema(connection)

    assert "sessions.tool_exposure_state" in missing
    assert "parts.canonical_tool_id" in missing
    assert "internal_parts" in missing
    assert "cloud_desktops" in missing
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
