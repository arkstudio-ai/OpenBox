# w3-spool-blobs report

The spool blob code (spool format v2) is done and committed, but I wrote no new unit tests: your message came before I got to them. So the seven behaviours on the original list (dedup, mtime refresh, missing or corrupt blob becoming a gap, safe cleanup, blob bytes in the budget, mixed v1/v2 files, same sha as inline content) are not tested. I also did not run the full unit suite. I ran only the three test files I changed: 33 passed, 0 failed, 0 skipped in the end. The first run had 1 failure, in a test I had just edited; I fixed that test and reran its file, which passed 11 of 11.

- **Branch:** `wp/w3-spool-blobs`
- **Head:** `9512910195c095797fc997e3d0bce8744067215c` (base `05bc977`, 2 commits, not pushed, tree clean)

## Summary
- **Emitter (writer thread only):** these values move into `blobs/<sha256>`:
  - the `request.prepared` `input.system`, `instructions` and `tools`;
  - each item of `input.messages`, or of a list-valued `input.input`, larger than `TRAJECTORY_SPOOL_BLOB_MIN_BYTES` (default 1024);
  - any other value inside `data` over 16 KiB.

  A blob is written as temp file, fsync, rename; if it already exists its mtime is refreshed, at most once a minute per writer. Blob bytes count toward `spool_max_bytes`. If a blob can't be stored, the event is written inline. `emit()` is unchanged.
- **Format:** only event lines that reference blobs are version 2; every other line stays version 1. The worker accepts versions 1 and 2. The budget file, `producer.json` and `worker.json` stay at version 1.
- **Worker reading:** `read_batch` puts the blob contents back into the line before ingest decodes it, so sanitize, the event hash and content addressing see exactly the inline content. A missing or corrupt blob turns that line into a `gap` control (reason `spool_blob_missing` or `spool_blob_corrupt`) and the rest of the file is ingested normally.
- **Cleanup:** at most once a minute, after a complete scan, `scan_spool` deletes blobs older than the oldest remaining data file minus 600 s. Each candidate is renamed aside and rechecked first, so a blob the writer has just reused is kept.
- **Deterministic tests:** the two writer tests that failed under load in the wave-2 checks (`test_spool_budget_drops_…` and `test_torn_tail_…`) now wait until the writer is idle before emitting. The budget test also samples disk usage on every writer cycle instead of relying on a timer.
- **SPEC §3** now describes format v2.

## Changed files
- `backend/trajectory/emitter.py`
- `backend/trajectory/spool.py`
- `backend/trajectory/worker/spool_reader.py`
- `backend/trajectory/worker/ingest.py` (outside my ownership, see deviations)
- `docs/trajectory-rearch/SPEC.md`
- `backend/tests/unit/test_trajectory_emitter_writer.py`
- `backend/tests/unit/test_trajectory_spool.py`
- `backend/tests/unit/test_worker_ingest_spool.py` (outside my ownership, see deviations)

## Tests
`cd backend && uv run pytest tests/unit/test_trajectory_spool.py tests/unit/test_worker_ingest_spool.py tests/unit/test_trajectory_emitter_writer.py -q`
- Final result: 33 passed, 0 failed, 0 skipped.
- Before any change, the full suite was 3146 passed, 41 skipped.

## New settings
- `TRAJECTORY_SPOOL_BLOB_MIN_BYTES`, default 1024, backend. It is read in `emitter.get_emitter()`, so `config.py` is untouched.
- The 16 KiB value limit, the 60 s refresh, the 600 s margin and the 60 s sweep interval are constants in `spool.py` and `spool_reader.py`, not settings.

## Deviations
1. **`ingest.py` (owned by w3-harden-worker), two lines:** item size now uses the size after blobs are put back. Without this, large content would skip the worker's size checks and be stored inline, and the per-user byte budget would undercount. One docstring was also updated.
2. **`test_worker_ingest_spool.py` (owned by w3-harden-worker):** the unsupported-version case now uses `"v":3`, because version 2 is valid.
3. **Extra keys moved:** `instructions` and list-valued `input` items move along with `system` and `messages`, matching the keys the worker already content-addresses.
4. **Size measure:** compact orjson size is used instead of canonical JSON; the lengths are practically the same.
5. **Nesting:** a value that already contains a blob reference stays inline, so blobs never contain references.
6. **Cleanup placement:** the sweep runs inside `scan_spool`, which avoids another hook in `ingest.py`.
7. **Emitter stats:** three new keys, `blobs_written`, `blob_bytes_written` and `blob_errors`.

## Open issues
- **Deploy order:** workers must be deployed before backends. An older worker quarantines files that contain version 2 lines.
- **Metrics:** blob bytes are not in the worker's `spool_bytes` gauge, and `deploy/gw2/scripts/push-metrics.sh` counts only `*.jsonl` files. The spool alarm therefore under-reports (w3-ops / w3-harden-worker).
- **Blob-line gaps:** they are counted with the ordinary gaps; there is no separate metric.
- **Test helper:** `tests/unit/trajectory_producer_support.py` reads raw spool lines, so a future producer test that emits large `request.prepared` inputs would see `$blob` references.
- **Other emitter tests:** `test_enospc_…` has the same startup race as the two fixed tests and was left as it is.
