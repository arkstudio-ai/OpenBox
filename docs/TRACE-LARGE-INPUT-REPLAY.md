# Large Trace inputs and checkpoint recovery

## Incident verified on 2026-09-17

The affected production session's checkpoint endpoint returned HTTP 413 under
the existing 8 MiB read budget. The browser classified this as a network error
and repeatedly tried the same checkpoint, leaving replay in the opening state.
The session header and record summaries remained available.

Separately, 142 `request.prepared` events were dropped at the producer's 32 MiB
raw-event limit. Each event was 46,845,802–47,047,679 bytes. Run and session scopes
each received a gap marker, accounting for 284 markers. These are genuine missing
input snapshots; the original discarded bodies cannot be reconstructed from the
Trace. Ingestion and projection were caught up, with no producer-loss or
quarantine errors in the inspected metrics.

## Changes

- A checkpoint 413 falls back to contiguous, paginated event replay. Historical
  seeks retain their original watermark. Existing adaptive event-page sizing
  and server read limits remain in force. An individual event that still returns
  413 produces an explicit size error and stops automatic retries.
- Images resolved from business assets are replaced only in the captured request
  with an internal `$asset_media` marker containing asset identity, object key,
  SHA-256, byte count and media type. The actual provider request keeps its image
  bytes. URI and Anthropic base64 forms are supported; unbound media stays verbatim.
- The worker translates that marker into the existing `$media` payload reference,
  checks ownership, deduplicates asset references and honors source deletion.
  This happens before JSON content addressing, without copying image bytes to
  the spool or uploading another blob. Metadata lag is allowed only for an object
  in the recording user's asset namespace, bound to its source asset for deletion.
- Historical gap markers remain intact. Fixing future recording cannot restore
  input events discarded before they reached the spool.

No database migration or read/queue limit increase is required. Release the
worker before the backend so it understands compact media markers before new
producers write them. The frontend recovery is compatible with the old backend.

## Verification

- A read-only production replay at watermark 7,533 read 16 pages successfully,
  with 32,116,597 response bytes in total and a largest page of 5,836,264 bytes.
  Server-side elapsed time was 6.41 seconds, including 0.2 seconds between pages.
  This is a single-session measurement, not a concurrency benchmark.
- Regression coverage includes a 48 MiB multimodal request captured as less than
  8 KiB without altering provider inputs or issuing business SQL; marker ingestion,
  deduplication, foreign-reference rejection and deletion with delayed metadata;
  snapshot fallback, historical seeks and an oversized individual event.

The 32 MiB event limit still protects the business process from other oversized
unbound inputs. Such losses remain explicitly recorded as gaps.
