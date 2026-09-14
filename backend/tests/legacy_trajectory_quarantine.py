"""Legacy trajectory tests quarantined during wave 2: pytest node id -> one-line reason.

tests/conftest.py skips every listed test (a parametrized test can be listed
without its parameters). Only tests of semantics that wave 2 removes on
purpose belong here: the in-transaction recorder, the business-database
trajectory tables and the old in-process admin API. Wave 3 must empty this
registry.
"""

_ADMIN_API_EXPORT = ("w2-archive: exports via the in-process admin API on business-database trajectory tables; "
                     "the worker's ExportService builds them from the trace database")
_BUSINESS_EXPORT = ("w2-archive: create_export/build_export on business-database trajectory tables; "
                    "exports now live in the trace database")

QUARANTINE: dict[str, str] = {
    "tests/integration/test_trajectory_storage.py::test_payload_history_export_deletion": _ADMIN_API_EXPORT,
    "tests/integration/test_trajectory_storage.py::test_pause_resume_baseline_and_export_gaps": _ADMIN_API_EXPORT,
    "tests/integration/test_trajectory_storage.py::test_export_shutdown_resumes_and_deletion_wins":
        "w2-archive: patches the legacy in-process export build on business tables; leased ExportService builds replace it",
    "tests/integration/test_trajectory_media_deletion.py::"
    "test_inline_and_derived_media_have_no_independent_copy_after_source_deletion[False]": _ADMIN_API_EXPORT,
    "tests/integration/test_trajectory_media_deletion.py::"
    "test_inline_and_derived_media_have_no_independent_copy_after_source_deletion[True]": _ADMIN_API_EXPORT,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[role-403-export]":
        _ADMIN_API_EXPORT,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[revoked-401-export]":
        _ADMIN_API_EXPORT,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[asset_deleted-410-export]":
        _ADMIN_API_EXPORT,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[payload_deleted-410-export]":
        _ADMIN_API_EXPORT,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[root_deleted-404-export]":
        _ADMIN_API_EXPORT,
    **{
        "tests/integration/test_trajectory_stream_redaction.py::"
        f"test_fragmented_request_credentials_never_enter_retained_views[{case}-{storage}]": _BUSINESS_EXPORT
        for case in ("litellm_arguments", "litellm_text", "litellm_reasoning", "responses_arguments", "responses_text")
        for storage in ("inline", "payload")
    },
    "tests/integration/test_trajectory_stream_redaction.py::test_tool_cumulative_output_redaction_resets_for_each_call":
        _BUSINESS_EXPORT,
}
