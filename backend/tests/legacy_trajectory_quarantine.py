"""Legacy trajectory tests quarantined by the re-architecture (wave 2).

Node id -> one-line reason; tests/conftest.py skips these ids. Every entry
exercises semantics wave 2 removes: the in-transaction recorder, the
business-database trajectory tables or the old in-process admin API.
Wave 3 must empty this registry.
"""

QUARANTINE = {
    "tests/integration/test_trajectory_agent_loop.py::test_pause_resume_captures_new_baseline_and_reads_attachments_before_write_lock":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_agent_loop.py::test_real_compaction_prunes_effective_context_but_preserves_replay":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_agent_loop.py::test_user_input_through_application_agent_loop_and_auxiliary_request":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_auxiliary.py::test_image_edit_captures_actual_parameters_owned_versions_and_existing_bill":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_boundaries.py::test_late_job_callbacks_use_submission_identity_and_cannot_revive_deleted_trace":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_boundaries.py::test_readonly_socket_has_no_execution_side_effect_and_rechecks_live_role":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_boundaries.py::test_retained_attachment_reuse_and_explicit_delete":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_media_deletion.py::test_inline_and_derived_media_have_no_independent_copy_after_source_deletion[False]":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_media_deletion.py::test_inline_and_derived_media_have_no_independent_copy_after_source_deletion[True]":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_media_dispatch.py::test_derived_media_remains_revocable_with_original_video":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_media_dispatch.py::test_video_dispatch_captures_final_wire_and_poll_does_not_create_request[ark]":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_media_dispatch.py::test_video_dispatch_captures_final_wire_and_poll_does_not_create_request[bossip]":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_media_dispatch.py::test_video_dispatch_captures_final_wire_and_poll_does_not_create_request[sd2]":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_media_dispatch.py::test_video_dispatch_captures_final_wire_and_poll_does_not_create_request[task]":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_process_crash.py::test_sigkill_preserves_committed_tool_output_and_never_reexecutes":
        "crash benchmark replays business-database trajectory tables through repository.state_at",
    "tests/integration/test_trajectory_read_races.py::test_admin_json_and_ticket_are_not_cacheable":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[asset_deleted-410-export]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[asset_deleted-410-payload]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[payload_deleted-410-export]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[payload_deleted-410-payload]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[revoked-401-export]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[revoked-401-payload]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[role-403-export]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[role-403-payload]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[root_deleted-404-export]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_download_rechecks_during_blob_read[root_deleted-404-payload]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_record_detail_accepts_encoded_path_identity[file:/workspace/100%/literal%2Fname.md]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_record_detail_accepts_encoded_path_identity[file:/workspace/\\u62a5\\u544a ?draft#1.md]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_read_races.py::test_record_detail_accepts_encoded_path_identity[file:/workspace/report.md]":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_responses_title.py::test_responses_native_and_fallback_persist_actual_dispatches[fallback]":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_responses_title.py::test_responses_native_and_fallback_persist_actual_dispatches[native]":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_storage.py::test_admin_read_paths_live_role_and_target":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_storage.py::test_checkpoint_pages_reuse_unchanged_history":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_storage.py::test_child_ownership_deleted_session_unknown_result":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_storage.py::test_durable_payload_staging_archives_without_transaction_lock":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_storage.py::test_export_shutdown_resumes_and_deletion_wins":
        "old in-process export task over business trajectory tables; exports move to the trajectory worker",
    "tests/integration/test_trajectory_storage.py::test_historical_checkpoint_and_stream":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_storage.py::test_malformed_cursors_are_validation_errors":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_storage.py::test_pause_resume_baseline_and_export_gaps":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_storage.py::test_payload_history_export_deletion":
        "drives the old in-process admin API over business tables; admin reads now serve the trace database",
    "tests/integration/test_trajectory_storage.py::test_read_committed_cache_race_does_not_leak_future":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_stream_redaction.py::test_fragmented_request_credentials_never_enter_retained_views[litellm_arguments-inline]":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_stream_redaction.py::test_fragmented_request_credentials_never_enter_retained_views[litellm_arguments-payload]":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_stream_redaction.py::test_fragmented_request_credentials_never_enter_retained_views[litellm_reasoning-inline]":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_stream_redaction.py::test_fragmented_request_credentials_never_enter_retained_views[litellm_reasoning-payload]":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_stream_redaction.py::test_fragmented_request_credentials_never_enter_retained_views[litellm_text-inline]":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_stream_redaction.py::test_fragmented_request_credentials_never_enter_retained_views[litellm_text-payload]":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_stream_redaction.py::test_fragmented_request_credentials_never_enter_retained_views[responses_arguments-inline]":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_stream_redaction.py::test_fragmented_request_credentials_never_enter_retained_views[responses_arguments-payload]":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_stream_redaction.py::test_fragmented_request_credentials_never_enter_retained_views[responses_text-inline]":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_stream_redaction.py::test_fragmented_request_credentials_never_enter_retained_views[responses_text-payload]":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_stream_redaction.py::test_redaction_state_is_not_shared_between_requests":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_stream_redaction.py::test_tool_cumulative_output_redaction_resets_for_each_call":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_tool_output.py::test_bash_redaction_spans_chunks_after_chat_preview_budget":
        "reads business-database payload rows through payload reads that now serve the trace database",
    "tests/integration/test_trajectory_tool_output.py::test_bash_stream_over_preview_limit_is_replayable_before_and_after_finish":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_tool_output.py::test_mcp_internal_truncation_preserves_full_observed_public_body[False]":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_tool_output.py::test_mcp_internal_truncation_preserves_full_observed_public_body[True]":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
    "tests/integration/test_trajectory_tool_output.py::test_web_fetch_retains_processed_body_before_tool_truncation":
        "passes business-database trajectory rows to repository reads, which now read the trace database",
}
