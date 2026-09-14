"""Daily Parquet exports of trajectory statistics (plan phase 5, WAVE3 contract 4).

``python -m trajectory.analytics export --date YYYY-MM-DD [--dry-run]`` reads
one UTC day from the trace database (session summaries, request records and
tool records: ``source``), converts it to ``sessions``, ``requests`` and
``tools`` Parquet files with a confined DuckDB in a temporary directory
(``parquet``) and uploads them through the trajectory blob store as
``{TRAJECTORY_ANALYTICS_PREFIX}dt=YYYY-MM-DD/<table>.parquet`` (``export``).
A rerun overwrites that date. The files hold identifiers, statuses, names,
times and counts, never content. Schema, querying and the ClickHouse
trigger: docs/trajectory-rearch/ANALYTICS.md.
"""
