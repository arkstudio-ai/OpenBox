# Infrastructure, Deploy and Dependencies: Current State Before the Trajectory Re-architecture

Repo root: `/Users/wang/workspace/OpenBox/.claude/worktrees/trajectory-rearch`. Every citation is `path:line`, relative to that root. Snapshot: `worktree-trajectory-rearch` @ `5993237`. Read-only survey.

## 0. Key facts

1. **One database today.** There is one business Postgres and one engine singleton. All 7 trajectory tables live in it, and `/health` checks them (`backend/db/base.py:65-87`, `:329-341`; `backend/main.py:392-401`).
2. **One Alembic chain.** It has 62 revisions and a single head `c7e9b1d3f5a7`. `env.py` always uses `DATABASE_URL` and `Base.metadata`, and that metadata includes the trajectory models (`backend/db/migrations/env.py:10-22`; `backend/db/models/__init__.py:52-53`). The backend container migrates on start (`backend/Dockerfile:26`).
3. **The archive worker runs inside every web process.** It is an asyncio task, always started, that polls every 5 s. Nothing prevents two processes from running it at once (`backend/main.py:107-108`; `backend/trajectory/payload.py:238-260`).
4. **No OSS blob provider exists.** Blob storage supports only local, GCS and Azure. The default is `azure` with an empty connection string, and that raises inside the archive loop (`backend/core/config.py:548-549`; `backend/trajectory/payload.py:40-42`).
5. **`core/oss.py` only signs URLs.** It supports PUT, GET, HEAD, DELETE and copy. It has no list, no multipart, no server-side upload or download of bytes, and no STS token (`backend/core/oss.py:56-166`).
6. **Production is manual.** Each server runs a single-host Docker Compose with one backend instance. The production compose files are not in the repo, releases are manual, and there is no CI (`docs/operations/DEPLOY.md:944-988`, `:1040-1063`, `:1086-1090`; no `.github/`).
7. **nginx has one upstream.** frontend-v2 nginx sends every `/api/` and `/ws/` request to `${BACKEND_HOST}`, filled in from a template (`frontend-v2/Dockerfile:20-21`; `frontend-v2/nginx.conf:10-12,42-59`).
8. **Dependencies.** Missing: `orjson`, `zstandard`, `pyarrow`, `duckdb`, `psycopg`. Present: `httpx`, `redis`, `asyncpg`, `watchfiles` (`backend/uv.lock`).
9. **Name trap.** Make targets delete any Compose service named `backend-worker` and any k8s Deployment named `openbox-backend-worker` (`Makefile:96-111`).

---

## 1. `backend/db/base.py`

| Item | Behavior | Cite |
|---|---|---|
| `JSONType` | JSONB on PG; TEXT with json dumps/loads elsewhere | `base.py:27-46` |
| `Base` | `DeclarativeBase`; `dict→JSONType`, `datetime→DateTime(timezone=True)` | `:53-58` |
| Singletons | module-level `_engine`, `_session_factory`: one DB per process | `:65-66` |
| `init_engine(url, pool_size=10, pool_overflow=20)` | if `"sqlite" in url`, no pool args; otherwise `pool_size` and `max_overflow`. No `pool_pre_ping`, `pool_recycle` or timeouts. `async_sessionmaker(expire_on_commit=False)`. Logs only the part after `@`. Calling it again overwrites the engine without disposing the old one | `:69-87` |
| `ensure_engine(config)` | Does nothing if an engine exists. With JWT: PG from config. Without JWT: SQLite `./.openbox/skill_jobs.db`, `create_all`, column retrofits (billing, skill store, trajectory `trace_context`, message center), then seeds a default user, workspace and project | `:90-123`, `:126-175`, `:273-315` |
| `get_engine` | Raises if not initialized | `:318-322` |
| `_READINESS_SCHEMA` | Maps each table to its required column names (see 1.1) | `:329-414` |
| `_missing_readiness_schema` / `database_schema_ready` | Checks via the inspector. Any missing name returns False and logs the names; any exception returns False | `:417-448` |
| `get_db_session()` | See 1.2 | `:451-470` |
| `close_engine()` | Disposes the engine and resets the singletons | `:473-480` |

Other places that create engines:
- Alembic env, with NullPool (`env.py:54-59`).
- `billing/backfill.py:97`, `scripts/migrate_json_to_pg.py:39`, `scripts/cleanup_containers.py:56`, `scripts/trajectory_dev_server.py:65`.
- The benchmarks in `trajectory/benchmark*.py`.

`storage/storage.py:27-33` chooses between DB and file storage by checking `db.base._engine`.

### 1.1 The `trajectory_payloads` entry at line 337

`_READINESS_SCHEMA` lists table and column names only; it does not check types or indexes. `/health` enforces it (`main.py:392-401` returns 503 `not_ready`), so a container missing additive columns never reports healthy (reason given at `base.py:325-328`). The trajectory entries are `:335-341`. Line 337 requires `payload_id, first_seq, content, storage_status, availability, sha256`. These are exactly the columns the "stage bytes in the DB, then archive to blob" flow uses:

| Column | Role today | Cite |
|---|---|---|
| `content` (LargeBinary, nullable) | Immutable bytes written inside the business transaction | `models/trajectory.py:62`; `payload.py:86-95` |
| `storage_status` (`pending`→`stored`) | Archive queue flag; a conditional update clears `content` | `:61`; `payload.py:208-225` |
| `availability` (`available`/`deleted`) | Deletion marker; deletion wins over a late archive | `:66`; `payload.py:190-196,227-229`; `lifecycle.py:21-23,38-39` |
| `sha256` | Dedupe within a trajectory, and integrity check on read and archive | `:59`; `payload.py:76-85,123-124,216-220` |
| `first_seq` | Payload is visible only at or after this sequence number | `:67`; `payload.py:103-104,155-156` |
| `payload_id` | Primary key and reference identity | `:57`; `payload.py:68-71` |

The neighbouring entries `session_executions.trace_context` and `cron_runs.trace_context` (`:330-334`) are business columns added by the same migration (`f6a8c0e2b4d6_session_trajectories.py:136-137`).

### 1.2 `get_db_session` commit/rollback behavior (`base.py:451-470`)
- The session starts its transaction automatically. On normal exit it commits. On `Exception` it rolls back and re-raises. It always closes.
- A `BaseException` such as `CancelledError` skips the explicit rollback; `close()` releases the connection and the transaction is discarded.
- Callers also commit in the middle of the block (`api/assets.py:294`, `:307`, `:435`), so `after_commit` can fire more than once per session. Read-only blocks still commit at the end.
- Existing hooks:
  - A global, class-level `after_commit` / `after_soft_rollback` listener on `sqlalchemy.orm.Session` handles trajectory notifications and skips nested transactions (`trajectory/recorder.py:243-262`, `:245`).
  - A per-session `once=True` `after_commit` listener is an existing example of emitting after commit (`notifications/inbox.py:121-133`).

### 1.3 SQLite vs PG
| Concern | Handling | Cite |
|---|---|---|
| Server (JWT set) | PG via `DATABASE_URL` | `main.py:40-49`; `base.py:102-107` |
| Desktop (no JWT) | SQLite, `create_all`, never runs Alembic, manual `ALTER`s | `base.py:109-123,190-201` |
| Tests | Autouse in-memory SQLite `create_all`. PG only via `TRAJECTORY_TEST_DATABASE_URL`, restricted to databases named `openbox_trajectory_storage_*` | `tests/conftest.py:36-47`; `tests/integration/test_trajectory_storage.py:27-40` |
| Upsert dialect | `sqlite_insert` or `pg_insert` | `trajectory/recorder.py:28-29` |
| Alembic JSON rendering | `postgresql.JSONB().with_variant(sa.Text(),"sqlite")` | `env.py:37-42` |
| PG extensions | No `CREATE EXTENSION`, `pg_trgm`, `tsvector` or GIN anywhere in `backend/`. The only `postgresql_using` hits are type casts | `versions/a1c3e5f7b9d2_structured_output.py:43,62` |

---

## 2. Alembic

### 2.1 Configuration
| Item | Value | Cite |
|---|---|---|
| ini | `backend/alembic.ini`; `script_location = db/migrations` (relative to cwd); default URL `postgresql+asyncpg://openbox:openbox@localhost:5432/openbox`. No `version_table`, `file_template` or `prepend_sys_path` | `alembic.ini:1-3` |
| env | Imports `db.base.Base` and all of `db.models`, including trajectory; `target_metadata = Base.metadata`; URL is `DATABASE_URL` from env, else the ini value | `env.py:10-22` |
| Online mode | Async engine, `NullPool`, `run_sync` | `env.py:54-64` |
| Autogenerate | `render_item` for `JSONType`; no `include_object` filter; default `alembic_version` table | `env.py:37-51` |
| Template | Typed revision header | `script.py.mako:1-25` |
| Import path | Docker sets `PYTHONPATH=/app`. Dev runs from `backend/` with an absolute script_location. The hatch package list is incomplete (for example `cron`, `publish`, `platforms`, `audit` are missing), so imports depend on the path, not on the wheel | `backend/Dockerfile:14`; `scripts/backend_entrypoint.py:42-47,57`; `pyproject.toml:64-70` |

### 2.2 Revision chain
- 62 files named `versions/<12-hex>_<slug>.py`, mostly with hand-picked hex ids.
  - Root: `ea9d90d96835`.
  - Merge revisions: `f14b351203e1`, `e2f4a6b8c0d2`, `e4f6a8b0c2d4`, `c7e9b1d3f5a7`.
  - Single head: `c7e9b1d3f5a7`, which merges `f6a8c0e2b4d6` (trajectories) with `a1c2e3b4d5f6` (message center) (`versions/c7e9b1d3f5a7_merge_message_center_trajectories.py:7-8`).
- `f6a8c0e2b4d6` (down-revision `e4f6a8b0c2d4`) creates 7 tables, their indexes, and the two `trace_context` columns. Its downgrade drops all of them (`f6a8c0e2b4d6_session_trajectories.py:10-149`).
  - All foreign keys point to `session_trajectories` with `ON DELETE CASCADE` (`:51,71,91,107,120,133`).
  - There are no foreign keys to business tables.
- Production was at `f6a8c0e2b4d6` on 2026-09-14 (`docs/operations/DEPLOY.md:18`). The next release will also apply `a1c2e3b4d5f6` and the merge.

### 2.3 How migrations run
| Context | Mechanism | Cite |
|---|---|---|
| Any container | `CMD /bin/sh -ec "alembic upgrade head\nexec uvicorn main:app --host 0.0.0.0 --port 8080"` | `backend/Dockerfile:26`; `docs/operations/DEPLOY.md:992` |
| Dev | `make dev/start/migrate` runs `backend_entrypoint.py --migrate-only` (sets `DATABASE_URL` to local `openbox_dev` if unset, removes proxy env vars), then uvicorn with `--skip-migrate` | `Makefile:3,19-25,38-41,93-94`; `backend_entrypoint.py:15,23-70` |
| Rollback | `docker compose run --rm --no-deps --entrypoint alembic backend downgrade <rev>`, then swap the image. The trajectory release's rollback goes down to `e4f6a8b0c2d4` | `DEPLOY.md:265,288,307,45` |
| Guardrails | A boot once failed on migrations, giving the rule "check `alembic heads` before release"; a test asserts one head and unique ids | `DEPLOY.md:274-279`; `tests/unit/test_migration_heads.py:17-29` |

---

## 3. Process lifecycle (`backend/main.py`)

### 3.1 Startup order (lifespan `:93-198`)
| # | Step | Scope | Cite |
|---|---|---|---|
| 0 | `load_dotenv(backend/.env)` at import | all | `:13` |
| 1 | `_init_infrastructure`: with JWT, `init_engine(DATABASE_URL, pool)`, `RedisCache`, `setup_auth`; otherwise an in-memory ticket store | all | `:34-60,98` |
| 2 | `_init_agent`: tool registry, `tool.truncation.start_cleanup_task` | all | `:20-31,99` |
| 3 | `ensure_engine` (desktop SQLite bootstrap) | all | `:104-106` |
| 4 | **`trajectory.payload.start_archive_worker()`**, not gated on mode or on recording flags | all | `:107-108` |
| 5 | Log recording and admin flags | all | `:109-114` |
| 6 | `trajectory.export.resume_exports()` | if `TRAJECTORY_ADMIN_ENABLED` | `:115-117` |
| 7 | `sandbox.provider.reconcile()` | all | `:122-124` |
| 8 | Wuying fleet patrol | wuying per_user | `:148-150` |
| 9 | `init_redis_bus(REDIS_URL)` | JWT | `:153-155` |
| 10 | Cron scheduler | all | `:158-167` |
| 11 | Video job recovery | JWT | `:172-177` |
| 12 | `desktop_activation_service.start()` | all | `:179-180` |
| 13 | Legacy question reconcile, then `question_worker.start()` | all | `:181-186` |
| 14 | `PushWorker`, `InboxJanitor` | all | `:188-195` |

### 3.2 Shutdown order (`:199-251`)
1. Inbox janitor, push worker, question worker, desktop activation.
2. Wuying patrol, then cron.
3. Abort agent loops and wait up to 30 s.
4. **Close the Redis bus** (`:235-239`).
5. `sandbox_manager.release_all(destroy=False)`.
6. `_shutdown_trajectory()`: `stop_exports` → `flush` → `stop_archive_worker`, using try/finally (`:79-90,249`).
7. Close the engine and cache (`:63-76,251`).

Because the bus closes before the trajectory flush, notifications from the final flush reach only local subscribers (`bus.py:81-95`).

### 3.3 Trajectory archive worker
| Aspect | Current behavior | Cite |
|---|---|---|
| Loop | Every 5 s: `drain_payloads(100)` → `drain_checkpoints(10)` → `purge_deleted_content(100)`. Exceptions are logged by type only | `payload.py:242-258` |
| Concurrency | One task per process. Only a double start in the same process is prevented; no lock across processes | `payload.py:238-241` |
| `drain_payloads` | Selects `pending` + `available` rows by `created_at` and loads up to 100 `content` blobs into memory. For each: upload, download the whole object again, verify sha256, conditional UPDATE to `stored` with `content=NULL`. If the row was deleted meanwhile, deletes the blob | `payload.py:199-229` |
| Failure | Per-row `except Exception: failed += 1`. Bytes stay in PG and it retries forever | `payload.py:230-231,252-253` |
| Staging sources | Event data larger than `TRAJECTORY_INLINE_BYTES`; asset and media bytes, including uploaded attachments up to the 1 GB asset limit, fetched from OSS | `recorder.py:150-151`; `artifacts.py:18-25,97,194`; `api/assets.py:279-281,299,306` |
| `drain_checkpoints` | GROUP BY max checkpoint per trajectory, joined to `session_trajectories`, filtered by `TRAJECTORY_CHECKPOINT_INTERVAL`; rebuilds state and inserts a checkpoint. The aggregate runs every tick | `repository.py:192-214` |
| `purge_deleted_content` | Deletes blobs for deleted payloads and exports, then blanks `storage_key` | `lifecycle.py:31-59` |
| Blob keys | `trajectories/{tid}/payloads/{pid}/{sha}` and `trajectories/{tid}/exports/{eid}/{sha}.zip` | `payload.py:87`; `export.py:109` |
| Exports | Built as a ZIP in memory in the web process; unfinished jobs resume on startup | `export.py:17-24,41-128,64-66,131-145` |
| Stop | Cancels and awaits the task | `payload.py:263-271` |

### 3.4 In-process pieces that assume the recorder runs inside the web process
- Global `after_commit` publishes `trajectory.available`; `after_soft_rollback` drops the pending hints (`recorder.py:167-169,243-262`).
- Per-(event loop, user, session) stream queues with byte caps and in-memory failure state (`recorder.py:265-363`). `flush` walks those queues (`:413-444`).
- The stale-run check reads `session_executions` inside every append (`recorder.py:182-187`).
- The event bus is in-process plus the Redis channel `bus:events`, which ignores messages from its own worker id (`bus.py:29,65-95,134-191`). The admin WS subscribes locally (`admin_trajectory_ws.py:159`).

---

## 4. Images, Compose and production topology

### 4.1 `backend/Dockerfile`
- Base image `python:3.12-slim`. The uv image `ghcr.io/astral-sh/uv:latest` is not pinned (`:1-5,11`). `curl` is installed (`:7-9`).
- `WORKDIR /app`, `PYTHONPATH=/app` (`:13-14`).
- Dependencies install via `uv export --frozen --no-dev --no-hashes --no-emit-project` into the system Python. Optional extras such as `test`/`aiosqlite` are not installed (`:15-18`). Because of `--frozen`, `uv.lock` must be current or the build fails.
- Copies all of `backend/` and `container/dev-browser/` (`:20-21`), then runs a build-time import check (`:23`).
- `EXPOSE 8080`. CMD migrates, then starts **one uvicorn process** with no `--workers` (`:25-26`). There is no `HEALTHCHECK` instruction.
- The build context is the repo root. `.dockerignore` includes only `backend/` and `container/dev-browser/`, and excludes `.env*`, `openbox.json`, `backend/tests/`, `backend/docs/` and `.openbox/*.db` (`.dockerignore:1-24`).

### 4.2 Compose files in the repo (local dev only: `README.md:122`, `DEPLOY.md:1088-1090`)
- `docker-compose.yml`:
  - backend is built from source, with the Docker socket and `openbox.json` mounted, `env_file backend/.env`, and `DATABASE_URL`/`REDIS_URL` defaulting to the compose service names (`:2-22`).
  - **frontend builds the legacy `./frontend`** (`:24-32`).
  - redis 7 and postgres 16 with credentials `openbox/openbox/openbox` (`:34-52`); volumes `redis-data`, `postgres-data` (`:54-56`).
  - No blob volume, no healthchecks, no worker.
- `docker-compose.dev.yml`: postgres (password `openbox_dev`), redis, and Azurite blob storage (`:1-26`). Used by `make deps` (`Makefile:87-88`).

### 4.3 Production, as documented in `docs/operations/DEPLOY.md` (compose files not in the repo)
| Fact | Cite |
|---|---|
| AWS (dev) and gw2 (prod) run the same compose and config | `:3` |
| `/opt/openbox/`: `docker-compose.yml`, `docker-compose.override.yml`, `.env` (`OPENBOX_IMAGE_TAG`, `OPENBOX_DB_PASSWORD`), `config/backend.env`, `config/openbox.json`, `secrets/aliyun-config.json` (mounted to `/run/secrets`), wuying key | `:946-970` |
| Compose `environment:` overrides the env file: `DATABASE_URL=...@postgres:5432/openbox`, `REDIS_URL`, `WUYING_ENDPOINT`, Logto redirect URIs | `:975-988` |
| `backend.env` has about 49 keys, including OSS, Blob, DB/Redis; the values are not recorded | `:972-973` |
| Request path: browser → reverse proxy (Tencent Lighthouse nginx) → frontend nginx → `backend:8080` | `:933-942,747` |
| Release: build `linux/amd64`, `docker save`, gzip, transfer (OSS presigned URL or scp), `docker load`, edit the image pins in the override file, `docker compose up -d --no-deps <svc>`. Tags are `<date>-<batch>-<sha>` | `:1004-1036,1007,626-633,875-891` |
| Availability rules: single backend; never a bare `up -d`; switch backend, wait for healthy, then frontend; back up config and `preflight.dump` first | `:1040-1063`, `:19-20` |
| Trajectory release 2026-09-12: migration `e4f6a8b0c2d4→f6a8c0e2b4d6` ran automatically on both hosts | `:29-45` |
| OSS region `cn-shanghai`, bucket noted as `bossip/cn-shanghai`; internal endpoint used for image transfer | `:444,452,1018-1025` |

---

## 5. `k8s/`: frozen legacy (`README.md:120,156`), though Make can still apply it

| Aspect | `base.yaml` (GKE) | `aks.yaml` (AKS) |
|---|---|---|
| Backend / frontend replicas | 1 / 1 (`:81`, `:203`) | 1 / 1 (`:97`, `:162`) |
| Image | `gcr.io/PROJECT_ID/openbox-backend:latest` (`:93`) | `YOUR_ACR_NAME.azurecr.io/...:latest` (`:109`) |
| Env | Explicit env and secretKeyRefs; `SANDBOX_PROVIDER=kubernetes`; `BLOB_PROVIDER=azure` plus connection secrets (`:96-159`) | ConfigMap with `BLOB_PROVIDER=azure`, `BLOB_AZURE_CONTAINER=ads-staging`, plus `envFrom` secret (`:69-86,113-117`) |
| `TRAJECTORY_*` / `OSS_*` | none | none |
| Probes | readiness and liveness on `/health` (`:167-178`) | (`:125-136`) |
| Migrations | image CMD only | same |
| Ingress | GCE: `/api`, `/ws` (separate `openbox-backend-ws` Service plus BackendConfig with 3600 s timeout), `/health`, `/` (`:239-305`) | nginx ingress, 3600 s timeouts, 50m body: `/api`, `/ws`, `/health`, `/` (`:200-243`) |
| Legacy frontend nginx | kube-dns resolver with a hard-coded service address (`frontend/nginx.conf:1,14,21`) | — |

## 6. Makefile
| Target | Effect | Cite |
|---|---|---|
| `backend` / `dev` / `start` / `migrate` | Run `backend_entrypoint.py` (migrate, then uvicorn); `start` does `uv sync`, `npm install`, nohup and curl `/health` | `:13-50,93-94` |
| `deploy` | `git pull` + `make start` (host processes, not Docker) | `:63-65` |
| `build` / `up` / `down` / `clean` | compose build/up/down; `clean` runs **`down --volumes`** | `:67-78` |
| `retire-legacy-worker` | Removes containers labelled with compose service `backend-worker` for this checkout; run before dev, start, up and migrate | `:96-103` |
| `k8s-apply(-aks)` | Deletes Deployment `openbox-backend-worker`, then applies the manifest | `:105-111` |
| `test` / `test-isolation` | `uv run pytest tests/ -v` (needs `uv sync --extra test`) | `:113-117,83-85` |

---

## 7. Blob storage (`backend/blob/*`)

### 7.1 Implementations
| Class | Subclasses `IBlobStorage`? | upload / download | Notes | Cite |
|---|---|---|---|---|
| `LocalBlobStorage` | no | `(key, bytes or AsyncIterator, metadata=None)`; download is an async generator of 64 KiB chunks | Default base `/opt/openbox/blobs`; writes a `<key>.__meta__.json` sidecar; presigned URL is `file://`; sync FS calls for exists/delete | `local_blob.py:14-86` |
| `AzureBlobStorage` | no | `upload_blob(overwrite=True, metadata)`; async chunks | Lazy client; tries `create_container` on first use; SAS read URL | `azure_blob.py:11-99` |
| `GCSBlobStorage` | yes | `(key, bytes, content_type)`; download returns bytes | Sync client in a 4-thread executor; default Google credentials | `gcs_blob.py:15-62` |
| `IBlobStorage` | — | `content_type`, bytes | Local and Azure do not follow it | `interfaces.py:5-26` |
| `BlobSyncService` | — | — | No importers (dead code) | `sync.py:108-298` |

`upload_bytes` and `download_bytes` smooth over these differences by inspecting signatures (`payload.py:50-65`).

### 7.2 Provider selection (`trajectory/payload.py:25-47`)
The order is:
1. The `set_storage` override.
2. A cached instance.
3. If `blob_provider=="local"` **or no JWT**: `LocalBlobStorage`, using `BLOB_LOCAL_PATH`, or `.openbox/trajectory-blobs` on desktop.
4. `gcs`: uses `GCS_BUCKET`.
5. `azure`: raises `RuntimeError` if `BLOB_AZURE_CONNECTION_STRING` is empty.
6. Anything else raises.

There is no OSS option.

### 7.3 Every blob usage outside `backend/trajectory`
| Use | Cite |
|---|---|
| Legacy k8s sandbox backup, only when `blob_provider=="gcs"` with `gcs_bucket`. **This is the only non-trajectory consumer of the global setting** | `sandbox/kubernetes.py:818-835` |
| Acceptance server: `blob_provider="local"`, `LocalBlobStorage`, `set_storage` | `scripts/trajectory_dev_server.py:39-40,62-71` |
| Admin API reads blobs through `download_bytes` | `api/admin_trajectories.py:14,191` |
| Config, env example, manifests, tests | `core/config.py:548-552,849-853`; `.env.example:102-103,153-158`; `k8s/base.yaml:123-134`; `k8s/aks.yaml:84-85`; `tests/unit/test_config.py:24-25`; `tests/integration/test_trajectory_storage.py:19-25,44,56` |
| Policies | **None.** No `get_storage`, `blob_provider` or `LocalBlobStorage` in `permission/`, `skill/`, `billing/`, `publish/` or any policy module |
| Not blob storage: `storage/storage.py` is a kv_store (DB/file) layer that also emits trajectory events | `storage/storage.py:1-60,70-94` |

---

## 8. OSS client (`backend/core/oss.py`)

### 8.1 Operations
| Operation | Supported | Details | Cite |
|---|---|---|---|
| Presigned PUT | yes | OSS V1 query signature; Content-Type is part of the signature | `:56-98` |
| Presigned GET | yes | Optional `response-content-disposition` | `:100-111` |
| Presigned HEAD / DELETE | yes | | `:113-117` |
| Server-side HEAD | yes | httpx, 15 s; returns size, mime, etag | `:119-135` |
| Server-side copy | yes | PUT with signed `x-oss-copy-source`, 60 s; returns None on failure | `:137-157` |
| Server-side DELETE | yes | 15 s; true on 200/204 | `:159-166` |
| Server-side PUT or GET of bytes | **no** | Callers presign, then call httpx themselves (`tool/image_gen.py:450-458`; `sandbox/assets.py:176-188`; `trajectory/artifacts.py:18-25`; `api/assets.py:384`) | — |
| List objects | **no** | | — |
| Multipart upload | **no** | | — |
| Forbid-overwrite, tagging, batch delete, V4 signing | **no** | | — |
| STS `security-token` | **no** | Loaded from the CLI profile but never used in signing | `core/aliyun.py:36-37` |

### 8.2 Endpoints, credentials, HTTP
- `get_oss()` builds a new client and reloads credentials on every call. It reads `OSS_BUCKET` (missing raises `OssNotConfigured`, which maps to 503), `OSS_REGION`, and `OSS_ENDPOINT` (default `oss-{region}.aliyuncs.com`) (`oss.py:169-181`).
- Hosts:
  - Public host is `{bucket}.{endpoint}`.
  - The internal host `{bucket}.oss-{region}-internal.aliyuncs.com` applies only when the endpoint ends in `.aliyuncs.com`, and only to presigned PUT/GET with `internal=True`.
  - Server-side HEAD, copy and DELETE always use the public host (`oss.py:36-50,84,113-166`).
- Credentials:
  - First `ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET` (or `ALICLOUD_*`).
  - Then the aliyun CLI JSON at `ALIYUN_CLI_CONFIG` or `~/.aliyun/config.json`, profile from `ALIBABA_CLOUD_PROFILE`, then current, then default (`core/aliyun.py:17-38`).
  - Production mounts `secrets/aliyun-config.json` (`DEPLOY.md:957`).
- HTTP: `httpx` imported lazily; a new `AsyncClient` per call; proxy env vars are honoured by default (`oss.py:121-124,144-152,161-165`).
  - Trajectory media fetch sets `trust_env=False, follow_redirects=False` (`artifacts.py:22`).
  - Only the dev entrypoint strips proxy variables (`backend_entrypoint.py:23-40`); the container CMD does not.

### 8.3 Callers
- API: `api/assets.py:64,132,176,273,296,327,352,384,437`; `api/sessions.py:1122`; `api/platform_accounts.py:291`.
- Tools: `image_gen`, `video_production`, `video_analyze`, `video_compose`, `douyin_publish`, `computer`, `share_file`, `view_image`.
- Other: `sandbox/assets.py:95,168-188`; `publish/desktop_service.py:216`; `platforms/service.py:530-534`; `agent/loop.py:2138-2142`.
- Trajectory: `agent/trajectory.py:153,205`; `trajectory/artifacts.py:21`; `scripts/verify_wuying_desktop.py:92-142`.

---

## 9. Configuration

### 9.1 `OpenBoxConfig` fields and env vars (`core/config.py`)
| Field | Default | Env | Cite |
|---|---|---|---|
| `oss_bucket` / `oss_region` / `oss_endpoint` / `oss_user_quota_bytes` | `""` / `cn-hangzhou` / `""` / 5 GiB | `OSS_BUCKET` / `OSS_REGION` / `OSS_ENDPOINT` / `OSS_USER_QUOTA_BYTES` (int) | `:536-541,841-844,889` |
| `database_url` | `postgresql+asyncpg://openbox:openbox@localhost:5432/openbox` | `DATABASE_URL` | `:544,845` |
| `db_pool_size` / `db_pool_overflow` | 10 / 20 | `DB_POOL_SIZE` / `DB_POOL_OVERFLOW` (int) | `:545-546,846-847,886` |
| `redis_url` | `redis://localhost:6379/0` | `REDIS_URL` | `:547,848` |
| `blob_provider` | `azure` | `BLOB_PROVIDER` | `:548,849` |
| `blob_azure_connection_string` / `blob_azure_container` | `""` / `ads-staging` | `BLOB_AZURE_CONNECTION_STRING` / `BLOB_AZURE_CONTAINER` | `:549-550,850-851` |
| `blob_local_path` / `gcs_bucket` | `/opt/openbox/blobs` / `""` | `BLOB_LOCAL_PATH` / `GCS_BUCKET` | `:551-552,852-853` |
| `jwt_secret` (switches server vs desktop mode) | `""` | `JWT_SECRET` | `:560,855` |

Loading order: config files, then `OPENBOX_CONFIG_CONTENT`, then `{env:VAR}` substitution, then the explicit env map. The config is a singleton (`:669-697,778-936,943-986`). Booleans parse as `== "true"` (`:898-902`).

### 9.2 Trajectory env: **not part of `OpenBoxConfig`**; read with `os.getenv` at call time
| Var | Default | Cite |
|---|---|---|
| `TRAJECTORY_RECORDING_ENABLED` / `TRAJECTORY_RECORD_USER_IDS` | false / empty means all users | `trajectory/config.py:5-20` (flag accepts `1/true/yes/on`) |
| `TRAJECTORY_ADMIN_ENABLED` / `TRAJECTORY_ADMIN_USER_IDS` | false / empty | `:23-24` |
| `TRAJECTORY_INLINE_BYTES` | 65536 | `recorder.py:150` |
| `TRAJECTORY_BATCH_MS` | 50 | `recorder.py:326` |
| `TRAJECTORY_PENDING_BYTES` / `TRAJECTORY_TOTAL_PENDING_BYTES` | 4 MiB / 64 MiB | `recorder.py:271,304,399` |
| `TRAJECTORY_CHECKPOINT_INTERVAL` | 1000 | `repository.py:194` |
| Test and benchmark DB URLs (`TRAJECTORY_TEST/BENCHMARK/STREAM/CRASH/CONCURRENCY_DATABASE_URL`) | guarded to local `openbox_trajectory_storage_*` | `test_trajectory_storage.py:29-32`; `trajectory/benchmark.py:37-40` etc. |

`integer()` clamps values to at least 1 (`trajectory/config.py:27-28`). The rollout table is documented in `docs/SESSION_TRAJECTORY_IMPLEMENTATION_STATUS.md:151-161`.

## 10. Dependencies (`backend/pyproject.toml`, `backend/uv.lock`)
| Package | Status | Declared where |
|---|---|---|
| httpx 0.28.1 (+h2 4.4.1) | present | direct `httpx[http2]` (`pyproject.toml:10`) |
| redis 7.1.1 (+hiredis 3.3.0) | present | direct (`:31`) |
| asyncpg 0.31.0, sqlalchemy 2.0.47 (+greenlet 3.3.2), alembic 1.18.4 | present | direct (`:27-29`) |
| uvicorn 0.40.0 (standard: uvloop 0.22.1, **watchfiles 1.1.1**) | present | direct (`:8`); watchfiles is transitive |
| aiofiles 24.1.0, azure-storage-blob 12.28.0, google-cloud-storage 3.9.0 | present | direct (`:14,33,42`) |
| aiosqlite 0.22.1 | test extra only (not in the image) | `:58-62` |
| prometheus-client 0.24.1, opentelemetry-api 1.39.1, boto3 1.42.49 | present | transitive |
| **orjson, zstandard, pyarrow, duckdb, psycopg (3), psycopg2, oss2, msgspec, xxhash, lz4, brotli** | **absent** | — |

---

## 11. frontend-v2 image and nginx
- **Template substitution.** The Dockerfile sets `ENV BACKEND_HOST=backend:8080` and copies `nginx.conf` to `/etc/nginx/templates/default.conf.template` (`frontend-v2/Dockerfile:20-21`). The official nginx image's entrypoint renders templates into `/etc/nginx/conf.d/default.conf`, substituting only env vars that are defined. So `${BACKEND_HOST}` is replaced while `$host` and `$uri` stay intact. This is behavior of the base image, not repo code, but `scripts/test-nginx.mjs:66-86` exercises it by mounting the template, setting `BACKEND_HOST`, and running `nginx -t`.
- **`nginx.conf` settings:**
  - `client_max_body_size 1g` (`:4`).
  - `resolver 127.0.0.11 valid=5s` is Docker's embedded DNS only (`:10`).
  - `set $backend_upstream http://${BACKEND_HOST}` (`:12`).
  - SPA and asset cache rules (`:14-40`).
  - `/api/` proxy (`:42-48`).
  - `/ws/` proxy with Upgrade headers (`:50-59`).
  - Because `proxy_pass` uses a variable with no URI part, the original URI and query pass through unchanged. `/ws/` sets no `proxy_read_timeout`, so nginx's 60 s default applies.
- **Existing nginx test** checks: SPA, assets, URI and body passthrough, status codes, WebSocket upgrade, and re-resolving DNS after the backend IP changes, without restarting the frontend (`scripts/test-nginx.mjs:85-161`; `package.json:14`).
- **Client side:**
  - The SPA uses same-origin `/api/admin/trajectories` (`src/features/admin-trajectories/api/endpoints.ts:18`).
  - The WS URL is `wsBase()`: `VITE_API_URL` or `location.origin` (`src/shared/config/env.ts:1-8`; `src/shared/ws/client.ts:108`; `api/socket.ts:9`).
  - The Vite dev proxy has only `/api` and `/ws`, both to 8080 (`vite.config.ts:42-45`).
- **Ignore file:** `frontend-v2/.dockerignore:9-11` excludes `e2e/` and `docs/`.

## 12. Scripts, CI, docs conventions
- **`backend/scripts`:**
  - Deploy-relevant: `backend_entrypoint.py` (migrations and uvicorn) and `trajectory_dev_server.py` (SQLite, local blob, fixture events via `append_events_in_tx`, `:33-121`).
  - DB utilities: `migrate_json_to_pg.py`, `cleanup_containers.py`.
  - Desktop, tunnel and relay helpers: `wuying_*`, `logto_tunnel.sh`. None run alembic, uvicorn or compose.
  - There is no production release script; `deploy_common.sh` lived in a session scratchpad (`DEPLOY.md:113`).
- **CI:** none. There is no `.github/`, and no other CI or orchestration files beyond the two compose files.
- **Docs conventions:**
  - Design docs are Chinese, with a status blockquote (status, date, scope) and links to companion docs (`docs/plans/trajectory/SESSION_TRAJECTORY_IMPLEMENTATION_PLAN.md:1-9`; `docs/reference/SESSION_TRAJECTORY_PROTOCOL.md:1-5`).
  - `DEPLOY.md` is a newest-first release log per environment: tag, commit, migration ids, backup path, rollback commands (`:8-45`).
  - Evidence goes in `docs/evidence/` and `docs/trajectory-verification/`.
  - The README index is in English (`README.md:156-162`).

## 13. What the admin trajectory API/WS needs at runtime (a worker serving them needs the same access)
| Dependency | Used for | Cite |
|---|---|---|
| Business DB `users` | Admin check on every request, every WS send, and every 1 s | `trajectory/auth.py:42-56`; `admin_trajectory_ws.py:84-87,103,154-157` |
| Business DB `sessions`, `users`, `workspaces` | Session list SQL join, filters, `include_unrecorded`, header metadata | `repository.py:34-40,260-280,315-378` |
| Business DB `file_assets` | Payload visibility after asset deletion | `payload.py:109-113` |
| Business DB `audit_logs` (write) | Audit rows on reads and on WS subscribe | `admin_trajectories.py:38-39`; `admin_trajectory_ws.py:125-131` |
| Redis cache | WS ticket (audience `admin_trajectories`, TTL 30 s, atomic claim); JWT blacklist | `admin_trajectory_ws.py:21,34-38`; `auth/ticket.py:23-60`; `auth/middleware.py:57-61` |
| JWT secret | Bearer token decode | `auth/middleware.py:48-55`; `auth/__init__.py:9-16` |
| Redis bus | `trajectory.available` fan-out to the WS | `bus.py:29,65-95`; `recorder.py:250-253`; `admin_trajectory_ws.py:159` |
| Contract details to keep | `Cache-Control: no-store` route class; 16 subscriptions per socket; close codes 4401/4403; export download path | `trajectory/auth.py:15-28`; `admin_trajectory_ws.py:22,65-75,166-178`; `export.py:36-38` |

## 14. Tests that assume the current infrastructure
| Test | Assumption | Cite |
|---|---|---|
| `test_migration_heads.py` | One `alembic.ini` default section, one head | `:17-29` |
| `test_database_readiness.py` | Creates trajectory tables to satisfy readiness | `:48-60` |
| `test_trajectory_migration.py` | Imports `f6a8c0e2b4d6` from the business chain | `:8-22` |
| `tests/conftest.py` | Autouse in-memory SQLite `Base.metadata.create_all` (includes trajectory tables) | `:36-47` |
| `test_trajectory_storage.py` | Monkeypatches `db.base._engine` and `_session_factory`; `set_storage(MemoryBlob)` | `:27-56` |
| `test_config.py` | Blob defaults (`azure`, `ads-staging`) | `:24-25` |

---

## Migration notes

### A. What moves where
| Today (backend process, business PG) | Target |
|---|---|
| 7 trajectory tables and migration `f6a8c0e2b4d6` in the business chain (`db/models/__init__.py:52-53`) | Trace DB with its own Alembic env. A later business revision (after head `c7e9b1d3f5a7`) drops the tables once data is copied. **Keep** the business columns `session_executions.trace_context` and `cron_runs.trace_context` (`base.py:330-334`) |
| Archive, checkpoint and purge loops plus `resume_exports`/`stop_exports` in the lifespan (`main.py:79-90,107-117`) | Worker process loops (archive becomes OSS segments, checkpoints become async projection batches) |
| Admin routers (`main.py:332-335`) | Worker HTTP/WS on the same paths (§13) |
| Trajectory entries in `_READINESS_SCHEMA` (`base.py:335-341`) | Removed from the backend; worker `/health` checks the trace schema, spool and blob store |
| `get_storage` on the global `BLOB_*` settings (`payload.py:25-47`) | Worker-only blob provider with its own settings (§F) |
| Recorder transaction writes, `after_commit` publish, stream queues (`recorder.py:140-169,243-363`) | Backend `emit()` → bounded queue → writer thread → JSONL spool. Hook

 via a per-session `after_commit` listener (existing example: `notifications/inbox.py:121-133`); rollback discards pending events (today: `recorder.py:256-262`) |
| `trajectory.available` published from the backend commit (`recorder.py:250-253`; `lifecycle.py:26-28`) | Published by the worker after projection; the WS lives in the worker, so local dispatch is enough |
| Asset and media bytes copied into `trajectory_payloads.content` (`api/assets.py:279-306`; `artifacts.py:18-25,97,194`) | Store a reference only: `file_assets.id`, `oss_key`, size, and an etag from `OssClient.head` (`oss.py:119-135`). `file_assets` has no hash column (`db/models/file_asset.py:20-46`) |
| Deletion inside business transactions (`session/session.py:340-341`; `api/assets.py:292-294,433-435`) | An emitted fact after commit; the worker applies tombstones and GC later, so deletion becomes eventually consistent |

### B. Adding a second database engine
1. **Separate module and base.** Create a new module, for example `backend/trajectory/store/database.py`, with:
   - its own `TraceBase(DeclarativeBase)`, copying `type_annotation_map` and reusing `JSONType` (`base.py:27-58`);
   - `init_trace_engine(url, pool_size, overflow)`, `trace_session()` with the same commit, rollback and close behavior as `base.py:451-470`, and `close_trace_engine()`.
2. **Don't reuse the business singletons.** Leave `db.base._engine` and `_session_factory` alone: `storage/storage.py:27-33` checks them, and tests monkeypatch them (`test_trajectory_storage.py:39-40`).
3. **Unhook the models.** Remove `db.models.trajectory` from `db/models/__init__.py:52-53`. Otherwise the desktop `create_all` (`base.py:113-116`), the test `create_all` (`conftest.py:44-47`) and Alembic autogenerate keep treating the tables as business tables.
4. **Config.** Add `TRAJECTORY_DATABASE_URL` and pool sizes, either through the explicit env map (`config.py:845-848,886`) or in `trajectory/config.py`. The business backend must never read it.
5. **Scope the listeners.** The class-level listeners on `sqlalchemy.orm.Session` (`recorder.py:243-262`) fire for every session in the process, trace DB included. Scope them by a `session.info` key, or give the trace sessionmaker its own `sync_session_class`.
6. **Connection budget.** The business engine uses 10+20 connections per process (`config.py:545-546`). Add the worker pool and Alembic's NullPool connections, and check Postgres `max_connections` on the single gw2 Postgres.
7. **Creating the database.** The Postgres image creates only `POSTGRES_DB` on the first init of an empty volume (`docker-compose.yml:46-49`; `docker-compose.dev.yml:6-9`).
   - Existing volumes need a one-off `CREATE DATABASE`, which cannot run inside a transaction, or a separate server.
   - Add the new database to the release backup; today `preflight.dump` covers only `openbox` (`DEPLOY.md:19-20`).
8. **Search extensions.** Nothing exists today (§1.3). The trace migrations must run `CREATE EXTENSION IF NOT EXISTS pg_trgm`; tsvector is built in. pg_trgm is a trusted extension on PG 13+, but verify the role's privileges and the Postgres image version on production, since the production compose is not in the repo.

### C. Adding a second Alembic environment
- **Layout, option 1:** a separate `backend/alembic_trajectory.ini` (`script_location = trajectory/migrations`) with `trajectory/migrations/{env.py,script.py.mako,versions/}`, run with `alembic -c alembic_trajectory.ini upgrade head`.
- **Layout, option 2:** a `[trajectory]` section in `alembic.ini`, run with `alembic -n trajectory upgrade head`.
- Either way, leave the default `[alembic]` section unchanged so `test_migration_heads.py:28` keeps testing the business chain. Add a matching one-head test for the trace chain.
- **env.py pitfall:** read `TRAJECTORY_DATABASE_URL` and fail if it is unset. **Never fall back to `DATABASE_URL`.** Copying `env.py:22` would silently migrate the business DB, because every container gets `DATABASE_URL` (`docker-compose.yml:17`; `DEPLOY.md:980`).
- In the trace env.py:
  - set `target_metadata = TraceBase.metadata` and import only trace models;
  - consider a distinct `version_table` as a guard in case the URL ever points at the business DB;
  - keep the JSONB/SQLite `render_item` (`env.py:37-42`) only if SQLite trace tests remain.
- **Who runs it:** the worker command runs only the trace chain. The backend CMD keeps the business chain (`backend/Dockerfile:26`). Extend `backend_entrypoint.py:42-65` (`--migrate-only`) and `make migrate` (`Makefile:93-94`) so dev runs both.
- **Business side:**
  - Order: move the data (§I) first, then add a revision after `c7e9b1d3f5a7` that drops the 7 tables.
  - The documented rollback (`DEPLOY.md:45`, downgrade to `e4f6a8b0c2d4`) assumes those tables are disposable; rewrite the runbook.
- **Tests to update:** port `test_trajectory_migration.py:8-22` to the new chain.

### D. Adding the worker service (same image)
**Compose.** The production file lives on both servers (`DEPLOY.md:946-970,1086-1090`), so apply this to `/opt/openbox/docker-compose.yml` and the override on AWS and gw2, and mirror it in the repo's dev compose:
```yaml
services:
  backend:
    volumes: ["trajectory-spool:/var/lib/openbox/trajectory-spool"]
    environment: { TRAJECTORY_SPOOL_DIR: /var/lib/openbox/trajectory-spool }
  trajectory-worker:                  # never "backend-worker" (Makefile:96-103 deletes it)
    image: openbox-backend:${OPENBOX_IMAGE_TAG}   # also pin it in the override, like backend
    command: ["/bin/sh","-ec","alembic -c alembic_trajectory.ini upgrade head\nexec python -m trajectory.worker"]
    env_file: [config/backend.env]
    environment:
      TRAJECTORY_DATABASE_URL: postgresql+asyncpg://openbox:${OPENBOX_DB_PASSWORD}@postgres:5432/openbox_trajectory
      DATABASE_URL: postgresql+asyncpg://openbox:${OPENBOX_DB_PASSWORD}@postgres:5432/openbox  # only if §13 access is kept
      REDIS_URL: redis://redis:6379/0
    volumes: ["trajectory-spool:/var/lib/openbox/trajectory-spool"]
    depends_on: [postgres, redis]
    healthcheck: { test: ["CMD","curl","-fsS","http://127.0.0.1:8090/health"] }   # curl exists (Dockerfile:7-9)
  frontend:
    environment: { TRAJECTORY_HOST: "trajectory-worker:8090" }
volumes: { trajectory-spool: {} }
```
- **Command override is mandatory.** Otherwise the image CMD runs business migrations and starts a second uvicorn.
- **One writer only.** Use no `--scale`. Also take a Postgres advisory lock at worker start, because `compose run` or a stray container can still start a second writer.
- **Release order.** Deploy the worker first (it must read both old and new spool formats; put a version field in each record), then the backend, one at a time with `--no-deps` (`DEPLOY.md:1046-1054`). Pin the worker's image tag in the override just like the backend's (`DEPLOY.md:626-633,875-891`).
- **Dev caveat.** `make clean` runs `down --volumes` (`Makefile:78`), which wipes the spool. That is acceptable in dev only.

**k8s** (frozen legacy; update only if it gets revived):
- Either run the worker as a sidecar in the backend pod sharing an `emptyDir` spool (the spool is lost if the pod is deleted), or as its own Deployment with an RWX PVC, `replicas: 1` and `strategy: Recreate`.
- Add a Service on 8090 and ingress paths `/api/admin/trajectories` and `/ws/admin/trajectories` pointing at it; the longest Prefix wins.
- On GKE the WS path needs a BackendConfig timeout like `base.yaml:239-264`.
- Don't use the name `openbox-backend-worker` (`Makefile:105-111`).

**Desktop mode** (no JWT) has no second container (`main.py:40-46`; `base.py:109-123`). Choose between an in-process worker and recording off.

### E. Routing admin trajectory paths in nginx, with a safe default
- **Config.** Add to `frontend-v2/nginx.conf` inside the server block. nginx picks the longest matching prefix, and there are no regex locations, so these win over `/api/` and `/ws/`:
```nginx
set $trajectory_upstream http://${TRAJECTORY_HOST};
location /api/admin/trajectories/ {          # sessions/*, ticket, export download
    proxy_pass $trajectory_upstream;
    proxy_set_header Host $host;  proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;  proxy_set_header X-Forwarded-Proto $scheme;
}
location = /ws/admin/trajectories {           # query string is not part of location matching
    proxy_pass $trajectory_upstream;
    proxy_http_version 1.1;  proxy_set_header Upgrade $http_upgrade;  proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;  proxy_read_timeout 3600s;
}
```
- **Default is required.** Add `ENV TRAJECTORY_HOST=backend:8080` next to `frontend-v2/Dockerfile:20`. envsubst leaves undefined variables untouched, so nginx would see `${TRAJECTORY_HOST}` as an unknown nginx variable and refuse to start. With the default, existing deployments keep routing to the backend until compose sets `trajectory-worker:8090`.
- **DNS.** Reuse `resolver 127.0.0.11` (`nginx.conf:10`); it is Docker-only.
- **Tests.** Extend `scripts/test-nginx.mjs:48-80`: start a second fixture with a `trajectory-worker` alias, set `TRAJECTORY_HOST`, and assert that trajectory paths and the WS upgrade reach it while `/api/echo` still reaches `backend`. Also check that the container starts when `TRAJECTORY_HOST` is not set.
- **Dev proxy.** Add `"/api/admin/trajectories"` and `"/ws/admin/trajectories"` entries before `/api` and `/ws` in `vite.config.ts:42-45`.
- **Frontend code** needs no change; it is same-origin (`env.ts:6-8`).

### F. Is a trajectory-specific blob setting needed? Yes.
Reasons:
- **The global setting fits nothing today.** `BLOB_PROVIDER` defaults to `azure` with no connection string (`config.py:548-549`). Its only other consumer is the legacy k8s GCS backup (`sandbox/kubernetes.py:819`), and there is no OSS provider (`payload.py:33-46`).
- **The OSS settings are shared with user features.** `OSS_*` drives browser presigned URLs, per-user asset quota (`config.py:537-541`) and tools. Trajectory data needs its own prefix, lifecycle and retention rules, the internal endpoint, and possibly a separate bucket or credentials.
- **A shared endpoint would break browsers.** Setting a shared `OSS_ENDPOINT` to the internal host would also make browser-facing URLs internal (`oss.py:36-50`).

Proposed settings, read only by the worker:

| Setting | Default |
|---|---|
| `TRAJECTORY_BLOB_PROVIDER` (`oss` \| `local`) | `local` in dev and desktop |
| `TRAJECTORY_OSS_BUCKET` | `OSS_BUCKET` |
| `TRAJECTORY_OSS_REGION` | `OSS_REGION` |
| `TRAJECTORY_OSS_ENDPOINT` | `oss-{region}-internal.aliyuncs.com` on Alibaba |
| `TRAJECTORY_OSS_PREFIX` | `trajectories/` |
| `TRAJECTORY_BLOB_LOCAL_PATH` | a path on a volume, not the container filesystem (`local_blob.py:15`) |

`OssClient` gaps to fill, either in `core/oss.py` or with the `oss2` SDK (not in the lock):
- server-side PUT of bytes with `x-oss-forbid-overwrite`, for content-addressed idempotent writes;
- server-side GET;
- ListObjectsV2, for GC and segment listing;
- multipart upload, only if a single segment can exceed what one PUT allows;
- internal host for server-side calls;
- STS `security-token` support.

Also stop reloading credentials on every call (`oss.py:177-181`).

### G. Dependency additions
| Package | Need |
|---|---|
| `zstandard` | Required (zstd blobs and segments) |
| `orjson` | Optional, for JSONL speed. Keep `canonical()` hashing byte-identical or keep stdlib json for hashing |
| `watchfiles` | Already present transitively. Declare it directly if used to tail the spool, and keep a polling fallback for Docker Desktop mounts |
| `pyarrow`, `duckdb` | Not needed unless cold segments become Parquet or analytics |
| `psycopg[binary]` | Only if some worker path needs a sync driver in a thread; asyncpg covers async |
| `httpx`, `redis`, `asyncpg`, `alembic` | Already present |

After changing `pyproject.toml`, run `uv lock`: the Dockerfile builds with `--frozen` (`backend/Dockerfile:16`). Production loads images offline (`DEPLOY.md:1006-1016`), so build for `linux/amd64`; zstandard and orjson ship wheels for it.

### H. What breaks
1. `/health` returns 503 once the tables are dropped, until `base.py:335-341` is removed (§14 readiness test).
2. `test_migration_heads.py`, `test_database_readiness.py`, `test_trajectory_migration.py`, `conftest.py` and `test_trajectory_storage.py` (§14).
3. `scripts/trajectory_dev_server.py:33-121` (SQLite, local blob, `append_events_in_tx`).
4. Business code that writes trajectory rows inside its own transactions:
   - `session/session.py:340-341,591-592,737-738`, `session/fork.py:196-197`, `session/revert.py:20-26`
   - `api/assets.py:277-307,433-434`, `storage/storage.py:70-94`, `trajectory/jobs.py:14-66`
5. **Permission recovery reads `trajectory_events`.** When the Redis wake-up for a permission reply is lost, the durable fallback reads the committed approval from `trajectory_events` (`permission/permission.py:226-247`). It needs a business-DB source before the tables move.
6. The stale-run check lives in the recorder (`recorder.py:182-187`). It must move to question/runtime code (target item 3) before the recorder is removed.
7. Session-list SQL joins with business tables (`repository.py:323-371`) cannot span two databases. Either denormalize owner, title, workspace and status into the trace DB via events, or query both and merge (§13).
8. Worker auth still needs `JWT_SECRET`, Redis, business `users`, and an `audit_logs` write (§13).

### I. Risks
- **Existing data.** Production has recorded since 2026-09-12 (`DEPLOY.md:31`); an operator memory note outside the repo says recording has been on for all users since 2026-09-14. Staged `content` bytes may still sit in the business PG if the blob provider is unconfigured (§7.2). Copying events, payloads and checkpoints into the trace DB and OSS needs a resumable backfill and a cutover watermark.
- **Spool durability.** The spool sits on one host disk: add a disk budget and alerts, and add it to backups. Overflow must emit `recording.gap`, not block.
- **Version skew** between backend and worker, because services are swapped independently with `--no-deps`.
- **Two writers** during a recreate or a stray `compose run` → advisory lock.
- **OSS traffic.** Server-side OSS calls go over the public endpoint today (`oss.py:113-166`), and gw2 is in cn-shanghai (`DEPLOY.md:444`).
- **WebSocket timeouts.** nginx's 60 s idle default on the new WS location; set `proxy_read_timeout`.
- **No CI.** The migration-head and readiness tests only protect anyone who runs them by hand.
- **Shutdown.** The spool writer must flush and fsync within compose's stop timeout. Today the lifespan awaits `flush` (`main.py:79-90`), and the bus is already closed by then (`main.py:235-239`).

### J. Open questions
1. What are `BLOB_PROVIDER` and `BLOB_*` in gw2 and AWS `backend.env`? How many `trajectory_payloads` rows have `storage_status='pending'`, and how many `content` bytes sit in the business PG?
2. Same Postgres container with a second database, or a separate server? Who creates the database and extensions on existing volumes?
3. Should the production compose finally go into the repo (`DEPLOY.md:1086-1090`) as part of this change?
4. Worker access to the business DB (read users, sessions, workspaces, file_assets; write audit_logs) versus denormalized metadata and audits kept in the trace DB?
5. Desktop mode: run the worker in-process, or turn recording off?
6. Update the k8s manifests or keep them frozen?
7. Retention and budget targets and OSS lifecycle rules; a separate trajectory bucket or a prefix in the asset bucket?
8. Replacement for the `trajectory_events` permission fallback (`permission/permission.py:226-247`) before cutover?