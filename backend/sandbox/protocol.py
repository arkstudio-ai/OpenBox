"""Version-independent protocol contract for the WUYING Action Server."""

REQUIRED_ACTION_SERVER_CAPABILITIES = frozenset({
    "desktop_lease_v1",
    "execution_trace_v1",
    "run_fencing_v1",
    "run_lease_receipt_v2",
    "catalogue_projection_v1",
    "tenant_catalogue_scopes_v1",
    "skill_archive_create_only_v1",
    "skill_restore_fence_v1",
    "skill_archive_bounded_v1",
    "confined_file_delete_v1",
    "sensitive_search_filter_v1",
    "confined_path_resolve_v1",
    "mcp_desired_state_v1",
    "mcp_supervisor_v1",
    "terminal_project_cwd_v1",
})
