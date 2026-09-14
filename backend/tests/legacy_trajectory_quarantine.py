"""Legacy trajectory tests skipped while the re-architecture lands.

Each entry is a pytest node id (a module id skips every test in it) with the
reason its test exercises semantics wave 2 removed: the in-transaction
recorder, the business-database trajectory tables or the in-process admin
API. tests/conftest.py applies the skips. Wave 3 ports or rewrites these tests
and must leave this registry empty.
"""

_BUSINESS_EVENTS = "asserts facts read from the business trajectory_events table, retired in wave 2 (w2-producers)"

QUARANTINE: dict[str, str] = {
    # w2-producers: the in-transaction recorder and the business trajectory tables are gone.
    "tests/integration/test_trajectory_agent_loop.py":
        "reads business trajectory tables and the legacy recorder (w2-producers)",
    "tests/integration/test_trajectory_auxiliary.py":
        "imports the legacy recorder's storage fixture and business trajectory tables (w2-producers)",
    "tests/integration/test_trajectory_boundaries.py":
        "legacy recorder fixture, in-process admin API and business trajectory tables (w2-producers)",
    "tests/integration/test_trajectory_process_crash.py":
        "its crash benchmark reads business trajectory tables through the legacy recorder (w2-producers)",
    "tests/integration/test_trajectory_media_deletion.py":
        "in-transaction media retention into business trajectory payloads was removed (w2-producers)",
    "tests/integration/test_trajectory_media_dispatch.py":
        "recording-only media downloads and business trajectory payloads were removed (w2-producers)",
    "tests/integration/test_trajectory_read_races.py":
        "legacy recorder append and business trajectory tables were removed (w2-producers)",
    "tests/integration/test_trajectory_responses_title.py":
        "reads business trajectory tables and the legacy recorder (w2-producers)",
    "tests/integration/test_trajectory_storage.py":
        "tests the in-transaction recorder, stream receipts and business trajectory storage (w2-producers)",
    "tests/integration/test_trajectory_stream_redaction.py":
        "reads business trajectory tables, exports and payloads of the legacy recorder (w2-producers)",
    "tests/integration/test_trajectory_tool_output.py":
        "reads business trajectory tables and the legacy recorder (w2-producers)",
    "tests/integration/test_trajectory_video_billing.py":
        "reads business trajectory_events written by the legacy recorder (w2-producers)",
    "tests/unit/test_trajectory_session_runtime.py":
        "in-transaction recorder semantics on business trajectory tables (w2-producers)",
    "tests/unit/test_trajectory_emitter_sink.py::test_default_sink_is_db_and_invalid_values_fall_back":
        "wave 1 default sink db; wave 2 makes spool the default (w2-producers)",
    "tests/unit/test_trajectory_emitter_sink.py::test_spool_sink_record_stream_and_flush_never_touch_the_database":
        "patches legacy recorder functions that wave 2 removed (w2-producers)",
    "tests/unit/test_trajectory_emitter_sink.py::test_spool_sink_record_with_db_waits_for_commit_without_trajectory_sql":
        "patches the legacy append_events_in_tx that wave 2 removed (w2-producers)",
    "tests/unit/test_trajectory_emitter_sink.py::test_db_sink_keeps_the_legacy_recorder_path":
        "the legacy db sink path was removed in wave 2 (w2-producers)",
    "tests/unit/test_trajectory_emitter_commit.py::test_coexists_with_the_legacy_recorder_commit_listeners":
        "the legacy recorder commit listeners were removed with the db sink (w2-producers)",
    "tests/unit/test_trajectory_runtime.py::test_each_adapter_dispatch_has_identity_and_persists_chunks_before_delivery":
        "its fixture resolves chunk receipts asynchronously; producers no longer wait on receipts (w2-producers)",
    "tests/unit/test_trajectory_runtime.py::test_stream_reader_can_fill_one_batch_before_first_receipt":
        "chunk delivery no longer waits for receipts (w2-producers)",
    "tests/unit/test_run_fencing_boundaries.py::test_revoked_run_ends_as_an_abort_with_its_tool_recorded_cancelled[recording-on]":
        _BUSINESS_EVENTS,
    "tests/unit/test_run_fencing_delegation.py::test_cron_timeout_interrupts_the_run_and_releases_its_lease[recording-on]":
        _BUSINESS_EVENTS,
    "tests/unit/test_run_fencing_execution.py::test_poisoned_lease_is_released_quietly_by_recovery_and_by_new_input":
        _BUSINESS_EVENTS,
    "tests/unit/test_run_fencing_execution.py::test_two_completed_runs_in_one_turn_finish_cleanly_with_one_turn_finished":
        _BUSINESS_EVENTS,
    "tests/unit/test_run_fencing_execution.py::test_superseded_run_gets_no_auth_prompt_and_its_late_job_result_is_flagged":
        _BUSINESS_EVENTS,
}
