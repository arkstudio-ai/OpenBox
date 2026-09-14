# Wave 2 integration (`integ/wave2`)

Base `7c6b5ba`. SPEC.md stays authoritative; wave-1 NOTES.md decisions were followed.

## Merges

| # | source | commit | merge |
|---|---|---|---|
| 1 | w2-ingest (implementation) | `486e724` | `97e8bbb` |
| 2 | w2-producers (verified) | `dec1ccb` | `064b323` |
| 3 | w2-service (verified) | `86a2618` | `55421d0` |
| 4 | w2-projection (verified) | `89fc6f3` | `359ff2d` |
| 5 | w2-archive (verified) | `8a8ae7e` | `4196187` |
| 6 | w2-ingest verified fixes (`wp/w2-ingest-verified-r2`) | `bf7b74f` | `1d07011` |
| 7 | `fix/oss-file-already-exists` | `1587385` | `e789a70` |

Integration commits: `04f5561` wiring and legacy sink removal, `c48e1e4` unreachable `TrajectoryError` handlers,
`f324377` end-to-end and worker process tests, `ed7c3e7` projection object guard, and this report.

## Conflicts
- `backend/tests/conftest.py` (merges 3-5): kept the w2-producers hook set, which matches exact, parameter-less and
  module ids and collects a quarantined module without importing it. The other packages' second
  `pytest_collection_modifyitems` would have replaced it and was dropped.
- `backend/tests/legacy_trajectory_quarantine.py` (merges 3-5): union. Each package's registry is kept verbatim
  (`_PRODUCERS`, `_SERVICE`, `_PROJECTION`, `_ARCHIVE`) and `_union` keeps every reason of an id listed twice.
- `backend/trajectory/worker/services.py` (merge 6): the integration's direct service constructors plus the verified
  `ObjectGuard` and `GuardedGcBlobStore`.
- `backend/tests/unit/test_worker_meta.py` (merge 6): the `mark_asset_deleted` replica test plus the new
  `parse_time` overflow test; the stale `meta.revoke_asset` assertion was dropped.

## Wiring
- `WorkerServices` constructs `RetentionService`, `ProjectionService`, `ArchiveService` and `ExportService`
  (with `owner_id`) directly. `start()` does not block the lifespan. `run_once()` and `drain(include_archive=...)`
  drive the real services. GC deletes go through `GuardedGcBlobStore`, and ingest and projection share
  `object_guard`.
- Projection `project()` and `maybe_checkpoint()` / `build_checkpoint()` hold `object_guard.shared()` from their
  look at stored values, through the uploads, to the commit of the payload rows, as ingest does.
- Controls:
  - `session.deleted` goes through `RetentionService.tombstone`.
  - `asset.deleted` goes through `meta.mark_asset_deleted` (replica) and `lifecycle.revoke_asset`, the only
    revocation; `meta.revoke_asset` is removed. A copy outside the trajectory namespace is kept with a warning
    instead of failing the ingest batch.
- Asset reader:
  - The worker app installs `create_app(asset_reader=...)`, else OSS per `TRAJECTORY_OSS_INTERNAL` (default true).
  - Embedded and desktop mode install `start_embedded_worker(asset_reader=...)`, else OSS over the public endpoint
    unless `TRAJECTORY_OSS_INTERNAL` is true.
  - An explicitly passed blob store becomes the process store, so routes and exports read what the services write.
- Routes call `payload.payload_meta` and `payload.read_blob` directly and await `export.create_export`.
- Removed legacy sink code:
  - `payload.py`: store/drain/archive-worker helpers and `get_storage`
  - `repository.project_events_in_tx`
  - `lifecycle.delete_trajectory_in_tx` and `purge_deleted_content`
  - the export build/resume/stop hooks
  - `recorder.event_dict` and `types.recording_boundary`

  Only quarantined tests, `scripts/trajectory_dev_server.py` and `trajectory/benchmark*.py` (wave 3) still name them.
- `TrajectoryError`: 39 handlers narrowed to `RunRevoked`. 11 branches that only handled `TrajectoryError` and the
  `IdempotencyConflict` guard in `question.runtime` were removed, because no business path can raise it any more.
- `WorkerSettings.export_max_bytes` reads `TRAJECTORY_EXPORT_MAX_BYTES` and is added to SPEC §13.
- Checked and needing no change: settings names, `read_payload` / `read_blob` / `payload_meta` signatures, export
  sync/async, SPEC §8.13 metric names, `notify.publish_available`, the `main.py` lifespan (emitter start and
  `close(5.0)`, meta sync, embedded worker), `api/internal.py`, the removed admin modules, and the `define_tool`
  final flag.

## Edits to w2-ingest-owned files
- `worker/services.py`: direct constructors, verified guard wiring kept, guard passed to projection.
- `worker/meta.py`: `revoke_asset` replaced by `mark_asset_deleted` (replica only).
- `worker/ingest.py`: `asset.deleted` calls `lifecycle.revoke_asset` and appends `artifact.removed` from its result.
- `worker/settings.py`: `export_max_bytes`.
- `tests/unit/test_worker_meta.py` and `tests/unit/test_worker_services.py` follow these edits.

## Deviations
1. Embedded asset reads default to the public OSS endpoint (noted in the SPEC §13 row).
2. An explicitly passed blob store is installed as the process store for the app's lifetime.
3. `lifecycle.revoke_asset` keeps a foreign-namespace copy instead of raising inside ingest's transaction.
4. Tests of the removed export hooks and route shim were deleted; the duplicate-build test now targets `ExportService`.

## Tests
Final head:

| command | result |
|---|---|
| `cd backend && uv run pytest tests/unit -q` | 3146 passed, 41 skipped, 0 failed |

The 41 unit skips are 18 quarantined items, 22 PostgreSQL variants and one missing `systemd-analyze`.

Earlier gates, at `f324377`, were not rerun after merges 6 and 7 or the projection guard (orchestrator instruction):

| command | result |
|---|---|
| `uv run pytest tests/integration -q` | 83 passed, 71 skipped |
| PostgreSQL 16 container `obx-integ-w2-pg` (stopped): `TRAJECTORY_TRACE_TEST_DATABASE_URL` suites | 63 passed |
| `OBX_QUESTION_TEST_DATABASE_URL` suites | 200 passed, 9 skipped (quarantined) |
| `PUSH_TEST_DATABASE_URL` | 1 passed |
| `BILLING_TEST_POSTGRES_URL` | 5 passed |
| `VIDEO_TEST_POSTGRES_URL` | 14 passed |
| `ADMIN_TEST_DATABASE_URL` runner | 57 passed |
| `cd frontend-v2 && npm ci && npx vitest run` | 124 files, 859 tests passed |

`tests/integration/test_trajectory_pipeline_e2e.py` passed on SQLite and on PostgreSQL, and so did the worker process
test. Timing-sensitive tests were repeated 3 to 5 rounds without a failure.

## Quarantine
88 entries in 19 files, 13 of them whole modules. The audit ran them with the registry emptied: every entry fails for
removed legacy semantics (legacy recorder imports, retired business trajectory tables, removed recorder hooks, chunk
receipts, a raising recorder). None was removed; integration added 2.

| file | entries |
|---|---|
| integration `test_trajectory_agent_loop` | 4 |
| integration `test_trajectory_auxiliary` | 3 |
| integration `test_trajectory_boundaries` | 6 |
| integration `test_trajectory_media_deletion` | 3 |
| integration `test_trajectory_media_dispatch` | 6 |
| integration `test_trajectory_process_crash` | 2 |
| integration `test_trajectory_read_races` | 15 |
| integration `test_trajectory_responses_title` | 3 |
| integration `test_trajectory_storage` | 11 |
| integration `test_trajectory_stream_redaction` | 13 |
| integration `test_trajectory_tool_output` | 6 |
| integration `test_trajectory_video_billing` | 1 |
| unit `test_run_fencing_boundaries` | 1 |
| unit `test_run_fencing_delegation` | 1 |
| unit `test_run_fencing_execution` | 4 |
| unit `test_trajectory_emitter_commit` | 1 |
| unit `test_trajectory_emitter_sink` | 4 |
| unit `test_trajectory_runtime` | 3 |
| unit `test_trajectory_session_runtime` | 1 |

## Open issues for wave 3
- Port the 88 quarantined tests. Rewrite `scripts/trajectory_dev_server.py` and `trajectory/benchmark*.py`.
- Rerun the integration, PostgreSQL and frontend gates on the final head.
- `ObjectGuard` is process-local, so `tools/migrate_legacy.py` uploads outside it. Run the converter with recording
  disabled, as its release step says.
- A tombstone publishes the deleted notification twice (ingest and lifecycle).
- Export downloads are validated twice, in different orders (routes and `export.validate_export`).
- Embedded SQLite trace databases have no upgrade path after `t0001`.
- With `REDIS_URL` the worker decodes every business bus event.
- `/api/internal/trajectory/*` is reachable through nginx `/api/`.
- Refused audit rows are retried forever.
- Memory and latency:
  - expanded states at head
  - export ZIPs held in memory
  - whole asset downloads
  - replay while projection lags
  - `unsupported_events` grows without bound
- `frontend-v2/e2e/helpers/trajectory-server.ts` still has to model refs, blobs and `?meta=1`.
- `TRAJECTORY_OSS_INTERNAL` must be false on the AWS development deployment.
