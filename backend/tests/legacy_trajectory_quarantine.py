"""Legacy trajectory tests whose semantics the re-architecture removes (wave 2).

``tests/conftest.py`` skips every node id listed here. Only tests of the
in-transaction recorder, the business-database trajectory tables or the old
in-process admin API belong here; wave 3 rewrites them and empties this registry.
"""

_WORKER_API = "admin HTTP/WS API moved to the trajectory worker (SPEC 8.12); covered by tests/unit/test_worker_*"
_READ_RACES = "read-race matrix reproduced against the worker app in tests/unit/test_worker_routes_read_races.py"

QUARANTINE: dict[str, str] = {
    "tests/integration/test_trajectory_boundaries.py::test_scoped_tickets_are_atomic_and_cannot_authenticate_execute_socket": _WORKER_API,
    "tests/integration/test_trajectory_boundaries.py::test_readonly_socket_has_no_execution_side_effect_and_rechecks_live_role": _WORKER_API,
    "tests/integration/test_trajectory_boundaries.py::test_idle_subscription_stops_after_token_revocation": _WORKER_API,
    **{f"tests/integration/test_trajectory_media_deletion.py::test_inline_and_derived_media_have_no_independent_copy_after_source_deletion[{large}]": _WORKER_API
       for large in (False, True)},
    "tests/integration/test_trajectory_storage.py::test_admin_read_paths_live_role_and_target": _WORKER_API,
    "tests/integration/test_trajectory_storage.py::test_payload_history_export_deletion": _WORKER_API,
    "tests/integration/test_trajectory_storage.py::test_pause_resume_baseline_and_export_gaps": _WORKER_API,
    "tests/integration/test_trajectory_storage.py::test_malformed_cursors_are_validation_errors": _WORKER_API,
    "tests/integration/test_trajectory_auxiliary.py::test_shutdown_stops_archive_after_receipt_failure":
        "backend lifespan no longer runs the archive worker or exports (SPEC 10 main.py); see test_main_trajectory_lifecycle",
    **{f"tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[{change}-{status}-{resource}]": _READ_RACES
       for change, status in (("role", 403), ("revoked", 401), ("asset_deleted", 410), ("payload_deleted", 410), ("root_deleted", 404))
       for resource in ("payload", "export")},
    "tests/integration/test_trajectory_read_races.py::test_admin_json_and_ticket_are_not_cacheable": _READ_RACES,
    # pytest escapes non-ASCII characters in parameter ids.
    **{f"tests/integration/test_trajectory_read_races.py::test_record_detail_accepts_encoded_path_identity[{artifact_id.encode('unicode_escape').decode()}]": _READ_RACES
       for artifact_id in ("file:/workspace/report.md", "file:/workspace/100%/literal%2Fname.md", "file:/workspace/报告 ?draft#1.md")},
}
