# gw2 runbook: trajectory worker topology

> Status: prepared with the trajectory re-architecture (SPEC §12, work packages w1-ops and w3-ops); **not released yet**.
> Scope: the Alibaba Cloud production host gw2 (`/opt/openbox`, cn-shanghai). The AWS development host uses the same files.
> Companions: [docs/DEPLOY.md §五](../../docs/DEPLOY.md) (Chinese summary; the release log stays there),
> [SPEC](../../docs/trajectory-rearch/SPEC.md) §8, §12, §13.

Every production step below follows the existing release rules of docs/DEPLOY.md ("发布可用性要求"):
back up first, change one service at a time with `--no-deps`, never a bare `docker compose up -d`,
wait for healthy before the next service, and recreate backend or postgres only while no run holds an
execution lease.

## 1. Topology

```
browser → Lighthouse proxy → frontend (nginx :80)
                               ├─ /api/admin/trajectories/*, /ws/admin/trajectories → trajectory-worker:8090
                               ├─ /api/*, /ws/*                                      → backend:8080
                               └─ /                                                  → static SPA
backend ── JSONL append ──► volume trajectory-spool (backend + worker only) ──► trajectory-worker
trajectory-worker ──► postgres/openbox_trace (role openbox_trace) · OSS <bucket>/trajectories/ · redis ·
                      backend /api/internal/trajectory/{viewer,audit}
host systemd timers ──► CloudMonitor custom metrics · OSS backups/postgres/ · docker image prune
```

What `docker-compose.trajectory.yml` changes:

| Service | Change | Why |
|---|---|---|
| `trajectory-worker` (new) | backend image, command `alembic -c alembic_trajectory.ini upgrade head && exec python -m trajectory.worker`, `env_file: config/backend.env`, `TRAJECTORY_DATABASE_URL=postgresql+asyncpg://openbox_trace:${OPENBOX_TRACE_DB_PASSWORD}@postgres:5432/openbox_trace`, `DATABASE_URL=""`, `TRAJECTORY_WORKER_MODE=external`, `TRAJECTORY_BLOB_PROVIDER=oss`, `TRAJECTORY_BACKEND_INTERNAL_URL=http://backend:8080`, `ALIYUN_CLI_CONFIG=/run/secrets/aliyun-config.json`; spool volume, aliyun secret (ro); starts after healthy postgres and redis; healthcheck on :8090; 1 CPU, 1 GiB; log rotation | Ingest, projection, archive, admin API. The worker connects as the dedicated role `openbox_trace`, which owns only the trace database; the blank `DATABASE_URL` keeps the business connection string of `config/backend.env` out of the worker. The command override is mandatory: the image CMD would run the business migrations and a second uvicorn. One writer only: never `--scale`, never name it `backend-worker` (`make retire-legacy-worker` deletes that service). |
| `backend` | `TRAJECTORY_WORKER_MODE=external`, spool volume | Business requests only append to the spool. `TRAJECTORY_WORKER_MODE=off` switches the whole pipeline off: no emitter, no metadata sync and no worker. |
| `frontend` | `TRAJECTORY_HOST=trajectory-worker:8090` | nginx routes the two admin trajectory paths to the worker (the image default still points at the backend). |
| `postgres` | `mem_limit: 2g`; `shared_buffers=512MB`, `effective_cache_size=1GB`, `shared_preload_libraries=pg_stat_statements`, `pg_stat_statements.track=all`, `max_connections=200` | Room for the trace database and its pool; query statistics for the isolation check. Applying it recreates postgres. |
| volumes | `trajectory-spool` | The spool may contain unredacted data; it is mounted into backend and worker only. |

## 2. Files

| File | Where it runs | Purpose |
|---|---|---|
| `docker-compose.trajectory.yml` | gw2 | The overlay above. |
| `docker-compose.base.example.yml`, `docker-compose.override.example.yml`, `env.example`, `config/backend.env` | developer machine | Sanitized copies of the gw2 files as read on 2026-09-14 (`name: openbox`, images, commands, health checks, `condition: service_healthy`, memory limits, logging, volumes, the override's image pins, port `127.0.0.1:8080` and per-file secret mounts) with placeholders for secrets and host names. `config/backend.env` is a placeholder so that the overlay validates in a checkout; `.gitattributes` keeps it out of the release bundle. `backend/tests/unit/test_trajectory_ops_deploy_assets.py` validates the overlay against these files with `docker compose config`. Compare them with the real server files before a release. |
| `oss-lifecycle.xml` | operator machine | OpenBox-managed lifecycle rules (§9). |
| `scripts/lib.sh` | both | Shared helpers (sourced). |
| `scripts/create-trace-db.sh` | gw2 | Role `openbox_trace`, database `openbox_trace`, `pg_trgm`, `pg_stat_statements` (idempotent). |
| `scripts/push-metrics.sh` | gw2, minute timer | Host metrics + worker metrics → CloudMonitor (§8). |
| `scripts/pg-backup.sh` | gw2, daily timer | `pg_dump -Fc` of `openbox` and `openbox_trace` → OSS (§10). |
| `scripts/prune-images.sh` | gw2, weekly timer | Image cleanup, dry run unless `--execute`. |
| `scripts/install-timers.sh` | gw2 | Installs the systemd units in `systemd/` and the metrics instance (§7). |
| `scripts/apply-oss-lifecycle.sh` | operator machine | Merges `oss-lifecycle.xml` into the bucket's rules, dry run unless `--execute`. |
| `scripts/setup-alarms.sh` | operator machine | CloudMonitor alarm rules, dry run unless `--execute`. |
| `scripts/restore-check.sh` | gw2 | Restores a backup into a scratch database and drops it (§10), dry run unless `--execute`. |
| `scripts/drill-*.sh`, `scripts/rebuild-trace-db.sh` | gw2 | Drills (§11), dry run unless `--execute`. |
| `backend/trajectory/ops/{cms,backup,lifecycle,rebuild,deletion,latency}.py` | worker image / operator | Python side of the scripts and of the release comparison (§5 step 9); `python -m trajectory.ops.<module> --help`. |

Every script prints its usage with `--help`. Scripts on gw2 read `OPENBOX_DIR` (default `/opt/openbox`).

## 3. Server layout and compose files

gw2 has no source checkout. The release bundle carries `deploy/gw2` next to the images:

```bash
# operator machine, repository at the release commit
TAG=<date>-<batch>-<sha>
git archive --format=tar.gz -o deploy-gw2-$TAG.tgz HEAD deploy/gw2
aliyun ossutil cp -f deploy-gw2-$TAG.tgz oss://<private-bucket>/_deploy-tmp/$TAG/deploy-gw2.tgz --acl private --region cn-shanghai
aliyun ossutil presign oss://<private-bucket>/_deploy-tmp/$TAG/deploy-gw2.tgz --expires-duration 2h \
  -e https://oss-cn-shanghai-internal.aliyuncs.com --region cn-shanghai

# gw2
curl -sS -o /tmp/deploy-gw2.tgz '<presigned URL>'
tar -xzf /tmp/deploy-gw2.tgz -C /opt/openbox && rm -f /tmp/deploy-gw2.tgz
ls -l /opt/openbox/deploy/gw2/scripts   # executable bits come from git
```

Delete the `_deploy-tmp/` objects afterwards, as for images.

`/opt/openbox/.env` holds, after this release, `OPENBOX_IMAGE_TAG`, `OPENBOX_DB_PASSWORD`, `OPENBOX_TRACE_DB_PASSWORD` and
`COMPOSE_FILE` (`env.example`). `OPENBOX_IMAGE_TAG` is stale on gw2 (`20260912-videourl-961075e` on 2026-09-14):
since 2026-09-07 the override pins every image and its pins win, so images are switched in the override, never in `.env`.

Compose file order, set once in `/opt/openbox/.env` (release step 4):

```bash
COMPOSE_FILE=docker-compose.yml:deploy/gw2/docker-compose.trajectory.yml:docker-compose.override.yml
```

- Every plain `docker compose …` in `/opt/openbox` then includes the overlay. Without it, a routine
  `docker compose up -d --no-deps backend` would recreate the backend without the spool volume.
- The override stays last so its image pins win. It also publishes the backend on `127.0.0.1:8080` (host scripts
  call it there) and mounts the secrets file by file. Pin the worker exactly like the backend:

  ```yaml
  services:
    backend:
      image: openbox-backend:<TAG>
    trajectory-worker:
      image: openbox-backend:<TAG>
  ```

- Relative paths resolve against `/opt/openbox` (the first file's directory).

Checks after editing `.env` or the override:

```bash
cd /opt/openbox
docker compose config --services   # backend frontend postgres redis trajectory-worker
docker compose config --volumes    # includes trajectory-spool and blob-data
docker compose config --images     # trajectory-worker has the backend's tag
```

`docker compose config` warns when `OPENBOX_TRACE_DB_PASSWORD` is not set; the worker would then fail to connect.

## 4. Preflight checklist

1. **The release commit is merged to `origin/main` before anything changes on gw2**, and the override's current image
   commits are contained in it (lesson of 2026-09-10): `git fetch origin && git merge-base --is-ancestor <release commit> origin/main`
   exits 0. Otherwise the next routine deploy from `main` by anyone else breaks the backend: a main-built image cannot
   find the business revisions `d3b5f7a9c1e2` and `e5c7a9b1d3f4` in its migration scripts, so its `alembic upgrade head`
   fails and the backend never starts.
2. Both migration chains have one head, checked in the new image:
   `docker run --rm --entrypoint alembic openbox-backend:<TAG> heads` and
   `docker run --rm --entrypoint alembic openbox-backend:<TAG> -c alembic_trajectory.ini heads`.
3. `config/backend.env` has non-empty `JWT_SECRET` and `INTERNAL_API_TOKEN` (the worker authenticates
   viewers through the backend's internal endpoints), `OSS_BUCKET` and `OSS_REGION` (or `TRAJECTORY_OSS_*`),
   and the shared recording flags (`TRAJECTORY_RECORDING_ENABLED`, `TRAJECTORY_RECORD_USER_IDS`,
   `TRAJECTORY_ADMIN_ENABLED`, `TRAJECTORY_ADMIN_USER_IDS`). On the AWS development host it also has
   `TRAJECTORY_OSS_INTERNAL=false`: the internal OSS endpoint is reachable only inside Alibaba Cloud.
4. `.env` has a password for the trace role, 16 to 128 letters, digits, `.`, `_`, `~` or `-` (it is part of
   `TRAJECTORY_DATABASE_URL`). Add it once without printing it:

   ```bash
   cd /opt/openbox
   cp -p .env ".env.bak-$(date +%Y%m%d%H%M%S)"
   if ! grep -q '^OPENBOX_TRACE_DB_PASSWORD=' .env; then
     [ -z "$(tail -c 1 .env)" ] || echo >> .env
     printf 'OPENBOX_TRACE_DB_PASSWORD=%s\n' "$(openssl rand -hex 32)" >> .env
   fi
   grep -c '^OPENBOX_TRACE_DB_PASSWORD=' .env   # 1
   ```

5. The RAM user in `secrets/aliyun-config.json` may PUT/GET/HEAD/DELETE/LIST under `trajectories/` and
   `backups/postgres/` of the bucket and call `cms:PutCustomMetric`. The operator's own profile needs
   `oss:GetBucketLifecycle`, `oss:PutBucketLifecycle` and `cms:PutCustomMetricRule`.
6. Disk: `df -h / "$(docker info --format '{{.DockerRootDir}}')"` shows at least 20 % free (the spool budget is 2 GiB).
7. PostgreSQL: `docker compose exec postgres postgres --version` (13+ for trusted `pg_trgm`),
   `docker compose exec postgres psql -U openbox -tAc 'SHOW max_connections'`.
8. Active runs (repeat right before every backend or postgres recreation; must be 0):

   ```bash
   docker compose exec -T postgres psql -U openbox -d openbox -tAc \
     "SELECT count(*) FROM session_executions WHERE run_id IS NOT NULL AND lease_until > now()"
   ```

9. The overlay validates on a developer machine, with the `env.example` values in the environment (or copied to
   `deploy/gw2/.env`, which git ignores):
   `docker compose -f deploy/gw2/docker-compose.base.example.yml -f deploy/gw2/docker-compose.trajectory.yml -f deploy/gw2/docker-compose.override.example.yml config --quiet`
   and `cd backend && uv run pytest tests/unit/test_trajectory_ops_deploy_assets.py -q`.

## 5. Release procedure (SPEC §12.1)

Keep a terminal probing the public site during every switch, as in previous releases.

1. **Build** `linux/amd64` images from the release commit, from scratch: the release adds Python dependencies, so
   no image may be layered on a running tag. Pin the nginx runtime of the frontend:

   ```bash
   docker build --platform linux/amd64 --pull --no-cache -f backend/Dockerfile -t openbox-backend:$TAG .
   docker build --platform linux/amd64 --pull --no-cache --build-arg NGINX_IMAGE=nginx:1.31.5-alpine \
     -t openbox-frontend-v2:$TAG frontend-v2/
   docker run --rm --entrypoint python openbox-backend:$TAG -c 'import orjson, zstandard'   # new dependencies present
   ```

   Build the `deploy/gw2` bundle (§3).
2. **Transfer and load** through OSS `_deploy-tmp/`, `docker load`, keep previous tags; unpack the bundle.
3. **Back up** into `/opt/openbox/backups/<TAG>/activation-<UTC stamp>/` (0700): `.env`, `config/backend.env`,
   both compose files, `docker inspect` of the running containers, and
   `docker compose exec -T postgres pg_dump -U openbox -Fc openbox > preflight.dump` verified with
   `docker compose exec -T postgres pg_restore --list < preflight.dump > /dev/null`.
4. **Trace role, database and postgres tuning** (maintenance window, 0 active runs, preflight 4 done):

   ```bash
   cd /opt/openbox
   cp -p .env ".env.bak-$(date +%Y%m%d%H%M%S)"
   grep -c '^OPENBOX_TRACE_DB_PASSWORD=' .env          # 1
   # Append COMPOSE_FILE once, on a line of its own even when .env does not end with a newline.
   if ! grep -q '^COMPOSE_FILE=' .env; then
     [ -z "$(tail -c 1 .env)" ] || echo >> .env
     echo 'COMPOSE_FILE=docker-compose.yml:deploy/gw2/docker-compose.trajectory.yml:docker-compose.override.yml' >> .env
   fi
   grep '^COMPOSE_FILE=' .env                          # exactly the value above, once
   docker compose config --services | grep -x trajectory-worker
   docker compose up -d --no-deps postgres            # recreates postgres with the tuning
   docker compose ps postgres                          # wait for healthy
   deploy/gw2/scripts/create-trace-db.sh --dry-run     # prints the statements, the password masked
   deploy/gw2/scripts/create-trace-db.sh               # role openbox_trace, openbox_trace, pg_trgm, pg_stat_statements
   docker compose restart backend                      # fresh connection pool; restart keeps its configuration
   ```

   The script sends the role statements to psql on stdin (the password is in no process's arguments) in a session that
   neither logs statements nor records them in `pg_stat_statements`, and refuses to run without the password.
   Verify: `SHOW shared_preload_libraries` lists `pg_stat_statements`;
   `docker compose exec -T postgres psql -U openbox -d postgres -tAc "SELECT rolconfig FROM pg_roles WHERE rolname = 'openbox_trace'"`
   shows `statement_timeout=5s` and `work_mem=32MB`;
   `... -tAc "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'openbox_trace'"` is `openbox_trace`;
   backend healthy; public probes 200.
   Rollback: restore the `.env` backup, `docker compose up -d --no-deps postgres`, `docker compose restart backend`;
   `openbox_trace` and its role can stay.
5. **Worker**: add the `trajectory-worker` pin (§3) to the override, then

   ```bash
   docker compose up -d --no-deps trajectory-worker
   docker compose ps trajectory-worker                                      # healthy
   docker compose exec -T trajectory-worker curl -fsS http://127.0.0.1:8090/health
   docker compose exec -T trajectory-worker alembic -c alembic_trajectory.ini current
   docker compose logs --since 5m trajectory-worker
   docker compose exec -T postgres psql -U openbox -d postgres -tAc \
     "SELECT usename, datname, count(*) FROM pg_stat_activity WHERE usename = 'openbox_trace' GROUP BY 1, 2"   # only openbox_trace
   ```

   `/health` must show `"writer": true, "db": true, "spool": true, "blob_store": true`. The worker only reads the
   spool; nothing records to it yet. Rollback: `docker compose stop trajectory-worker` (no business impact).
6. **Backend on the spool**: back up `config/backend.env`, set `TRAJECTORY_RECORDING_ENABLED=false` (recording
   restarts in step 9), pin the new backend tag, check 0 active runs, then `docker compose up -d --no-deps backend` and
   wait for healthy. The business migration drops the seven old trajectory tables; old recordings are not kept.
   Verify, then reset the query statistics right away:

   ```bash
   docker compose exec -T postgres psql -U openbox -d openbox -c '\dt trajectory_*'          # Did not find any relation
   docker compose exec -T postgres psql -U openbox -d openbox -c '\dt session_trajectories'          # Did not find any relation
   docker compose exec -T postgres psql -U openbox -d openbox -c '\dt legacy_trajectory_*'   # Did not find any relation
   docker compose exec -T backend alembic current                                            # e5c7a9b1d3f4 (head)
   docker compose exec -T postgres psql -U openbox -d openbox -c 'SELECT pg_stat_statements_reset()'
   ```

   The migration's `DROP TABLE … trajectory_*` statements match the `business_trajectory_statements` isolation filter
   of `scripts/push-metrics.sh` (§8); the reset keeps them out of that metric. Rollback: §6.
7. **Old trajectory payload files**: the business blob store (volume `blob-data`, mounted at `/tmp/openbox-blobs` in the
   backend) still holds the payload files of the old recordings in its `trajectories/` directory, about 1.5 GB of
   unredacted content that nothing reads any more. List the sizes, then remove only that directory; `policies/` stays:

   ```bash
   docker compose exec -T backend sh -c 'du -sh /tmp/openbox-blobs/*'     # trajectories about 1.5G, policies
   docker compose exec -T backend rm -rf /tmp/openbox-blobs/trajectories
   docker compose exec -T backend ls /tmp/openbox-blobs                    # policies remains
   ```

   The deletion cannot be undone; neither `pg-backup.sh` nor the step 3 backup contains these files.
8. **Frontend**: pin the new frontend tag, `docker compose up -d --no-deps frontend`, wait for healthy. Check routing:
   `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/api/admin/trajectories/sessions` answers 401 and
   `docker compose logs --since 1m trajectory-worker | grep admin/trajectories` shows the request. The access log lines
   now end with `rt=<seconds> urt=<seconds>`, which step 9 measures.
9. **Recording**, internal users first, compared with an equal window without recording. Pick two windows of the same
   length (at least 60 minutes) with comparable traffic, for example the same hours on two days; the first runs
   now, with recording still off. Export each window's access log right after it ends (the frontend log rotates at 60 MB).

   ```bash
   cd /opt/openbox
   compare=/opt/openbox/backups/$TAG/compare && install -d -m 0700 "$compare"
   db_size() { docker compose exec -T postgres psql -U openbox -d openbox -tAc "SELECT pg_database_size('openbox')"; }
   # measure NAME MINUTES: database size before and after, backend docker stats every 30 s, the frontend access log.
   measure() {
     local start end
     db_size > "$compare/db-size-$1-start"
     start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
     end=$(($(date +%s) + $2 * 60))
     while [ "$(date +%s)" -lt "$end" ]; do
       docker stats --no-stream --format 'cpu={{.CPUPerc}} mem={{.MemPerc}}' "$(docker compose ps -q backend)" >> "$compare/stats-$1.txt"
       sleep 30
     done
     docker compose logs --no-log-prefix --since "$start" --until "$(date -u +%Y-%m-%dT%H:%M:%SZ)" frontend > "$compare/access-$1.log"
     db_size > "$compare/db-size-$1-end"
   }
   measure off 60
   ```

   Enable recording for internal users. Backend and worker share the flags, so recreate both (0 active runs for the backend):

   ```bash
   docker compose exec -T postgres psql -U openbox -d openbox -c 'SELECT pg_stat_statements_reset()'
   # backend.env: TRAJECTORY_RECORDING_ENABLED=true, TRAJECTORY_RECORD_USER_IDS=<internal user ids>
   docker compose up -d --no-deps trajectory-worker && docker compose up -d --no-deps backend
   measure on 60                                        # use the product with an internal account meanwhile
   ```

   Compare and check isolation and flow:

   ```bash
   { echo '#window off'; cat "$compare/access-off.log" "$compare/stats-off.txt"
     echo '#window on'; cat "$compare/access-on.log" "$compare/stats-on.txt"; } |
     docker compose exec -T trajectory-worker python -m trajectory.ops.latency     # exit 0
   for window in off on; do
     echo "$window: openbox grew by $(( $(cat "$compare/db-size-$window-end") - $(cat "$compare/db-size-$window-start") )) bytes"
   done
   docker compose exec -T postgres psql -U openbox -d openbox -tAc \
     "SELECT count(*) FROM pg_stat_statements s JOIN pg_database d ON d.oid = s.dbid
      WHERE d.datname = 'openbox' AND s.query ILIKE '%trajectory\_%'"   # 0
   docker compose exec -T trajectory-worker curl -fsS http://127.0.0.1:8090/metrics
   ```

   Pass criteria:
   - `trajectory.ops.latency` exits 0. Per API route (method and path, ids collapsed) it prints request counts and the
     p95 of `rt=` for both windows. It flags a route with at least 50 requests in both windows whose p95 grew by more
     than 20 % and more than 50 ms, and a mean backend CPU (percent of one core) more than 10 points higher.
   - The growth of `pg_database_size('openbox')` in the recording window stays comparable to the window without recording
     (the legacy recorder added hundreds of MB per hour at this traffic).
   - The isolation query answers 0, the worker metrics are normal, and the admin UI shows the new session live.

   Then clear `TRAJECTORY_RECORD_USER_IDS` and recreate worker and backend again. Record `db_size` once more 24 hours later
   and compare the daily growth with the days before the release.
10. **Operations**: install the timers (§7), apply the lifecycle rules (§9) and alarms (§8) from the operator machine,
    run the drills and the restore check (§10, §11), and append the release record to docs/DEPLOY.md.

## 6. Rollback

| Situation | Steps |
|---|---|
| Worker misbehaves (any step) | `docker compose stop trajectory-worker`. Business requests keep working; the spool grows up to its budget (2 GiB), after which events are dropped as `spool_full` gaps. Fix and start it again. |
| Backend switched (step 6 or later) | The backend rollback below. |
| Frontend | Pin the previous frontend tag, `docker compose up -d --no-deps frontend`. |
| Trace role and postgres tuning | Remove `COMPOSE_FILE` from `.env` only together with the backend rollback, then `docker compose up -d --no-deps postgres` in a maintenance window. `OPENBOX_TRACE_DB_PASSWORD`, the role and `openbox_trace` may stay; to remove them, stop the worker, back up `openbox_trace`, then `DROP DATABASE openbox_trace` and `DROP ROLE openbox_trace`. |
| Whole topology | Order: frontend → backend → worker stop → `install-timers.sh --uninstall` → `.env` without `COMPOSE_FILE` → postgres. |

Backend rollback, with 0 active runs (preflight 8):

1. With the **new** backend image still pinned, downgrade the business chain:
   `docker compose run --rm --no-deps backend alembic downgrade c7e9b1d3f5a7`. It recreates the seven old trajectory
   tables, empty: the previous image's readiness check and session deletion expect them.
2. Keep `TRAJECTORY_RECORDING_ENABLED` and `TRAJECTORY_ADMIN_ENABLED` false in `config/backend.env` (set them before the
   next step if step 9 enabled recording).
3. Pin the previous backend and frontend tags in the override and recreate both with `--no-deps`, one at a time,
   waiting for healthy.
4. `docker compose stop trajectory-worker`, then take the overlay out of `COMPOSE_FILE` in `.env` (§3).

Old recordings are gone in both directions: the recreated tables stay empty, and the previous admin UI cannot show what
was recorded into `openbox_trace`.

## 7. Timers

```bash
/opt/openbox/deploy/gw2/scripts/install-timers.sh                  # gw2: installs /etc/systemd/system/openbox-*.{service,timer}
/opt/openbox/deploy/gw2/scripts/install-timers.sh --instance aws   # any other host, such as the AWS development host
systemctl list-timers 'openbox-*'
journalctl -u openbox-trajectory-metrics.service -n 20
systemctl start openbox-pg-backup.service                          # run a backup now
/opt/openbox/deploy/gw2/scripts/install-timers.sh --uninstall
```

A host installed with the default instance reports its metrics as `instance=gw2` and raises the production alarms, so
every host other than gw2 needs its own `--instance`.

| Timer | Schedule | Script | State |
|---|---|---|---|
| `openbox-trajectory-metrics.timer` | every minute | `push-metrics.sh` | `/var/lib/openbox-ops/cms-state.json` (counter samples); instance in `/etc/systemd/system/openbox-trajectory-metrics.service.d/instance.conf` |
| `openbox-pg-backup.timer` | daily 03:30 Asia/Shanghai, catches up after downtime | `pg-backup.sh` | `/var/backups/openbox/postgres` (dumps of failed uploads and `--keep-local` runs; removed after 3 days) |
| `openbox-prune-images.timer` | Sunday 04:30 Asia/Shanghai | `prune-images.sh --execute` | — (removes nothing when the compose files cannot be read) |

Overlapping runs are skipped through `/run/lock/openbox-<name>.lock`. Re-run `install-timers.sh` after a bundle update.

## 8. Metrics and alarms

`push-metrics.sh` pipes host measurements into `python -m trajectory.ops.cms push` in the worker container (a
one-off container while the worker is stopped), which adds the worker's `/health` and `/metrics` and reports with
CloudMonitor `PutCustomMetric` (API 2019-01-01, endpoint `metrics.cn-shanghai.aliyuncs.com`, 21 entries per call),
group `TRAJECTORY_CMS_GROUP_ID` (default `0`) and dimension `instance=<instance>` (§7; `gw2` in production).

| Metric | Source |
|---|---|
| `host_disk_used_percent`, `docker_disk_used_percent` | `df` of `/` and the Docker root |
| `spool_bytes`, `spool_files`, `spool_oldest_age_seconds`, `spool_quarantined_files` | spool volume on the host: `spool_bytes` counts every file under `producers/`, `blobs/` and `quarantine/`; files and age count non-empty `*.jsonl` and `*.jsonl.part` |
| `oom_kills_1h`, `backend_oom_kills_1h` | `journalctl -k` `oom-kill:` lines of the last hour (backend: its container id in the memory cgroup) |
| `trace_db_bytes` | `pg_database_size('openbox_trace')` |
| `backend_cpu_percent`, `backend_mem_percent` | `docker stats --no-stream` of the backend container: CPU in percent of one core (the single uvicorn process saturates near 100), memory in percent of its limit (3 GiB) |
| `business_trajectory_statements` | `pg_stat_statements` entries of the database `openbox` whose query mentions `trajectory_`, cumulative since the last `pg_stat_statements_reset()` (release steps 6 and 9 reset it); 0 while the extension is not installed in `openbox` |
| `worker_up`, `worker_writer`, `worker_degraded`, `worker_db_ok`, `worker_spool_ok`, `worker_blob_store_ok` | worker `/health` |
| `ingest_lag_seconds`, `projection_lag_events`, `archive_lag_events`, `gc_queue_depth`, `hot_events_rows`, `trajectories_degraded`, `trajectories_blocked`, `stale_hot_partitions`, `budget_degraded_trajectories`, `budget_degraded_users` | worker `/metrics` gauges |
| `hot_partitions` | worker `/metrics` gauge: attached daily `trajectory_events_p*` partitions dated today or earlier (UTC); pre-created future partitions and the default partition are not counted. A healthy worker holds about 8. |
| `events_ingested_24h` | worker `/metrics` gauge: events ingested in the last 24 hours, sampled every 5 minutes |
| `<counter>_delta` for `ingest_lines`, `ingest_events`, `duplicates`, `idempotency_conflicts`, `deleted_drops`, `ownership_drops`, `gaps_recorded`, `producer_loss_events`, `quarantined_files`, `blob_puts`, `blob_put_bytes`, `blob_put_failures`, `segment_uploads`, `segment_failures`, `gc_deleted`, `gc_failures`, `exports_built`, `blob_put_raw_bytes`, `audit_dead_letters`, `failed_batches` | worker counters, increase since the previous minute (restarts handled) |
| `gaps_recorded_1h`, `blob_put_failures_5m` | trailing sums of the counters above |

A name the worker does not serve is not reported.

Alarm rules (operator machine; IDs `openbox-<instance>-<name>`, re-running updates them). Give the rules the metrics'
own dimension and group: `--instance` as installed on the host (default `gw2`) and `--group-id` equal to
`TRAJECTORY_CMS_GROUP_ID` in `config/backend.env` (0 when unset); a rule on another group or instance never sees the
metrics. Every rule uses the `Average` statistic, the only value the PutCustomMetricRule reference documents; with one
sample per minute it is the reported value.

```bash
deploy/gw2/scripts/setup-alarms.sh                                         # dry run: prints the fifteen PutCustomMetricRule calls
deploy/gw2/scripts/setup-alarms.sh --execute [--group-id <id>] [--webhook URL]
```

| Rule | Condition | First response |
|---|---|---|
| `host-disk` | `host_disk_used_percent` ≥ 80, 3 min | `docker system df`; `prune-images.sh --execute`; check `/var/backups/openbox/postgres` and log sizes. |
| `spool-bytes` | `spool_bytes` ≥ 1 GiB, 2 min | Worker down or lagging: §13. At 2 GiB the backend drops events as gaps. |
| `spool-age` | `spool_oldest_age_seconds` ≥ 60, 3 min | Ingest stalled: worker health, logs, `ingest_lag_seconds`. |
| `worker-down` | `worker_up` < 1, 3 min | `docker compose ps trajectory-worker`, logs, restart; business is unaffected. |
| `recording-gaps` | `gaps_recorded_1h` > 0 | Find the reason in worker logs (`queue_overflow`, `spool_full`, `producer_lines_lost`, blob outage). |
| `projection-lag` | `projection_lag_events` ≥ 5000, 5 min | Worker CPU (limit 1), trace DB latency, large trajectories. |
| `blob-put-failures` | `blob_put_failures_5m` ≥ 10 | OSS status, credentials, internal endpoint reachability. |
| `trace-db-size` | `trace_db_bytes` ≥ 20 GiB | `archive_lag_events`, `stale_hot_partitions`, retention settings. |
| `oom-kill` | `oom_kills_1h` > 0 | `journalctl -k \| grep oom-kill`; which container; memory limits. |
| `archive-lag` | `archive_lag_events` ≥ 50000, 15 min | Archiving stalled: worker log (`segment_failures_delta`), OSS reachability, `hot_events_rows`, `trace_db_bytes`. |
| `hot-partitions` | `hot_partitions` > 10, 5 min | More past-day partitions are attached than a healthy worker keeps (about 8), so old partitions are not dropped: `stale_hot_partitions`, `archive_lag_events`, partition maintenance warnings in the worker log. |
| `backend-cpu` | `backend_cpu_percent` > 90, 5 min | The backend process is saturated: busiest routes of the frontend access log (`rt=`), polling clients, `docker stats`. |
| `backend-memory` | `backend_mem_percent` > 90, 5 min | An OOM kill at the 3 GiB limit is next: `docker stats`, large responses, history loads; restart only with 0 active runs. |
| `business-trajectory-statements` | `business_trajectory_statements` > 0 | Trajectory SQL reached the business database. List it with `SELECT calls, left(query, 200) FROM pg_stat_statements s JOIN pg_database d ON d.oid = s.dbid WHERE d.datname = 'openbox' AND s.query ILIKE '%trajectory\_%' ORDER BY calls DESC`, fix the producer, then `SELECT pg_stat_statements_reset()` clears the count. |
| `events-ingested-24h` | `events_ingested_24h` > 1000000, 5 min, level INFO | Not an incident: more than a million events in 24 hours is above the planned load. Check `trace_db_bytes`, `archive_lag_events` and the budget gauges, and review capacity. |

Testing a rule: `oom-kill` alarms when the average of one minute is above 0, so
`docker compose exec -T trajectory-worker python -m trajectory.ops.cms put --metric oom_kills_1h=1` raises it once
(the next timer run reports the real value 0 again). Metrics that stop arriving do not
alarm: check `systemctl list-timers 'openbox-*'` during the weekly review, or add a no-data alert in the CloudMonitor console.

## 9. OSS lifecycle rules

`oss-lifecycle.xml` (IDs start with `openbox-`):

| Rule | Prefix | Action |
|---|---|---|
| `openbox-trajectories-ia-30d` | `trajectories/` except `trajectories/_exports/` | Transition to IA after 30 days. The rule uses the last modified time, so objects of every size move; IA bills an object under 64 KB as 64 KB. |
| `openbox-trajectory-exports-expire-30d` | `trajectories/_exports/` | Expire after 30 days |
| `openbox-trajectories-abort-multipart-7d` | `trajectories/` | Abort incomplete multipart uploads after 7 days |
| `openbox-postgres-backups-expire-30d` | `backups/postgres/` | Expire after 30 days; abort incomplete multipart uploads after 7 days |

The bucket is shared with user assets and PutBucketLifecycle replaces all rules, so the script reads the current
rules (a bucket without rules answers `NoSuchLifecycle`), merges ours by ID with
`backend/trajectory/ops/lifecycle.py` and refuses what OSS rejects (overlapping part policies, a part policy with a
Not filter, same-action overlaps unless `--allow-same-action-overlap`):

```bash
deploy/gw2/scripts/apply-oss-lifecycle.sh --bucket <bucket>                               # dry run, prints the merged XML
deploy/gw2/scripts/apply-oss-lifecycle.sh --bucket <bucket> --backup-dir <dir> --execute  # saves, applies, reads back, verifies
```

Rollback: the previous rules are in `<dir>/oss-lifecycle-<bucket>-<stamp>.xml`. Non-empty file:
`aliyun ossutil api put-bucket-lifecycle --bucket <bucket> --lifecycle-configuration file://<file> --region cn-shanghai`;
empty file (the bucket had no rules): `aliyun ossutil api delete-bucket-lifecycle --bucket <bucket> --region cn-shanghai`.
OSS loads new rules within 24 hours and runs them daily at 08:00 (UTC+8).

## 10. Backups and restore

`pg-backup.sh` dumps each database with `pg_dump -Fc` inside the postgres container, checks the dump with
`pg_restore --list`, and streams it into `python -m trajectory.ops.backup upload` in the worker container: a presigned
PUT (internal endpoint, signed with `secrets/aliyun-config.json`) with exact `Content-Length` and
`x-oss-meta-sha256`, then a signed HEAD that checks size and digest. Objects:
`backups/postgres/<YYYYMMDD Asia/Shanghai>/<database>-<UTC stamp>.dump`, expired by the lifecycle rule after 30 days.
A database that does not exist yet is skipped. Old recordings are not kept, so the `openbox` dump passes
`--exclude-table-data` for the old trajectory tables: their schema stays, their rows are not dumped, and after release
step 6 the patterns match nothing (the exact patterns are in the script). The run fails (systemd marks the unit failed)
when PostgreSQL cannot be queried, a dump or upload fails, or nothing was backed up; the dump of a failed upload stays in
`/var/backups/openbox/postgres` for 3 days. One presigned PUT stores at most 5 GiB, so `trajectory.ops.backup` refuses
a larger dump before sending it: add multipart uploads before a dump approaches that size (the `openbox` dump was
162 MB on 2026-09-14).

```bash
deploy/gw2/scripts/pg-backup.sh                                  # openbox and openbox_trace
deploy/gw2/scripts/pg-backup.sh --database openbox --keep-local  # one database, keep the local file
docker compose exec -T trajectory-worker python -m trajectory.ops.backup verify --key <key> --size <bytes> --sha256 <hex>
```

Restore check (after the release, then quarterly): `restore-check.sh` downloads a backup through
`python -m trajectory.ops.backup download` in the worker image (size and recorded sha256 checked) into
`/var/backups/openbox/restore-check` (0700), restores it with `pg_restore --no-owner --no-privileges --exit-on-error` into
a new database `openbox_restore_check_<UTC stamp>`, compares its tables with the dump's table of contents, and drops the
database and the downloaded file, also when a step fails. It never restores into or drops a live database. The scratch
database needs free space about the size of the restored database (the postgres volume held 777 MB on 2026-09-14).

```bash
journalctl -u openbox-pg-backup.service --since -2d | grep uploading                      # keys of recent backups
deploy/gw2/scripts/restore-check.sh --key backups/postgres/<YYYYMMDD>/openbox-<stamp>.dump            # dry run
deploy/gw2/scripts/restore-check.sh --key backups/postgres/<YYYYMMDD>/openbox-<stamp>.dump --execute
deploy/gw2/scripts/restore-check.sh --file /var/backups/openbox/postgres/<name>.dump --execute        # a local dump
```

A real restore stops the writers first (`backend`, `trajectory-worker`), restores into a new database, swaps names,
and starts the services one at a time.

## 11. Drills

Run after release step 10 and then quarterly, in a quiet period, with an internal test account producing traffic.
Every drill is a dry run without `--execute`, restores the service when interrupted (Ctrl-C), and exits non-zero when a
pass criterion fails or cannot be verified (worker metrics unreadable, or no test traffic during the window).

| Drill | Command | Proves | Pass criteria | Impact |
|---|---|---|---|---|
| Worker stop | `drill-worker-stop.sh --execute` (15 min) | Business is independent of the worker; the spool buffers | All backend probes pass; the spool drains within 5 min of the restart (`--drain-timeout 300`); `producer_loss_events` 0 | Admin trajectory UI unavailable for 15 min |
| Blob outage | `drill-blob-outage.sh --execute` (30 min) | Ingest survives OSS failures with backoff | All backend probes pass; `blob_put_failures` rises; spool drains after the fault | Content recorded in the window may become `blob_store_unavailable` gaps |
| Spool full | `drill-spool-full.sh --minutes 3 --execute` | The spool budget drops events instead of blocking | All backend probes pass; `gaps_recorded` increases after the restore | Two backend recreations (each waits for 0 active runs) |
| Session deletion | `drill-delete-session.sh --session-id <id> --user-token-file <file> --admin-token-file <file> --execute` | Deleting a session removes its trajectory content | The admin API answers 404 or 410 for the session; `trajectory.ops.deletion verify` finds the trajectory tombstoned, no pending garbage collection and no object under `trajectories/<trajectory id>/`, within `--timeout` (900 s) | The internal test session is deleted for good |
| Restore check | `restore-check.sh --key <backup key> --execute` | A backup in OSS restores | Size and sha256 match; `pg_restore` succeeds; every table of the dump exists | A scratch database `openbox_restore_check_*` exists during the run |
| Rebuild | `rebuild-trace-db.sh --limit 50 --execute` | Archived segments in OSS restore the event history | Exit 0: segment sha256, event counts, sequence ranges, event keys and stream digests match | Read-only on `openbox_trace`; a scratch database `openbox_trace_rebuild_*` exists during the run |

The session deletion drill needs a root session of the internal test account that has been idle for more than
5 minutes (so its events are archived to OSS; the drill refuses a session without stored objects and deletes nothing),
an access token of that account and one of an admin account. Write each token into a file of mode 0600 without putting
it on a command line (`install -m 0600 /dev/null <file>`, then paste it into `cat > <file>`), pass `--workspace-id`
when the session is not in the account's default workspace, and delete the files afterwards. The script hands the
tokens to curl on stdin, deletes through `DELETE http://127.0.0.1:8080/api/agent/session/<id>` and asks the admin API
through the frontend at `http://127.0.0.1`.

Record the output of each drill in the release log.

## 12. Retention

| Data | Retention | Enforced by |
|---|---|---|
| Trajectory content (events, blobs, segments) | 180 days after the last activity (`TRAJECTORY_CONTENT_RETENTION_DAYS`); the summary row stays | worker retention |
| Hot events in PostgreSQL | archived to OSS segments; partitions dropped after 7 days (`TRAJECTORY_HOT_DAYS`) | worker archive |
| Idempotency keys | 30 days (`TRAJECTORY_DEDUPE_DAYS`) | worker archive |
| Exports | 30 days (`TRAJECTORY_EXPORT_RETENTION_DAYS`) | worker retention, lifecycle rule as backstop |
| Trajectory objects in OSS | IA after 30 days | lifecycle rule |
| Deleted sessions and assets | removed when the deletion reaches the worker (GC queue with retries) | worker retention |
| PostgreSQL backups | 30 days in OSS; local dumps of failed uploads 3 days | lifecycle rule, `pg-backup.sh` |
| Docker images | in use, pinned, and the newest 3 tags per repository | `prune-images.sh` weekly |
| Spool | consumed files deleted after ingest; budget 2 GiB (`TRAJECTORY_SPOOL_MAX_BYTES`) | worker, emitter |
| Worker logs | 5 × 50 MB | compose logging options |

## 13. Troubleshooting

- **Worker restarts or stays unhealthy**: `docker compose logs --tail 200 trajectory-worker`. Typical causes: trace
  migrations (`TRAJECTORY_DATABASE_URL`, `openbox_trace` missing, `pg_trgm` privileges), OSS credentials, memory limit.
- **`password authentication failed for user "openbox_trace"`**: `OPENBOX_TRACE_DB_PASSWORD` in `.env` differs from the
  role's password (changed or missing). Run `create-trace-db.sh` again, which sets the role's password from `.env`, then
  `docker compose up -d --no-deps trajectory-worker`.
- **`canceling statement due to statement timeout` in the worker log**: the role default is 5 s
  (`SELECT rolconfig FROM pg_roles WHERE rolname = 'openbox_trace'`); an operation that legitimately takes longer must set
  `SET LOCAL statement_timeout` itself. Report the statement instead of raising the role default.
- **`"writer": false`**: another process holds the writer lock (a stray `docker compose run` of the worker service).
  Find it: `SELECT pid, application_name, backend_start FROM pg_stat_activity WHERE datname = 'openbox_trace'` and
  `SELECT pid FROM pg_locks WHERE locktype = 'advisory'`; stop the extra container.
- **Spool keeps growing**: worker stopped or lagging (`ingest_lag_seconds`), disk full, or quarantined files blocking a
  producer. Business requests are not affected; at the budget the backend records gaps (`spool_full`), and while the
  spool's file system has less than `TRAJECTORY_SPOOL_MIN_FREE_BYTES` (1 GiB) free it drops events as `disk_full`
  gaps even below the budget.
- **Files in `quarantine/`**: read the `.reason` sidecar and the worker log. The files may contain unredacted content;
  keep them only as long as the analysis needs, then delete them.
- **Admin trajectory pages fail (502/404)**: `docker compose exec frontend env | grep TRAJECTORY_HOST`, worker health,
  `INTERNAL_API_TOKEN` on both sides (401 on every admin request).
- **Blob upload failures**: OSS status page, `secrets/aliyun-config.json`, reachability of
  `<bucket>.oss-cn-shanghai-internal.aliyuncs.com` from the worker container.
- **OOM kills**: `journalctl -k --since -1h | grep oom-kill` names the task and memory cgroup; compare with
  `docker stats --no-stream`.
- **`COMPOSE_FILE` missing**: `docker compose config --services` lacks `trajectory-worker`; restore the `.env` line (§3)
  before any `docker compose up`.
