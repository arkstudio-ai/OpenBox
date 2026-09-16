# Wave 1 outcomes and contract decisions for wave 2

All six wave-1 packages were implemented and independently verified (verdict `fixed` for each: verifiers found and fixed real defects). Full reports: `reports/wave1/<id>.json`. This file pins the decisions that wave-2 packages must follow. Where it refines SPEC.md, this file wins.

## Contract decisions

1. **Blob endpoint.** `GET /api/admin/trajectories/sessions/{sid}/blobs/{sha256}?through_seq=H` returns the referenced JSON value itself as the body (`application/json`), headers `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`. 404 = unknown or not visible at H, 410 = deleted/expired, 409 = corrupt.
2. **Payload meta.** `GET .../payloads/{payload_id}?meta=1[&through_seq=H]` returns `{"payload_id","availability","media_type","size_bytes","sha256"}` with the same visibility and status rules as the byte download.
3. **`expand=refs`.** On `/records/{record_id}` only `$ref` values stay unexpanded, both in `record.data` and in `record.events[*].data`. `$payload` and `$media` envelopes behave exactly as today. The default `expand=full` keeps today's response.
4. **Header capability.** `capabilities.refs = true` from the worker.
5. **Worker health and metrics formats** (consumed by `trajectory/ops/cms.py` and the drills):
   - `GET /health` → `{"status": "ok"|"degraded", "writer": bool, "db": bool, "spool": bool, "blob_store": bool, "version": str}`.
   - `GET /metrics` → `{"counters": {...}, "gauges": {...}, "uptime_seconds": number}` using the exact metric names of SPEC §8.13 (drills require `producer_loss_events`, `blob_put_failures`, `gaps_recorded`).
6. **Legacy converter URL.** `python -m trajectory.tools.migrate_legacy` accepts `--business-database-url` or env `TRAJECTORY_LEGACY_DATABASE_URL` (preferred in production so the password stays out of the process list).
7. **Final delta marker.** Producers set `data.final = true` on the last `request.delta` emitted by `RequestCapture.finish()` (the redaction-finalize delta) and on the final tool output; degraded budget mode keeps those.
8. **Emitter lifecycle.** The backend lifespan starts the emitter at startup (the first `get_emitter()` does file I/O and must not happen on a request path) and calls `get_emitter().close(5.0)` at shutdown.

## Carry-over notes per wave-2 package

- **w2-ingest**
  - Allocate `seq` only under the trajectory row lock (`SELECT ... FOR UPDATE` + single writer). PostgreSQL enforces `(trajectory_id, seq)` uniqueness only within one `recorded_on` partition.
  - `record_stream` receipts resolve to `None` with the spool sink; nothing may wait on receipts.
  - `TRAJECTORY_BLOB_FAULT` is implemented by `FaultInjectingBlobStore` (w1-storage); the worker must build its store through `get_blob_store()` so the drill works.
  - Blob keys: `trajectory_id` must not start with `_` and must not contain `/`; `sha256` is 64 lowercase hex; `delete_prefix` requires a prefix ending in `/`.
  - `OssError(status=0)` means a transport failure (retry). A 404 `NoSuchBucket` raises `OssError`, not `FileNotFoundError` (w1-storage verifier fix).
- **w2-archive**
  - Call the partition helpers only in their own short READ COMMITTED transaction with `SET LOCAL statement_timeout = '60s'`.
  - Handle `PartitionLockUnavailable` and `PartitionIsolationError`.
  - Creating a partition scans `trajectory_events_default`.
  - Align `backend/trajectory/ops/rebuild.py` (w1-ops) with the real trace schema and segment line format, and add a test that builds a segment with the archive code and rebuilds from it.
- **w2-service**
  - Wire the emitter lifecycle (decision 8), meta sync start/stop, and the embedded worker.
  - `trajectory/store/migrations/env.py` uses `asyncio.run()`. Embedded mode must not call alembic from inside the running event loop; use `TraceBase.metadata.create_all` for SQLite embedded mode, or run alembic in a thread.
  - Implement decisions 1-5.
- **w2-producers**
  - `tool/image_gen.py` and `tool/video_analyze.py` call `RequestCapture.start` directly; make sure the request-start fence (`assert_current("request")`, SPEC §9.2) applies on those paths.
  - Implement decision 7.
  - Remove the legacy recorder path; with it goes the recording-on deleted-session edge case where the legacy recorder raises `OwnershipError` from `run_loop`'s finally.
- **w3-tests**
  - Extend `frontend-v2/e2e/helpers/trajectory-server.ts` to model `capabilities.refs`, `expand=refs`, `/blobs` and `?meta=1`, and run the Playwright trajectory specs in refs mode.
  - Real-OSS checks (multi-page listing, quiet batch delete, forbid-overwrite conflict, NoSuchBucket, HEAD `x-oss-err`) happen in the production drill, not in tests.

## Known limitations accepted from wave 1 (documented, not blocking)
- A revoked run can no longer stamp `finish=aborted` on its last message or close its own running tool parts; this matches production behavior with recording on.
- A lease-expired zombie run whose lease was recovered by another process still passes the write rule; single-process production deployment makes this a non-issue today.
- Superseding a parent run does not revoke subagent children; children stop via in-process abort forwarding or the parent heartbeat plus the 10-second grace.
- Admin UI polling changes (10 s events poll while the socket is connected) are not gated on `capabilities.refs`.
- `TRAJECTORY_OSS_INTERNAL` defaults to `true`; the AWS development deployment must set it to `false`.
