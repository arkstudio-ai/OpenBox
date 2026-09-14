"""Legacy trajectory tests skipped while the re-architecture lands (wave 2).

Each entry is a pytest node id with the reason its test exercises semantics
wave 2 removed on purpose: the in-transaction recorder, the business-database
trajectory tables or the old in-process admin API. A module id skips every
test in it (the module is not imported); a parametrized test may be listed
without its parameters. tests/conftest.py applies the skips. Wave 3 ports or
rewrites these tests and must leave this registry empty.

Every wave-2 package kept its own registry; ``QUARANTINE`` is their union, and
a test listed by several packages keeps each package's reason.
"""

_BUSINESS_EVENTS = "asserts facts read from the business trajectory_events table, retired in wave 2 (w2-producers)"

_PRODUCERS: dict[str, str] = {
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

_WORKER_API = "admin HTTP/WS API moved to the trajectory worker (SPEC 8.12); covered by tests/unit/test_worker_*"
_READ_RACES = "read-race matrix reproduced against the worker app in tests/unit/test_worker_routes_read_races.py"

_SERVICE: dict[str, str] = {
    # w2-service: the admin HTTP/WS API, exports and the archive task moved to the trajectory worker.
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

_PAYLOAD_ROWS = "reads business-database payload rows through payload reads that now serve the trace database"
_REPOSITORY_ROWS = "passes business-database trajectory rows to repository reads, which now read the trace database"
_ADMIN_READS = "drives the old in-process admin API over business tables; admin reads now serve the trace database"

_PROJECTION: dict[str, str] = {
    # w2-projection: payload and repository reads serve the trace database.
    "tests/integration/test_trajectory_agent_loop.py::test_pause_resume_captures_new_baseline_and_reads_attachments_before_write_lock":
        _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_agent_loop.py::test_real_compaction_prunes_effective_context_but_preserves_replay":
        _REPOSITORY_ROWS,
    "tests/integration/test_trajectory_agent_loop.py::test_user_input_through_application_agent_loop_and_auxiliary_request":
        _REPOSITORY_ROWS,
    "tests/integration/test_trajectory_auxiliary.py::test_image_edit_captures_actual_parameters_owned_versions_and_existing_bill":
        _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_boundaries.py::test_late_job_callbacks_use_submission_identity_and_cannot_revive_deleted_trace":
        _REPOSITORY_ROWS,
    "tests/integration/test_trajectory_boundaries.py::test_readonly_socket_has_no_execution_side_effect_and_rechecks_live_role":
        _ADMIN_READS,
    "tests/integration/test_trajectory_boundaries.py::test_retained_attachment_reuse_and_explicit_delete":
        _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_media_deletion.py::test_inline_and_derived_media_have_no_independent_copy_after_source_deletion[False]":
        _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_media_deletion.py::test_inline_and_derived_media_have_no_independent_copy_after_source_deletion[True]":
        _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_media_dispatch.py::test_derived_media_remains_revocable_with_original_video":
        _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_media_dispatch.py::test_video_dispatch_captures_final_wire_and_poll_does_not_create_request[ark]":
        _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_media_dispatch.py::test_video_dispatch_captures_final_wire_and_poll_does_not_create_request[bossip]":
        _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_media_dispatch.py::test_video_dispatch_captures_final_wire_and_poll_does_not_create_request[sd2]":
        _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_media_dispatch.py::test_video_dispatch_captures_final_wire_and_poll_does_not_create_request[task]":
        _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_process_crash.py::test_sigkill_preserves_committed_tool_output_and_never_reexecutes":
        "crash benchmark replays business-database trajectory tables through repository.state_at",
    "tests/integration/test_trajectory_read_races.py::test_admin_json_and_ticket_are_not_cacheable": _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[asset_deleted-410-export]": _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[asset_deleted-410-payload]": _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[payload_deleted-410-export]": _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[payload_deleted-410-payload]": _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[revoked-401-export]": _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[revoked-401-payload]": _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[role-403-export]": _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[role-403-payload]": _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[root_deleted-404-export]": _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[root_deleted-404-payload]": _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_record_detail_accepts_encoded_path_identity[file:/workspace/100%/literal%2Fname.md]":
        _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_record_detail_accepts_encoded_path_identity[file:/workspace/\\u62a5\\u544a ?draft#1.md]":
        _ADMIN_READS,
    "tests/integration/test_trajectory_read_races.py::test_record_detail_accepts_encoded_path_identity[file:/workspace/report.md]":
        _ADMIN_READS,
    "tests/integration/test_trajectory_responses_title.py::test_responses_native_and_fallback_persist_actual_dispatches[fallback]":
        _REPOSITORY_ROWS,
    "tests/integration/test_trajectory_responses_title.py::test_responses_native_and_fallback_persist_actual_dispatches[native]":
        _REPOSITORY_ROWS,
    "tests/integration/test_trajectory_storage.py::test_admin_read_paths_live_role_and_target": _ADMIN_READS,
    "tests/integration/test_trajectory_storage.py::test_checkpoint_pages_reuse_unchanged_history": _REPOSITORY_ROWS,
    "tests/integration/test_trajectory_storage.py::test_child_ownership_deleted_session_unknown_result": _REPOSITORY_ROWS,
    "tests/integration/test_trajectory_storage.py::test_durable_payload_staging_archives_without_transaction_lock": _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_storage.py::test_export_shutdown_resumes_and_deletion_wins":
        "old in-process export task over business trajectory tables; exports move to the trajectory worker",
    "tests/integration/test_trajectory_storage.py::test_historical_checkpoint_and_stream": _REPOSITORY_ROWS,
    "tests/integration/test_trajectory_storage.py::test_malformed_cursors_are_validation_errors": _ADMIN_READS,
    "tests/integration/test_trajectory_storage.py::test_pause_resume_baseline_and_export_gaps": _ADMIN_READS,
    "tests/integration/test_trajectory_storage.py::test_payload_history_export_deletion": _ADMIN_READS,
    "tests/integration/test_trajectory_storage.py::test_read_committed_cache_race_does_not_leak_future": _REPOSITORY_ROWS,
    **{f"tests/integration/test_trajectory_stream_redaction.py::test_fragmented_request_credentials_never_enter_retained_views[{case}-{storage}]":
       _REPOSITORY_ROWS if storage == "inline" else _PAYLOAD_ROWS
       for case in ("litellm_arguments", "litellm_reasoning", "litellm_text", "responses_arguments", "responses_text")
       for storage in ("inline", "payload")},
    "tests/integration/test_trajectory_stream_redaction.py::test_redaction_state_is_not_shared_between_requests": _REPOSITORY_ROWS,
    "tests/integration/test_trajectory_stream_redaction.py::test_tool_cumulative_output_redaction_resets_for_each_call": _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_tool_output.py::test_bash_redaction_spans_chunks_after_chat_preview_budget": _PAYLOAD_ROWS,
    "tests/integration/test_trajectory_tool_output.py::test_bash_stream_over_preview_limit_is_replayable_before_and_after_finish":
        _REPOSITORY_ROWS,
    "tests/integration/test_trajectory_tool_output.py::test_mcp_internal_truncation_preserves_full_observed_public_body[False]":
        _REPOSITORY_ROWS,
    "tests/integration/test_trajectory_tool_output.py::test_mcp_internal_truncation_preserves_full_observed_public_body[True]":
        _REPOSITORY_ROWS,
    "tests/integration/test_trajectory_tool_output.py::test_web_fetch_retains_processed_body_before_tool_truncation": _REPOSITORY_ROWS,
}

_ADMIN_API_EXPORT = ("w2-archive: exports via the in-process admin API on business-database trajectory tables; "
                     "the worker's ExportService builds them from the trace database")
_BUSINESS_EXPORT = ("w2-archive: create_export/build_export on business-database trajectory tables; "
                    "exports now live in the trace database")

_ARCHIVE: dict[str, str] = {
    # w2-archive: exports are built by the worker's leased ExportService from the trace database.
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


def _union(*registries: dict[str, str]) -> dict[str, str]:
    """One registry; a node id listed by several packages keeps every distinct reason."""
    merged: dict[str, str] = {}
    for registry in registries:
        for node_id, reason in registry.items():
            previous = merged.get(node_id)
            if previous is None:
                merged[node_id] = reason
            elif reason not in previous:
                merged[node_id] = f"{previous}; {reason}"
    return merged


_INTEGRATION: dict[str, str] = {
    # integ/wave2: business code can no longer receive a TrajectoryError, so its dead handlers were removed.
    "tests/unit/test_trajectory_runtime.py::test_failed_recording_never_dispatches_provider":
        "injects a raising recorder to stop provider dispatch; wave 2 recording is fail-open (SPEC §0.2) and the "
        "handler that re-raised the error was removed (integ/wave2)",
    "tests/unit/test_run_fencing_execution.py::"
    "test_recording_failure_while_applying_answers_is_retried_not_a_resume_failure":
        "injects RecordingError while answers are applied; wave 2 recording never raises and the retry branch for it "
        "was removed (integ/wave2)",
}

QUARANTINE: dict[str, str] = _union(_PRODUCERS, _SERVICE, _PROJECTION, _ARCHIVE, _INTEGRATION)
