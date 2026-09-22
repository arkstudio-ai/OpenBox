# Trajectory admin API, WebSocket, auth and frontend contract: map for the re-architecture

**Path abbreviations:** `AT` backend/api/admin_trajectories.py · `WS` backend/api/admin_trajectory_ws.py · `TA` backend/trajectory/auth.py · `RP` backend/trajectory/repository.py · `PL` backend/trajectory/payload.py · `EX` backend/trajectory/export.py · `RC` backend/trajectory/recorder.py · `PJ` backend/trajectory/projector.py · `MW` backend/auth/middleware.py · `FE/` frontend-v2/src/features/admin-trajectories/

## 0. Most consequential findings
1. **The browser does the projection.** The detail page loads a checkpoint (the full state, every record with its full `data`). It then loads every event with `include_data=true` and folds them locally (FE/api/sync.ts:228-338, FE/utils/projector.ts:469-480,610-633). The record table, timeline, statistics and agent tree are all computed client-side (FE/components/session/SessionWorkspace.tsx:83-94,159). Server `/records` is used only for the first-paint preview (session/PendingPosition.tsx:21, SummaryPreview.tsx:28-29).
2. **Large values travel in full, repeatedly.** Event `data` larger than `TRAJECTORY_INLINE_BYTES` (65536) is stored as `{$payload}` (RC:150-151). But `/events` expands it back inline (RP:231-232, PL:128-142), and so does `/checkpoint` (RP:181-189).
3. **The detail page reads few header fields.** It uses title, session_id, owner, workspace, coverage_start (fallback), running/recording status, trajectory_id, capabilities.export and committed_seq (SessionHeaderBar.tsx:71-121, RecordedSession.tsx:35,56,61,85, TrajectorySessionPage.tsx:82).
   - `statistics`, `agents`, `model` and `unsupported_events` from the header are unused.
   - `useSessionHeaderAt` is never called (FE/api/queries.ts:108-117).
4. **Polling is heavy.**
   - Event poll: every 1 s visible / 5 s hidden, even with the WS connected (FE/hooks/useTrajectorySync.ts:8-9,50-73).
   - Header: every 5 s, and each call writes an audit `view` row (queries.ts:95-105, AT:63).
   - List probe: every 5 s, each writing an audit `list` row (queries.ts:82-92, AT:53).
   - Payload body: re-downloaded every 15 s (queries.ts:24,231-246).
   - Record detail: re-read up to every 400 ms while live or playing (RecordInspector.tsx:46,64-65).
5. **Each WS notification recomputes the full session header** (WS:147 → RP:283-312). The client only uses `committed_seq` from it (FE/hooks/useTrajectorySocket.ts:34-39).
6. **Performance cliff:** if any payload in a trajectory is not `available`, fast paths and checkpoints are disabled. Header, records, detail and search then replay from seq 0 (RP:174-175,243,288,383).
7. **Reads depend on business tables:**
   - sessions, users, workspaces (RP:34-42,260-280,323-371)
   - file_assets (AT:167-171, PL:109-113)
   - users for admin checks (TA:42-51)
   - mobile_sessions (backend/auth/mobile.py:115-131)
   - audit_logs (backend/db/repository/audit_repo.py:11-20)
8. **WS close codes and idle timeouts.**
   - A rejection before `accept` becomes HTTP 403 (WS:69-75; uvicorn 0.40.0 `websockets_impl.py:286-293`). The browser then sees close code 1006, not 4401/4403.
   - The server sends no heartbeat (WS:154-157); the agent socket sends one every 25 s (backend/api/ws.py:437-442).
   - nginx `/ws/` has no `proxy_read_timeout` (default 60 s; frontend-v2/nginx.conf:50-59).
   - Likely effect (inferred, not verified in prod): idle viewer sockets drop about every 60 s, then reconnect with a new ticket, a new subscribe audit row and a header recompute.
9. **No production code writes `jwt_bl:{jti}`.** Only hashed refresh-token keys are written (backend/auth/routes.py:85-112). The jti checks (MW:58-61, WS:47-49) are exercised only by tests.

## 1. Registration, lifecycle, routing
| Item | Location |
|---|---|
| Routers included | backend/main.py:332-335 |
| HTTP router: prefix `/api/admin/trajectories`, `route_class=NoStoreRoute` | AT:19 |
| WS router (ticket POST and socket), NoStoreRoute | WS:19,25,65 |
| Startup: archive worker (archive payloads, build checkpoints, purge; every 5 s) | main.py:107-108; PL:238-260 |
| Startup: log flags; `resume_exports()` only if `admin_enabled()` | main.py:109-117; EX:131-136 |
| Startup: Redis bus (multi-user mode), channel `bus:events` | main.py:153-155; backend/bus/bus.py:29,65-95,166-168 |
| Shutdown: stop exports → recorder flush → stop archive worker, before DB/cache close | main.py:79-90,247-251 |
| Auth/ticket/blacklist init only with JWT_SECRET; otherwise a MemoryCache ticket store | main.py:40-57; backend/auth/__init__.py:9-16 |
| Container: single uvicorn process after `alembic upgrade head` | backend/Dockerfile:26 |
| nginx: `/api/` and `/ws/` → `${BACKEND_HOST}` (default backend:8080) | frontend-v2/nginx.conf:10-12,42-59; frontend-v2/Dockerfile:20-21 |
| Vite dev proxy: `/api`, `/ws` → :8080 | frontend-v2/vite.config.ts:42-45 |
| k8s ingress: `/api` → openbox-backend; `/ws` → openbox-backend-ws (same pods, 3600 s timeout) | k8s/base.yaml:240-262,270-305 |
| Env flags: `TRAJECTORY_ADMIN_ENABLED`, `TRAJECTORY_ADMIN_USER_IDS` (empty = everyone), `TRAJECTORY_RECORDING_ENABLED`, `TRAJECTORY_RECORD_USER_IDS` | backend/trajectory/config.py:10-24 |
| Frontend routes under `RequireAdmin`: `/app/admin/trajectories` and `.../sessions/:sessionId` (page keyed by sessionId, installs access watcher) | src/app/router/router.tsx:31-32,86-106; src/routes/admin/AdminTrajectoriesRoute.tsx:4-6; AdminTrajectorySessionRoute.tsx:10-15 |
| Viewer isolation: no agent socket, sandbox dialog, workbench, cron pill or credits | src/app/layouts/WorkspaceLayout.tsx:46,82,86,90-91,105 |

## 2. HTTP contract
### 2.1 Common rules
- **Auth** is the `require_trajectory_admin` dependency (TA:54-56; details in §4). Dependencies run before query validation, so 401, 403 and 404 (admin disabled) win over 422.
- **`errors` decorator** (AT:22-35):

  | Exception | Status | Body |
  |---|---|---|
  | `FileNotFoundError` | 410 | `{"detail":{"code":"trajectory_content_deleted","message"}}` |
  | `LookupError` (includes KeyError/IndexError) | 404 | `{"detail":"<str>"}` |
  | `CorruptContent` | 409 | `{"detail":{"code":"trajectory_corrupt","message"}}` |
  | Other `TrajectoryError` | 400 | `{"detail":{"code","message"}}`; codes `trajectory_invalid`, `trajectory_ownership`, `trajectory_idempotency_conflict`, `trajectory_recording_failed` (backend/trajectory/types.py:34-47,123-124) |
  | Pydantic validation | 422 | FastAPI default |

- **`Cache-Control: no-store`** is set on success, HTTPException and 422 responses (TA:15-28,55). Unhandled 500s do not get it.
- **Sequence numbers** are decimal strings in and out. Non-digits, or a value above the committed head, return 400 (types.py:72-78; RP:45-47).
- **Session lookup:** every sub-resource returns 404 "Session not found" (missing or `is_deleted`) or 404 "Session has not started recording" (RP:34-42).
- **`X-Workspace-Id`:** the SPA sends it (shared/api/http.ts:61-66); the server ignores it (tests/integration/test_trajectory_storage.py:140-141).
- **Frontend status mapping:**
  - 401/403 → denied, purge everything (FE/api/access.ts:25-27)
  - 404 → not recorded / not yet at this position
  - 410 → deleted
  - 409 → corrupt (FE/api/sync.ts:112-124; queries.ts:207-211)

### 2.2 Endpoints (prefix `/api/admin/trajectories`)
| # | Method, path | Params (validation) | Response | Extra errors | Audit / recheck | Frontend caller |
|---|---|---|---|---|---|---|
| 1 | GET `/sessions` (AT:42-54) | `user_id`; `user_query` (ILIKE username/email/id); `q` (ILIKE title/id); `workspace_id`; `status` (= coalesce(summary.running_status, session.status)); `recording_status`; `activity_from`/`activity_to` (datetime); `include_unrecorded`=false; `cursor`; `limit` 1..200 (default 50); `sort` ∈ {last_activity_desc, last_activity_asc} | `{items: SessionRow[], next_cursor, has_more}` (RP:378) | 400: bad sort, bad cursor, cursor from other filters (RP:318-319,357-366) | `admin.trajectory.list` `{user_id, workspace_id}` (AT:53) | queries.ts:48-92 (limit 50; probe limit 1) |
| 2 | GET `/sessions/{sid}` (AT:57-64) | `through_seq?` | SessionHeader (RP:283-312). Never recorded → 200 with `trajectory_id: null`, `recording_status: "not_recorded"`, seqs `"0"` | 404 session; 400 seq | `view` `{through_seq}` (AT:63) | useSessionHeader, 5 s |
| 3 | GET `/sessions/{sid}/events` (AT:67-74) | `after_seq`="0"; `until_seq?`; `limit` 1..2000 (500); `include_data`=true | `{events, from_seq, through_seq` (last seq in page, or after_seq)`, until_seq` (effective H)`, has_more, committed_seq}` (RP:236-238) | 409 sequence gap or missing tail (RP:227-235); 400 after > until | none | sync.ts:320-323,460-463; always sends `include_data=true` (endpoints.ts:116) |
| 4 | GET `/sessions/{sid}/records` (AT:77-85) | `through_seq?`; `before` (cursor bound to H); `limit` 1..500 (100); `kind`; `status`; `agent_id` | `{items: RecordSummary[]` (no data/blocks)`, next_cursor, has_more, through_seq, projector_version, unsupported_events}`; newest page first, items ascending (RP:381-425) | 400 cursor from another H | none | SummaryPreview only; no filters used |
| 5 | GET `/sessions/{sid}/records/{record_id:path}` (AT:88-94) | `through_seq?`; record_id is opaque (may contain `/`, `%`, Unicode); client encodes once (endpoints.ts:107) | `{record: TrajectoryRecord` with `as_of_seq`=H and `events[]` (record's events from start_seq..min(H, as_of))`, through_seq, projector_version}` (RP:428-444) | 404 "Record is not available at this position" | none | useRecordDetail (queries.ts:153-167) |
| 6 | GET `/sessions/{sid}/checkpoint` (AT:97-103) | `at_seq?` | `{checkpoint: null \| {through_seq, projector_version, state{projector_version, through_seq, records, unsupported_events, coverage_start}, digest}, through_seq: H}` (RP:188-189) | 409 digest mismatch; `null` when any payload is deleted (RP:174-175) | none | sync open (no `at_seq`) and seeks (sync.ts:235,417) |
| 7 | GET `/sessions/{sid}/search` (AT:106-113) | `q` 1..500 chars; `through_seq?`; `cursor` ([H, q, offset]); `limit` 1..200 (50) | `{items:[{record_id, seq` (= record as_of_seq)`, kind, preview` (JSON substring, 60 chars before / 180 after)`}], next_cursor, has_more, through_seq}`, ordered by (start_seq, record_id) (RP:447-467) | 400 cursor mismatch | none | useRecordSearch (queries.ts:174-191) |
| 8 | GET `/sessions/{sid}/payloads/{payload_id}` (AT:116-133) | `through_seq?` | Raw bytes with the payload's media type; headers no-store, nosniff, `attachment; filename="{payload_id}"` | 404 not at this position or wrong trajectory; 410 payload or source asset deleted; 409 blob missing, digest mismatch, changed during download (PL:102-125; AT:130-131) | audit `payload` `{payload_id, through_seq}` → `revalidate_viewer` → recheck row and sha256 in a new DB session (AT:125-131) | usePayload, useDownloadPayload |
| 9 | POST `/sessions/{sid}/export` (AT:140-150) | Body required: `{through_seq?: string}` | 202 `{export_id, status:"pending", through_seq, error:null, download_url:null}` (EX:36-38) | 400 seq | `export` `{through_seq, export_id}`; build runs as a BackgroundTask in the web process (AT:148-149) | useCreateExport |
| 10 | GET `/sessions/{sid}/exports/{id}` (AT:176-181) | — | Same shape. Status: pending, running, completed, failed or deleted. `error` = exception class name. `download_url` (API-relative) only when completed | 404 export not in this trajectory | none | useExportJob, polls 2 s |
| 11 | GET `.../exports/{id}/download` (AT:184-202) | — | Zip, `filename="{id}.zip"`, no-store, nosniff | 409 "Export is not ready" (string detail) or digest mismatch; 410 when a payload or asset was deleted after export creation (AT:162-173) | `download` `{export_id}`, then revalidate and recheck (AT:194-200) | useExportDownload |
| 12 | POST `/ticket` (WS:25-39) | Bearer token | `{ticket}` | 401 if auth is on and the token does not decode (WS:32-33) | none | shared/ws/client.ts:65-70 |

### 2.3 Response shapes (fields the frontend reads)
- **SessionRow** (RP:270-280; FE/types/protocol.ts:261-280):
  - Identity: session_id, user_id, trajectory_id, title, owner{user_id, username, email}, workspace{id, name}, workspace_id, model, agent.
  - Status: running_status; recording_status (`"paused"` when recording is disabled for the owner, `"not_recorded"` when there is no trajectory; RP:274).
  - Time and position: coverage_start, last_activity_at (summary value, else session.updated_at), committed_seq, projected_through_seq, through_seq.
  - statistics{request_count, tool_count, error_count, unknown_count, input_tokens|null, output_tokens|null, usage_complete, duration_ms: null, through_seq, coverage_start} (RP:263-269).
  - Table columns: SessionTable.tsx:36-146.
- **SessionHeader** = row fields plus:
  - statistics (with duration_ms)
  - agents[{agent_id, parent_agent_id, source_session_id, name, status, record_id}]
  - projector_version
  - capabilities{recording, admin_read: true, export: trajectory exists}
  - unsupported_events
  - When through_seq < head, running_status and model are recomputed from the state at that position (RP:302-311).
- **TrajectoryRecord** (PJ:83-90; protocol.ts:177-208):
  - Fields: record_id, kind, title, preview, result_preview, status, status_reason, 13 ID fields, start_seq, end_seq, as_of_seq, started_at, finished_at, duration_ms, timing_source, data, blocks[{block_id, type, text, chunk_index?}], usage.
  - Record ids: `request:`, `assistant:`, `system:{request_id}`, `tool:{call_id}`, `user:`, `run:`, `turn:`, `step:`, `agent:`, `interrupt:{event_id}`, `resume:`, `retry:`, `artifact:` (PJ:20-73,228-250).
- **Event** (RC:106-110): event_id, trajectory_id, user_id, session_id, context ID fields, source_session_id, seq (string), type, version, occurred_at, recorded_at, data. The client validates only seq, event_id, type, occurred_at, session_id and that data is an object (FE/utils/adapter.ts:105-117).
- **Content envelopes:**
  - `{$payload:{payload_id, sha256, size_bytes, media_type, availability[, reason]}}` (PL:68-71,135)
  - `$media` wrapper (protocol.ts:88-100)
  - Payload references get their availability rewritten to the current deletion state on read (PL:145-163).

### 2.4 Cursors and watermarks
- **Sessions cursor:** base64url of `[digest(filters+sort), last_activity (µs ISO), session_id]`, at most 8192 chars (RP:18-31,321-322,357-377). The frontend keeps it opaque, with a page trail, in the URL: `[A-Za-z0-9_-]+`, at most 16384 chars (FE/utils/params.ts:60-65,97-156).
- **Records `before`:** `[H, start_seq, record_id]` (RP:389-398).
- **Search cursor:** `[H, q, offset]` (RP:452-466).
- **Events:** paged by after/until seq; the client checks contiguity (sync.ts:127-139,452-472).

### 2.5 Database and blob reads per call
- **List:** one join over sessions, users, workspaces, left-joined trajectories and summaries (RP:323-370). `recording_status` goes through an env-dependent CASE (RP:346-352).
- **Header:** session, trajectory, summary, user, workspace, a deleted-payload probe, `data` of all agent/run records, and a cache-change probe. Fallback is `state_at`: all record rows, or checkpoint plus replay (RP:241-257,283-312).
- **Events:** session, trajectory and the event page. For each event, `expand` reads the payload row (possibly downloading the blob and hashing it), plus reference-availability queries (PL:117-163).
- **Record detail:** full `state_at`, plus that record's events in pages of 1000, expanded.
- **Checkpoint:** all record pages, downloaded 8 at a time, plus a digest check (PL:166-187).
- **Search:** full state, then `json.dumps` of every record.

## 3. WebSocket contract
- **URL:** `/ws/admin/trajectories?ticket=…`. The base is `VITE_API_URL` or the page origin, with a ws scheme (shared/config/env.ts:6-8; client.ts:108).
- **Ticket issue:** `create_ticket(user_id, "admin", client, sid, audience="admin_trajectories", auth_jti, auth_expires_at)`, stored as `ticket:{t}` with a 30 s TTL (WS:31-39; auth/ticket.py:23-40).
- **Ticket consume:** the audience must match. An atomic `incr ticket_claim:{t}` guarantees single use; the key is then deleted and `validate_ticket` runs (auth/ticket.py:43-68). Chat tickets cannot open this socket; a forged role claim cannot obtain a ticket (tests/integration/test_trajectory_boundaries.py:79-92).
- **Client ticket handling:** on 401, refresh the token once and retry. 403/404 are terminal (`__denied`); anything else reconnects with backoff (client.ts:77-103; FE/api/socket.ts:8-15).
- **`validate_viewer`** (WS:42-51) checks `assert_admin` (DB), the ticket's token expiry, `jwt_bl:{jti}` and mobile claims. It runs before accept (WS:72), before every send (WS:84-87), on every inbound frame (WS:103), and every 1 s (WS:154-157).
- **Close codes:**
  - 401 → 4401.
  - Everything else → 4403, including 403 and 404 (admin disabled).
  - A missing or invalid ticket → 4401 (WS:69-75,166-178).
  - A close before `accept` is sent to the browser as HTTP 403 and surfaces as 1006 (§0.8). Tests only assert the codes after accept (test_trajectory_boundaries.py:122-136).
  - The client treats 4401 and 4403 as terminal.

**Client → server** (events.ts:104-107):
| type | Fields | Server behaviour |
|---|---|---|
| `subscribe` | `session_id` (1..64 chars); `after_seq?` (ignored by server) | More than 16 distinct sessions → error `SUBSCRIPTION_LIMIT`. Header lookup failure → error `SESSION_NOT_FOUND` with session_id. First subscribe per connection writes audit `trajectory.subscribe` (resource_id = trajectory_id; details owner_user_id, session_id, through_seq) directly, not best-effort. Replies `subscribed` (WS:115-133) |
| `unsubscribe` | `session_id` | Replies `unsubscribed{session_id}` (WS:111-114) |
| `ping` | — | Replies `pong{}`. The frontend never sends ping |
| anything else | — | error `READ_ONLY`; non-JSON or non-object frames → error `INVALID_MESSAGE` (WS:99-106,134-136) |

**Server → client** frames are `{type, data}` (events.ts:86-102):
- `subscribed` and `trajectory.available` carry exactly `{user_id, owner_user_id, session_id, trajectory_id, committed_seq}`, built from a freshly computed header (WS:59-62). The exact key set is asserted in test_trajectory_boundaries.py:118-119.
- `unsubscribed`, `pong` and `error{code, message?, session_id?}` exist, but the frontend registers no handler for `error` or `pong` (useTrajectorySocket.ts:32-40).

**Push path:**
1. Recorder commit → `after_commit` → `publish("trajectory.available", {user_id, owner_user_id, session_id, trajectory_id, committed_seq})` (RC:167-169,244-253). Deletion adds `deleted: true` (backend/trajectory/lifecycle.py:26-28). Rollback drops the hint (RC:256-262).
2. The bus dispatches in-process and to other processes over Redis (bus.py:65-95,166-168). The agent socket ignores `trajectory.*` (backend/api/ws.py:247).
3. The socket's `notify` coalesces per session, latest wins (WS:89-94). The `publish` loop then discards the payload and recomputes the header for each pending session (WS:138-152).

**Cost per delivered notification:** `validate_viewer` (users row, optional mobile session, Redis) plus a full `get_session_header` (8+ queries, possibly a full replay).

**Replay and reconnect semantics:**
- There is no replay over the socket; messages are hints only. The client reads `/events` over HTTP from its own contiguous `loadedSeq` (sync.ts:253-259,315-338).
- Reconnect backoff is 1 s × 2ⁿ up to 30 s, reset on open (client.ts:111-119,197-206).
- On `__connected` the hook re-subscribes; `subscribed` also triggers a catch-up. Hints are filtered by session and owner (useTrajectorySocket.ts:24-33,30-31).
- The socket disconnects when its last user unmounts (useTrajectorySocket.ts:43-48). Lost hints are recovered by the 1 s poll.

## 4. Auth
### 4.1 HTTP chain
1. **`get_optional_current_user`** (MW:32-66):
   - Auth off: returns `{user_id:"default", role:"admin"}`.
   - No bearer: returns None, then `get_current_user` raises 401 "Not authenticated" (MW:69-75).
   - Decodes HS256 with JWT_SECRET; requires `type=="access"` and `sub` (backend/auth/jwt.py:11,80-94).
   - Rejects if `jwt_bl:{jti}` exists.
   - `validate_claims`: `web` passes; `mobile` requires a live `mobile_sessions` row for `sid`; legacy tokens (no `client`) are refused if the user has a mobile session. `X-Client-Type: mobile` requires a mobile token (auth/mobile.py:115-135). These refusals are 401 with an `X-Error-Code` header.
2. **`assert_admin`** (TA:42-51), against the DB `users` row:
   - missing, `is_deleted` or not active → 401
   - `role != "admin"` → 403
   - `admin_enabled(user_id)` false → 404 "Trajectory administration is disabled"
   - Returns `{user_id, role:"admin"}`.
   - The JWT `role` claim is ignored (TA:1). Other admin APIs do trust it, via `MW.require_admin` (MW:78-82; e.g. backend/api/admin_fleet.py:23).
- **JWT access claims** (jwt.py:33-43): `sub`, `role`, `type`="access", `exp` (default 15 min; backend/core/config.py:561), `jti`, `client` (web/mobile), `sid`. Issued by login, Logto login and refresh (backend/auth/routes.py:153,218,374,406). Logto JWTs are only verified during login (backend/auth/logto.py:98-113).
- **Logout** revokes the refresh cookie and the mobile session, not the access token's jti (routes.py:97-112,417-422).

### 4.2 `revalidate_viewer`
- **What it does** (TA:31-39): when auth is on, re-parses the Authorization header through the middleware; the user must be the same, else 401. Then runs `assert_admin`.
- **Where it runs:** only after blob I/O in payload (AT:126) and export download (AT:195), each followed by a DB recheck.
- **Test matrix** (tests/integration/test_trajectory_read_races.py:37-72): role change → 403, token revoked → 401, asset or payload deleted → 410, root deleted → 404, no bytes leaked, no-store present.

### 4.3 NoStoreRoute
- **What it does** (TA:15-28): renders HTTPException and validation errors itself, then sets `no-store`. The dependency also sets the header (TA:55).
- **Tests:** test_trajectory_read_races.py:75-90.

### 4.4 Audit
| Action | Trigger | Details |
|---|---|---|
| `admin.trajectory.list` | Every list call, including the 5 s probe | filters `{user_id, workspace_id}` |
| `admin.trajectory.view` | Every header call (5 s poll) | `{through_seq}` |
| `admin.trajectory.payload` | Payload GETs (15 s revalidation, downloads) | `{payload_id, through_seq}` |
| `admin.trajectory.export` | Export create | `{through_seq, export_id}` |
| `admin.trajectory.download` | Export download | `{export_id}` |
| `trajectory.subscribe` | First subscribe per WS connection | `{owner_user_id, session_id, through_seq}` |

- The five HTTP actions go through `audit.record` (best-effort, catches exceptions; backend/audit/__init__.py:12-41). It writes the business `audit_logs` table with target_type `trajectory`, target_id = session_id, IP and user agent (AT:38-39).
- The WS action calls `PgAuditRepo.create` directly, so a failure propagates (WS:124-131).
- The plan says to merge repeated polling into one audit entry (docs/plans/trajectory/SESSION_TRAJECTORY_IMPLEMENTATION_PLAN.md:462); this is not implemented.

### 4.5 Can a separate service authorize with only the JWT secret?
- **Covered by JWT_SECRET:** signature, `exp`, `type`, `sub`, `jti`, `client`, `sid`.
- **Not covered:**
  - DB role, active and deleted flags (`users`)
  - the `TRAJECTORY_ADMIN_*` allowlist
  - `mobile_sessions` validity
  - the jti blacklist (Redis)
  - the ticket store (Redis `ticket:*`, `ticket_claim:*`)
- **Internal endpoints:** the only one is `GET /api/internal/tunnel-keys`, guarded by `X-Internal-Token` == `INTERNAL_API_TOKEN` via `hmac.compare_digest` (backend/api/internal.py:12-22; core/config.py:480,812). No viewer or admin introspection endpoint exists.

### 4.6 Frontend auth behaviour
- **Route guard:** `RequireAdmin` uses `user.role` from `/api/auth/me` (src/app/router/guards.tsx:25-31; shared/api/auth-store.ts:39-55).
- **HTTP client:** sends the bearer token with `credentials: include`. On 401 it refreshes once and retries if the user is unchanged (shared/api/http.ts:49-84,106-121).
- **Access latch:** any 401/403 from a trajectory query, mutation or socket triggers a purge (FE/api/access.ts:75-102,153-189; stores/access.ts:26-35). The purge:
  - aborts in-flight requests
  - removes cached queries
  - stops sync engines
  - disconnects the socket
  - resets the view
  - bumps the epoch, which is part of every query key (api/keys.ts:12-31)

## 5. Frontend data dependencies
### 5.1 Engine
- **One `TrajectorySync` per (viewer, session)** (FE/api/registry.ts:48-77).
- **Open:** fetch `checkpoint` without `at_seq`; head = its `through_seq`. The checkpoint is used as the base only if it passes validation (version 1, shape, through_seq consistent, not beyond target; FE/utils/adapter.ts:82-92); otherwise replay starts from empty.
- **Catch-up ("pump"):** reads `/events?after_seq=loaded&limit=500&include_data=true`, up to 20 pages per pump (sync.ts:189-190,315-338).
- **Replay positions:** a separate segment built from checkpoint(at_seq) plus events exactly through H, or an extension of the current segment by up to 4 pages (sync.ts:93,406-449).

### 5.2 Per view
| View | Source | Fields read / notes |
|---|---|---|
| Session list (TrajectoryListPage.tsx:40-122; SessionFilters.tsx:48-177) | `/sessions` limit 50; probe limit 1 | Row fields in §2.3. "Updated" shows when the probe's first row differs in session_id or last_activity_at (TrajectoryListPage.tsx:29-55). URL keys: owner, user, q, workspace, status, recording, from, to, `unrecorded=1`, sort, cursor, trail (params.ts:49-125). `include_unrecorded` is sent only when true (endpoints.ts:22-30). Filter values: run status running/waiting/idle/error, recording status recording/gap/paused/not_recorded (constants/labels.ts:73-74). Unrecorded rows show dashes, keyed on trajectory_id null (SessionTable.tsx:92,106,125). The server includes unrecorded sessions only when `parent_id` is null (RP:330-333) |
| Header bar | `/sessions/{sid}` live | See §0.3. 404/410 messages at TrajectorySessionPage.tsx:65-77 |
| Statistics strip (StatisticsStrip.tsx:32-82) | Local projection | FE/utils/statistics.ts:70-88: counts, token totals or null, usage_complete, duration (union of run intervals), through_seq |
| Record tree (RecordRowView.tsx:38-155; utils/view.ts:38-190; ordinals.ts:12-51) | Local projection; SummaryPreview for first paint | record_id, kind, title, preview, result_preview, status, status_reason, start_seq, end_seq, started_at, duration_ms, agent_id; parent ids (parent_call_id, call_id, request_id, parent_agent_id, step_id, run_id, turn_id); quick filter over title, preview, result_preview, id, status_reason (view.ts:126-135) |
| Timeline (utils/timeline.ts:72-118) | Local | kind, status, start_seq, end_seq, started_at, finished_at, duration_ms |
| Playback (usePlaybackControls.ts:51-172; utils/playback.ts:27-75) | Local events | event seq, type, occurred_at |
| Inspector (RecordInspector.tsx:60-136; TabPanel.tsx:50-95; utils/tabs.ts:44-81) | Record detail at a position throttled to ≤ 400 ms (`useSettledSeq`) while live | Panels use the server record; tab list falls back to the local record; 404 → "not yet at this seq" |
| Panels reading server `data` | Record detail | **request:** input, capture_level, media_inputs (RequestInputPanel.tsx:154-184; RequestOptionsPanel.tsx:17-66). **system / tool_catalog:** system, tools, before, source_request_id, after.tools (SystemPromptPanel.tsx:12-50; SystemDiffPanel.tsx:40-83; ToolCatalogPanel.tsx:9-34). **tool:** requested/effective arguments, arguments_raw, output, model_output, metadata, error, schema, schema_source, request_schema_ref. **user:** text/content, attachments, committed_parts. **assistant:** committed_parts, channel, blocks. **artifact:** name, path, media_type, size, availability, payload. **question:** questions, answers. Generic tab/summary fields: constants/inspector.ts:140-253 |
| Panels reading the local projection (`useInspector().records`) | Local | Relation lookups: InputPreviewPanel:33, ToolArgumentsPanel:33, ToolResultPanel:35, SummaryPanel:57, TimingPanel:57, AgentTreeTab:20, AssistantPreviewPanel:90, InterruptImpactPanel:15, UsagePanel:96-119. **Needs full data:** RetryAttemptsPanel compares `data.input` between attempts (RetryAttemptsPanel.tsx:55-57); RelatedPanel reads `data.resume_of_run_id` (:20-21) |
| Events tab (EventsPanel.tsx:15-73) | `record.events` from detail | Full event JSON. RawContentPanel, JobProgressPanel and ArtifactDiffPanel also read `record.events` |
| Search (SearchPanel.tsx:30-150) | `/search` at the shown H, or at the loaded head if the viewer chooses that during replay | A hit is selectable if the record exists locally and hit.seq ≤ H; otherwise "move to" sets the playhead to hit.seq (SearchPanel.tsx:35,103,127) |
| Payload view (PayloadView.tsx:40-103; MediaRefView.tsx:40-79; ValueView.tsx:26-27; JsonTree.tsx:56-66) | `/payloads/{id}?through_seq=H` | Image/audio/video by media type; JSON or text shown inline up to 2 MiB (queries.ts:21-22,203-229). Envelopes inside JSON load on click; model-input media loads automatically (MessageView.tsx:27-37) |
| Payload download (hooks/useDownloadPayload.ts:46-97) | Fresh GET per click | 404/410/409 replace the cached state instead of saving |
| Copy / save value (ContentActions.tsx:34-56) | None | Built from the in-memory value |
| Export (ExportControl.tsx:28-79) | POST → poll 2 s → blob download | Enabled when `capabilities.export` and not wiped |

### 5.3 Request input rendering and lazy loading
- **How input reaches records today:** both projectors copy non-streaming event data into `record.data`, so `request.prepared.data.input` becomes `record.data.input` (PJ:138-226; FE/utils/projector.ts:469-480).
- **System records:**
  - `system:{request_id}` is derived from `input.system`/`instructions` (or system/developer messages) plus `input.tools`.
  - It is created only when these differ from the same agent's previous system record, and keeps a `before` snapshot (PJ:228-250; projector.ts:263-300).
- **Row preview** for a request is the first 240 chars of the input JSON (PJ:76,219; projector.ts:499-505).
- **RequestInputPanel** renders its structured Snapshot only when `data.input` is a plain object (RequestInputPanel.tsx:72-184; test RequestInputPanel.test.tsx:14-72):
  - system, then each message via MessageView, prompt, media_inputs, the tools list, extra_body, other fields, omitted_fields, and the raw tree.
  - Otherwise it falls back to ValueView; a `$payload` reference already renders as a lazily fetched JSON tree.
- **What lazy loading needs, without breaking rendering:**
  - A small inline stub: `input_sha256`, system/tools digests, message count, option keys, a short preview, and a reference to the full content.
  - Identical changes in the Python and TypeScript projectors: system snapshot compares digests, preview comes from the stub.
  - RetryAttemptsPanel compares digests instead of values.
  - Request, system prompt, system diff, tool catalog and options panels load the reference, then render the existing components.
  - ContentActions loads before copy/save.
  - Immutable content is not re-downloaded every 15 s.
  - The golden fixture and the e2e fixture server are updated.

### 5.4 Polling
| What | Interval | Where |
|---|---|---|
| Events poll | 1 s visible, 5 s hidden; immediate poll when the tab becomes visible | useTrajectorySync.ts:8-9,50-73 |
| WS hint → pump | On each hint (skipped if not newer than loaded) | sync.ts:253-259 |
| Header | 5 s visible, 30 s hidden, also in background | queries.ts:95-105 |
| List probe | 5 s / 30 s | queries.ts:82-92 |
| List page | never stale; manual refresh | queries.ts:59-68 |
| Record detail | once per position; position throttled to ≤ 400 ms while live or playing | RecordInspector.tsx:46,64; useSettledSeq.ts:11-28 |
| Payload | staleTime 0, gcTime 0, refetch 15 s, on mount and on focus | queries.ts:231-246 |
| Export job | 2 s while pending or running | queries.ts:301-311 |
| Records, search, checkpoint | immutable per H | queries.ts:130-191 |

Plan targets: 1 s watermark check, list every 5 s (IMPLEMENTATION_PLAN.md:661-663).

### 5.5 Watermark handling
- Seqs are compared as decimal strings (sync.test.ts:496-497).
- head = max(committed_seq, until_seq) from each page (sync.ts:326). A "catching up" notice shows while head > loaded (session/SyncNotice.tsx:46).
- Duplicate hints are ignored. A hole raises a gap error but keeps the verified prefix. An event beyond the requested seq is reported as malformed (sync.ts:127-139,340-358).
- 404 after data was loaded, or 410 → "gone"; 401/403 → "denied" (sync.ts:340-358).
- The URL `at` and `record` parameters mirror the playhead and selection (useDetailUrlSync.ts:10-23).
- A pinned replay position never shows head summaries (PendingPosition.tsx:19-31).

## 6. Frontend tests and how to run them
- **Package manager:** npm (package-lock.json). Setup is `npm ci` (frontend-v2/README.md:15). `node_modules` is not installed in this worktree.
- **Scripts** (frontend-v2/package.json:6-18):
  - `npm test` = `vitest run` (jsdom, `src/**/*.test.{ts,tsx}`; vite.config.ts:47-51)
  - `npm run check` = i18n + seo checks + lint + `tsc -b` + vitest (README.md:24)
  - `npm run test:nginx` (scripts/test-nginx.mjs; includes a WS upgrade check at :125-147)
- **Targeted run:** `npx vitest run src/features/admin-trajectories src/shared/ws/client.test.ts src/app/layouts/WorkspaceLayout.isolation.test.tsx src/app/router/router.test.ts src/features/admin/AdminNav.test.tsx`
- **Unit test files under FE/ (25):**
  - `api/`: access, endpoints, sync (`.test.ts`), queries (`.test.tsx`)
  - `components/`: TrajectorySessionPage.test.tsx, TrajectorySessionPage.fixedH.test.tsx
  - `inspector/`: MarkdownRenderer, MediaRefView
  - `inspector/panels/`: ArtifactPanels, RequestInputPanel, ToolArgumentsPanel
  - `session/`: PlaybackBar, RecordTable, seekScale, usePlaybackControls
  - `hooks/`: useDownloadPayload, usePlayback, useStableWatermark
  - `utils/`: adapter, artifact, layout, params, projector, seq, view
  - Harness: components/testing/harness.ts
- **Related tests outside the feature:**
  - shared/ws/client.test.ts:46-47,158-172: own ticket path, terminal statuses
  - WorkspaceLayout.isolation.test.tsx:66-73: allowed reads include `/api/admin/trajectories/`
  - router.test.ts:30-33
  - AdminNav.test.tsx:43-60
- **E2E with fixtures (no backend):**
  - Start Vite on 127.0.0.1:3101, then run `npx playwright test -c playwright.trajectories.config.ts`.
  - REST and WS are stubbed by e2e/helpers/trajectory-server.ts: routes :369-374, dispatch :436-458, ticket :446, WS handling :745-790.
  - Specs: e2e/trajectories.spec.ts:194-763 (15 tests); e2e/trajectories-scale.spec.ts:123 (100k events).
- **Native acceptance:** run `backend/scripts/trajectory_dev_server.py --data-dir <dir> --port 8091`, then `VITE_API_URL=http://127.0.0.1:8091 npm run dev -- --host 127.0.0.1 --port 3101` (docs/SESSION_TRAJECTORY_IMPLEMENTATION_STATUS.md:135).
- **Backend contract tests:**
  - test_trajectory_boundaries.py
  - test_trajectory_read_races.py
  - test_trajectory_storage.py:122-184: overrides `get_current_user` and monkeypatches `get_optional_current_user`
  - test_trajectory_media_deletion.py:69-85
  - Run: `cd backend && uv run pytest tests/integration/test_trajectory_*.py`

## Migration notes

**A. Routing the same contract to a worker service**
- **Paths that move:** `/api/admin/trajectories/*` (including `/ticket`) and `/ws/admin/trajectories`. Keep the same origin: the SPA uses relative URLs and one global `VITE_API_URL` (env.ts:2-4), so tokens and CORS need no changes.
- **nginx:** add `location ^~ /api/admin/trajectories/` and `location = /ws/admin/trajectories` ahead of the generic blocks (nginx.conf:42-59). Use a new template variable (`${TRAJECTORY_HOST}`), set Upgrade headers and a `proxy_read_timeout` above the heartbeat. Extend test-nginx.mjs.
- **Other entry points:**
  - Vite: add proxy entries before `/api` (vite.config.ts:42-45).
  - k8s: add more specific ingress paths (base.yaml:270-305; aks.yaml:216-237).
  - Production compose lives outside the repo (docs/operations/DEPLOY.md:3-4).
- **Work that moves into the worker:**
  - archive/checkpoint/purge loop
  - `resume_exports`
  - export stop on shutdown and recorder flush (main.py:79-117,249)
  - export builds (currently a BackgroundTask, AT:149): need a lease, because every replica's `resume_exports` would restart the same rows (EX:131-136).

**B. Auth in the worker.** Parity means keeping:
- the DB role as the authority
- the admin env allowlist
- mobile claims
- jti revocation
- expiry enforced during WS
- the recheck after blob I/O

Options:
1. **Local JWT verification plus direct reads.** The worker verifies JWTs with JWT_SECRET, reads business `users` and `mobile_sessions` through a read-only engine, and shares Redis for tickets and the blacklist. Fast; couples the worker to the business schema.
2. **Internal introspection endpoint.** Add e.g. `POST /api/internal/trajectory-viewer` guarded by `X-Internal-Token` (same pattern as api/internal.py:15-22). The WS rechecks every 1 s and before every send (WS:84-87,154-157), so this needs a cache of about 1 s or event-driven revocation. Admin viewing becomes dependent on the backend being up (fail-closed); the business path is unaffected.
3. **Split issue/consume.** `/ticket` stays in the backend and the worker consumes via shared Redis. The worker must honour the `ticket:{t}` JSON and `ticket_claim` incr formats (auth/ticket.py:31-40,59-61).
- In all options, keep 401/403/404 on HTTP and 4401/4403 on WS. Accept the socket before closing it on refusal, so browsers get 4401/4403 instead of 1006. Keep no-store, nosniff and `revalidate_viewer`.

**C. Audit.**
- **Current storage:** business `audit_logs`.
- **Options:** the worker writes to the business DB directly, sends audit facts to the backend, or keeps its own table.
- **Keep:** action names `admin.trajectory.{list,view,payload,export,download}` and `trajectory.subscribe`; make the WS audit best-effort.
- **Deduplicate** per viewer, session and action, since polling currently writes a row every 5 s (§0.4).

**D. Business data the trace DB cannot answer on its own**
| Need | Current source | Options |
|---|---|---|
| List filters and sort, `include_unrecorded`, is_deleted | sessions ⋈ users ⋈ workspaces (RP:323-370) | Denormalized session/user/workspace facts in the trace DB; a read-only business engine; or a split query (unrecorded sessions exist only in business data) |
| Header identity (title, agent, model, status, owner, workspace name) | RP:260-280 | Same as above |
| Session deletion → 404 | `is_deleted` (RP:34-37) plus `delete_trajectory_in_tx` inside the business transaction (lifecycle.py:10-28) | Emitted deletion fact, with an eventual-consistency window |
| `"paused"` recording status | Env flags evaluated at read time (RP:274,346-352) | Persist from producer facts |
| Payload/export validity after attachment deletion | `file_assets` read at request time (AT:167-171; PL:109-113) and `delete_for_asset` in the attachment transaction (PL:190-196) | Asset-deleted facts, or a read-time lookup. Mandatory for media recorded as references |

**E. Notifications and WS in the worker.**
- Publish after the trace-DB commit that advances `committed_seq`, and only when `/events` can serve it contiguously (the client requires gap-free pages; sync.ts:325,469).
- Keep the exact 5-key payload. Send it directly instead of recomputing the header per wake (WS:147).
- Add a server heartbeat under 30 s. The client message type already allows `ping`.
- Multiple worker replicas need pub/sub fan-out and a shared ticket store.

**F. SQL search.**
- **Keep:** `{record_id, seq, kind, preview}`, the H-bound cursor, and `seq` = record `as_of_seq` at H (it drives "move to" and selectability).
- **Current behaviour:** case-folded substring over the whole record JSON at H.
- **Risks:**
  - The `simple` tsvector configuration does not segment Chinese.
  - pg_trgm drops non-ASCII characters under C/POSIX LC_CTYPE and needs at least 3 characters to use the index.
  - Verify the DB locale; the zh-CN UI exists (src/locales/zh-CN/admin-trajectories.json).
- **Historical H:** results must exclude values written after H. That needs versioned rows or post-filtering.

**G. `get_record` read by index.**
- **Still required:** the full `data` (or its references) and `record.events` (EventsPanel, RawContentPanel, JobProgressPanel, ArtifactDiffPanel) at H.
- **Assistant/system records** also include events sharing the request_id (RP:441).
- **Historical H:** today a full state rebuild (RP:428-444). A single indexed row only answers the head. History needs per-record versions or a targeted replay; events are indexed only by request_id, call_id and agent_id (backend/db/models/trajectory.py:49-51).
- **Keep the 404 "not yet at this position".**

**H. Frontend changes for lazy input expansion** (see §5.3):
- **Shared stub format:** agree on it for events and records.
- **Projector changes:** edit both projectors plus FE/utils/projector.test.ts and the golden fixture (backend/trajectory/fixtures/session_v1.json).
- **Panel and loader changes:**
  - Load full content on demand, through the existing `$payload` path (ValueView.tsx:26-27) or a new blob endpoint authorized by (trajectory, H) and deletion state.
  - Re-render the existing `Snapshot`.
  - RetryAttemptsPanel compares digests.
- **Caching:** cache immutable bodies by sha256 and revalidate availability with a small JSON call instead of the 15 s full refetch (queries.ts:231-246).
- **Payload weight:** drop inline expansion from `/events` (endpoints.ts:116; RP:231-232) and shrink checkpoint state (RP:181-189).
- **Fixture server:** update e2e/helpers/trajectory-server.ts.

**I. Lower polling.**
- **Events:** poll every 10–30 s while the socket is connected (`WsClient.connected`, client.ts:42-44); keep 1–5 s when disconnected; keep the immediate poll when the tab becomes visible.
- **Header:** refresh on hint, or every 30–60 s.
- **List probe:** 30 s.
- **Record detail:** throttle to ≥ 2 s, or refetch only when the local `as_of_seq` changes.
- **Payload:** availability-only revalidation.
- **Tests to update:**
  - the useTrajectorySync constants
  - sync.test.ts:149 (lost-notification recovery)
  - queries.test.tsx:84-108 (payload revalidation)

**J. What breaks or must be ported.**
- **Backend tests** that build `main.create_app` or mount the routers with business fixtures (test_trajectory_boundaries.py:27-47; test_trajectory_storage.py:122-135).
- **Contract doc:** docs/reference/SESSION_TRAJECTORY_PROTOCOL.md:86-111.
- **Acceptance server:** trajectory_dev_server.py:21-143.
- **nginx and Vite proxy tests.**

**K. Risks and open questions**
1. The deleted-payload cliff (§0.6). Is it hit on production data?
2. Idle WS churn behind nginx (§0.8) is inferred; confirm in the gw2 logs.
3. With async projection, `committed_seq`, `projected_through_seq` and summary `applied_seq` diverge. Define what each response reports. A request with `through_seq` above the head returns 400, so spool lag must not look like a gap.
4. The HTTP 404 for "admin disabled" looks the same as "session not found" to the frontend engine.
5. Desktop mode (no JWT secret) yields user `"default"` (MW:28-29). Should the worker support it?
6. Where audit rows live after the split, and whether they are deduplicated.
7. Multi-replica worker: tickets, WS fan-out, export and archive leases.
8. Search locale behaviour for Chinese text.