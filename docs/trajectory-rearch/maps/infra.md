# Infrastructure migration notes — historical excerpt

> 改造前调查摘录，原段落和行号保留；阅读顺序见[源码映射目录](README.md)。

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