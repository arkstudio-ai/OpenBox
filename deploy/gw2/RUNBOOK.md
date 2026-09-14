# gw2 runbook: trajectory worker topology

> Status: prepared with the trajectory re-architecture (SPEC §12, work package w1-ops); **not released yet**.
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
trajectory-worker ──► postgres/openbox_trace · OSS <bucket>/trajectories/ · redis ·
                      backend /api/internal/trajectory/{viewer,audit}
host systemd timers ──► CloudMonitor custom metrics · OSS backups/postgres/ · docker image prune
```

What `docker-compose.trajectory.yml` changes:

| Service | Change | Why |
|---|---|---|
| `trajectory-worker` (new) | backend image, command `alembic -c alembic_trajectory.ini upgrade head && exec python -m trajectory.worker`, `env_file: config/backend.env`, `TRAJECTORY_DATABASE_URL=…/openbox_trace`, `TRAJECTORY_WORKER_MODE=external`, `TRAJECTORY_BLOB_PROVIDER=oss`, `TRAJECTORY_BACKEND_INTERNAL_URL=http://backend:8080`, `ALIYUN_CLI_CONFIG=/run/secrets/aliyun-config.json`; spool volume, aliyun secret (ro), `blob-data:/legacy-blobs:ro`; healthcheck on :8090; 1 CPU, 1 GiB; log rotation | Ingest, projection, archive, admin API. The command override is mandatory: the image CMD would run the business migrations and a second uvicorn. One writer only: never `--scale`, never name it `backend-worker` (`make retire-legacy-worker` deletes that service). |
| `backend` | `TRAJECTORY_SINK=spool`, `TRAJECTORY_WORKER_MODE=external`, spool volume | Business requests only append to the spool. |
| `frontend` | `TRAJECTORY_HOST=trajectory-worker:8090` | nginx routes the two admin trajectory paths to the worker (the image default still points at the backend). |
| `postgres` | `mem_limit: 2g`; `shared_buffers=512MB`, `effective_cache_size=1GB`, `shared_preload_libraries=pg_stat_statements`, `pg_stat_statements.track=all`, `max_connections=200` | Room for the trace database and its pool; query statistics for the isolation check. Applying it recreates postgres. |
| volumes | `trajectory-spool` | The spool may contain unredacted data; it is mounted into backend and worker only. |

## 2. Files

| File | Where it runs | Purpose |
|---|---|---|
| `docker-compose.trajectory.yml` | gw2 | The overlay above. |
| `docker-compose.base.example.yml`, `docker-compose.override.example.yml`, `env.example` | developer machine | Sanitized reconstruction of the server's compose files; `backend/tests/unit/test_trajectory_ops_deploy_assets.py` validates the overlay against them with `docker compose config`. Compare them with the real server files before a release. |
| `oss-lifecycle.xml` | operator machine | OpenBox-managed lifecycle rules (§9). |
| `scripts/lib.sh` | both | Shared helpers (sourced). |
| `scripts/create-trace-db.sh` | gw2 | `openbox_trace`, `pg_trgm`, `pg_stat_statements` (idempotent). |
| `scripts/push-metrics.sh` | gw2, minute timer | Host metrics + worker metrics → CloudMonitor (§8). |
| `scripts/pg-backup.sh` | gw2, daily timer | `pg_dump -Fc` of `openbox` and `openbox_trace` → OSS (§10). |
| `scripts/prune-images.sh` | gw2, weekly timer | Image cleanup, dry run unless `--execute`. |
| `scripts/install-timers.sh` | gw2 | Installs the systemd units in `systemd/`. |
| `scripts/apply-oss-lifecycle.sh` | operator machine | Merges `oss-lifecycle.xml` into the bucket's rules, dry run unless `--execute`. |
| `scripts/setup-alarms.sh` | operator machine | CloudMonitor alarm rules, dry run unless `--execute`. |
| `scripts/drill-*.sh`, `scripts/rebuild-trace-db.sh` | gw2 | Drills (§11), dry run unless `--execute`. |
| `backend/trajectory/ops/{cms,backup,lifecycle,rebuild}.py` | worker image / operator | Python side of the scripts; `python -m trajectory.ops.<module> --help`. |

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

Compose file order, set once in `/opt/openbox/.env` (release step 4):

```bash
COMPOSE_FILE=docker-compose.yml:deploy/gw2/docker-compose.trajectory.yml:docker-compose.override.yml
```

- Every plain `docker compose …` in `/opt/openbox` then includes the overlay. Without it, a routine
  `docker compose up -d --no-deps backend` would recreate the backend without the spool volume.
- The override stays last so its image pins win. Pin the worker exactly like the backend:

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

`blob-data:/legacy-blobs:ro` expects the base file to declare the `blob-data` volume of the legacy local
blob store. If `docker compose config` reports an undefined volume, find the legacy blob mount in
`docker inspect <backend container>` and declare that volume in the override (as `external: true` with its
real name) before continuing.

## 4. Preflight checklist

1. The release commit is on `origin/main` and the override's current image commits are contained in it (lesson of 2026-09-10).
2. Both migration chains have one head, checked in the new image:
   `docker run --rm --entrypoint alembic openbox-backend:<TAG> heads` and
   `docker run --rm --entrypoint alembic openbox-backend:<TAG> -c alembic_trajectory.ini heads`.
3. `config/backend.env` has non-empty `JWT_SECRET` and `INTERNAL_API_TOKEN` (the worker authenticates
   viewers through the backend's internal endpoints), `OSS_BUCKET` and `OSS_REGION` (or `TRAJECTORY_OSS_*`),
   and the shared recording flags (`TRAJECTORY_RECORDING_ENABLED`, `TRAJECTORY_RECORD_USER_IDS`,
   `TRAJECTORY_ADMIN_ENABLED`, `TRAJECTORY_ADMIN_USER_IDS`).
4. The RAM user in `secrets/aliyun-config.json` may PUT/GET/HEAD/DELETE/LIST under `trajectories/` and
   `backups/postgres/` of the bucket and call `cms:PutCustomMetric`. The operator's own profile needs
   `oss:GetBucketLifecycle`, `oss:PutBucketLifecycle` and `cms:PutCustomMetricRule`.
5. Disk: `df -h / "$(docker info --format '{{.DockerRootDir}}')"` shows at least 20 % free (the spool budget is 2 GiB).
6. PostgreSQL: `docker compose exec postgres postgres --version` (13+ for trusted `pg_trgm`),
   `docker compose exec postgres psql -U openbox -tAc 'SHOW max_connections'`.
7. Active runs (repeat right before every backend or postgres recreation; must be 0):

   ```bash
   docker compose exec -T postgres psql -U openbox -d openbox -tAc \
     "SELECT count(*) FROM session_executions WHERE run_id IS NOT NULL AND lease_until > now()"
   ```

8. The overlay validates on a developer machine: `cd backend && uv run pytest tests/unit/test_trajectory_ops_deploy_assets.py -q`.

## 5. Release procedure (SPEC §12.1)

Keep a terminal probing the public site during every switch, as in previous releases.

1. **Build** `linux/amd64` images from the release commit (`docker build --platform linux/amd64 -f backend/Dockerfile .`,
   `docker build --platform linux/amd64 --build-arg NGINX_IMAGE=<pinned nginx> frontend-v2/`) and the `deploy/gw2` bundle (§3).
2. **Transfer and load** through OSS `_deploy-tmp/`, `docker load`, keep previous tags; unpack the bundle.
3. **Back up** into `/opt/openbox/backups/<TAG>/activation-<UTC stamp>/` (0700): `.env`, `config/backend.env`,
   both compose files, `docker inspect` of the running containers, and
   `docker compose exec -T postgres pg_dump -U openbox -Fc openbox > preflight.dump` verified with
   `docker compose exec -T postgres pg_restore --list < preflight.dump > /dev/null`.
4. **Trace database and postgres tuning** (maintenance window, 0 active runs):

   ```bash
   cd /opt/openbox
   cp .env .env.bak-$(date +%Y%m%d%H%M%S)
   echo 'COMPOSE_FILE=docker-compose.yml:deploy/gw2/docker-compose.trajectory.yml:docker-compose.override.yml' >> .env
   docker compose config --services | grep -x trajectory-worker
   docker compose up -d --no-deps postgres            # recreates postgres with the tuning
   docker compose ps postgres                          # wait for healthy
   deploy/gw2/scripts/create-trace-db.sh               # openbox_trace, pg_trgm, pg_stat_statements
   docker compose restart backend                      # fresh connection pool; restart keeps its configuration
   ```

   Verify: `SHOW shared_preload_libraries` lists `pg_stat_statements`; backend healthy; public probes 200.
   Rollback: restore the `.env` backup, `docker compose up -d --no-deps postgres`, `docker compose restart backend`;
   `openbox_trace` can stay.
5. **Worker**: add the `trajectory-worker` pin (§3) to the override, then

   ```bash
   docker compose up -d --no-deps trajectory-worker
   docker compose ps trajectory-worker                                      # healthy
   docker compose exec -T trajectory-worker curl -fsS http://127.0.0.1:8090/health
   docker compose exec -T trajectory-worker alembic -c alembic_trajectory.ini current
   docker compose logs --since 5m trajectory-worker
   ```

   `/health` must show `"writer": true, "db": true, "spool": true, "blob_store": true`. The worker only reads the
   spool; nothing records to it yet. Rollback: `docker compose stop trajectory-worker` (no business impact).
6. **Backend on the spool**: back up `config/backend.env`, set `TRAJECTORY_RECORDING_ENABLED=false` (recording
   restarts in step 9; sessions recorded so far show a paused/resumed gap), pin the new backend tag, check 0 active
   runs, then `docker compose up -d --no-deps backend` and wait for healthy. The business migration renames the
   seven trajectory tables to `legacy_trajectory_*`:

   ```bash
   docker compose exec -T postgres psql -U openbox -d openbox -c '\dt legacy_trajectory_*'
   docker compose exec -T backend alembic current
   ```

   Rollback before step 7 finishes: see §6.
7. **Legacy conversion** (worker container; the password stays out of process arguments):

   ```bash
   export LEGACY_DATABASE_URL="postgresql+asyncpg://openbox:$(grep '^OPENBOX_DB_PASSWORD=' .env | cut -d= -f2-)@postgres:5432/openbox"
   convert() {
     docker compose exec -T -e LEGACY_DATABASE_URL trajectory-worker sh -c \
       'exec python -m trajectory.tools.migrate_legacy --business-database-url "$LEGACY_DATABASE_URL" --legacy-blob-path /legacy-blobs "$@"' sh "$@"
   }
   convert --dry-run
   convert
   convert --verify                                     # must exit 0
   deploy/gw2/scripts/pg-backup.sh --legacy-trajectory-tables
   convert --finalize-drop                              # point of no return for the legacy tables
   unset LEGACY_DATABASE_URL
   ```

8. **Frontend**: pin the new frontend tag, `docker compose up -d --no-deps frontend`, wait for healthy. Check routing:
   `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/api/admin/trajectories/sessions` answers 401 and
   `docker compose logs --since 1m trajectory-worker | grep admin/trajectories` shows the request.
9. **Recording**: reset the statistics, enable recording for internal users, then everyone. Backend and worker
   share the flags, so recreate both (0 active runs for the backend):

   ```bash
   docker compose exec -T postgres psql -U openbox -d openbox -c 'SELECT pg_stat_statements_reset()'
   # backend.env: TRAJECTORY_RECORDING_ENABLED=true, TRAJECTORY_RECORD_USER_IDS=<internal user ids>
   docker compose up -d --no-deps trajectory-worker && docker compose up -d --no-deps backend
   ```

   Use the product with an internal account, then check isolation and flow:

   ```bash
   docker compose exec -T postgres psql -U openbox -d openbox -tAc \
     "SELECT count(*) FROM pg_stat_statements s JOIN pg_database d ON d.oid = s.dbid
      WHERE d.datname = 'openbox' AND s.query ILIKE '%trajectory_%' AND s.query NOT ILIKE '%legacy_trajectory_%'"   # 0
   docker compose exec -T trajectory-worker curl -fsS http://127.0.0.1:8090/metrics
   ```

   The admin UI shows the new session live. Then clear `TRAJECTORY_RECORD_USER_IDS` and recreate worker and backend again.
10. **Operations**: install the timers (§7), apply the lifecycle rules (§9) and alarms (§8) from the operator machine,
    run the drills (§11), and append the release record to docs/DEPLOY.md.

## 6. Rollback

| Situation | Steps |
|---|---|
| Worker misbehaves (any step) | `docker compose stop trajectory-worker`. Business requests keep working; the spool grows up to its budget (2 GiB), after which events are dropped as `spool_full` gaps. Fix and start it again. |
| Backend switched (step 6), before `--finalize-drop` | 0 active runs; downgrade the business chain with the **new** image so the legacy tables get their names back: `docker compose run --rm --no-deps --entrypoint alembic backend downgrade <previous business head>`; pin the previous backend tag; `docker compose up -d --no-deps backend`. Restore `backend.env`. Recording written to `openbox_trace` since the switch is not visible to the old admin UI; keep the database for a later retry. |
| After `--finalize-drop` | The legacy tables are gone from `openbox`. Restore them from the step 7 dump (`pg_restore -U openbox -d openbox --no-owner` of the `openbox-legacy-trajectory-*.dump`, §10), rename them back to their original names, then roll back the backend as above. |
| Frontend | Pin the previous frontend tag, `docker compose up -d --no-deps frontend`. |
| Postgres tuning | Remove `COMPOSE_FILE` from `.env` only together with the backend rollback, then `docker compose up -d --no-deps postgres` in a maintenance window. |
| Whole topology | Order: frontend → backend → worker stop → `.env` without `COMPOSE_FILE` → postgres. |

## 7. Timers

```bash
/opt/openbox/deploy/gw2/scripts/install-timers.sh            # installs /etc/systemd/system/openbox-*.{service,timer}
systemctl list-timers 'openbox-*'
journalctl -u openbox-trajectory-metrics.service -n 20
systemctl start openbox-pg-backup.service                     # run a backup now
/opt/openbox/deploy/gw2/scripts/install-timers.sh --uninstall
```

| Timer | Schedule | Script | State |
|---|---|---|---|
| `openbox-trajectory-metrics.timer` | every minute | `push-metrics.sh` | `/var/lib/openbox-ops/cms-state.json` (counter samples) |
| `openbox-pg-backup.timer` | daily 03:30 Asia/Shanghai, catches up after downtime | `pg-backup.sh` | `/var/backups/openbox/postgres` (only failed uploads stay; removed after 3 days) |
| `openbox-prune-images.timer` | Sunday 04:30 Asia/Shanghai | `prune-images.sh --execute` | — |

Overlapping runs are skipped through `/run/lock/openbox-<name>.lock`. Re-run `install-timers.sh` after a bundle update.

## 8. Metrics and alarms

`push-metrics.sh` pipes host measurements into `python -m trajectory.ops.cms push` in the worker container (a
one-off container while the worker is stopped), which adds the worker's `/health` and `/metrics` and reports with
CloudMonitor `PutCustomMetric` (API 2019-01-01, endpoint `metrics.cn-shanghai.aliyuncs.com`, 21 entries per call),
group `TRAJECTORY_CMS_GROUP_ID` (default `0`) and dimension `instance=gw2`.

| Metric | Source |
|---|---|
| `host_disk_used_percent`, `docker_disk_used_percent` | `df` of `/` and the Docker root |
| `spool_bytes`, `spool_files`, `spool_oldest_age_seconds`, `spool_quarantined_files` | spool volume on the host (non-empty `*.jsonl` and `*.jsonl.part`) |
| `oom_kills_1h`, `backend_oom_kills_1h` | `journalctl -k` `oom-kill:` lines of the last hour (backend: its container id in the memory cgroup) |
| `trace_db_bytes` | `pg_database_size('openbox_trace')` |
| `worker_up`, `worker_writer`, `worker_degraded`, `worker_db_ok`, `worker_spool_ok`, `worker_blob_store_ok` | worker `/health` |
| `ingest_lag_seconds`, `projection_lag_events`, `archive_lag_events`, `gc_queue_depth`, `hot_events_rows`, `trajectories_degraded`, `trajectories_blocked`, `stale_hot_partitions` | worker `/metrics` gauges |
| `<counter>_delta` for `ingest_lines`, `ingest_events`, `duplicates`, `idempotency_conflicts`, `deleted_drops`, `ownership_drops`, `gaps_recorded`, `producer_loss_events`, `quarantined_files`, `blob_puts`, `blob_put_bytes`, `blob_put_failures`, `segment_uploads`, `segment_failures`, `gc_deleted`, `gc_failures`, `exports_built` | worker counters, increase since the previous minute (restarts handled) |
| `gaps_recorded_1h`, `blob_put_failures_5m` | trailing sums of the counters above |

Alarm rules (operator machine; IDs `openbox-<instance>-<name>`, re-running updates them):

```bash
deploy/gw2/scripts/setup-alarms.sh                       # dry run: prints the nine PutCustomMetricRule calls
deploy/gw2/scripts/setup-alarms.sh --execute [--webhook URL]
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

Testing a rule: `oom-kill` evaluates the maximum of one minute, so
`docker compose exec -T trajectory-worker python -m trajectory.ops.cms put --metric oom_kills_1h=1` raises it once
(the next timer run reports the real value 0 again). Metrics that stop arriving do not alarm: check
`systemctl list-timers 'openbox-*'` during the weekly review, or add a no-data alert in the CloudMonitor console.

## 9. OSS lifecycle rules

`oss-lifecycle.xml` (IDs start with `openbox-`):

| Rule | Prefix | Action |
|---|---|---|
| `openbox-trajectories-ia-30d` | `trajectories/` except `trajectories/_exports/` | Transition to IA after 30 days (objects under 64 KB stay Standard) |
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
A database that does not exist yet is skipped.

```bash
deploy/gw2/scripts/pg-backup.sh                                  # openbox and openbox_trace
deploy/gw2/scripts/pg-backup.sh --database openbox --keep-local  # one database, keep the local file
docker compose exec -T trajectory-worker python -m trajectory.ops.backup verify --key <key> --size <bytes> --sha256 <hex>
```

Restore check (never over the live database):

```bash
# operator machine: a presigned GET for the object
aliyun ossutil presign oss://<bucket>/<key> --expires-duration 1h -e https://oss-cn-shanghai-internal.aliyuncs.com --region cn-shanghai
# gw2
curl -sS -o /var/backups/openbox/restore.dump '<presigned URL>'
sha256sum /var/backups/openbox/restore.dump        # equals x-oss-meta-sha256 printed by the verify command
docker compose exec -T postgres createdb -U openbox openbox_restore_check
docker compose exec -T postgres pg_restore -U openbox -d openbox_restore_check --no-owner < /var/backups/openbox/restore.dump
docker compose exec -T postgres dropdb -U openbox openbox_restore_check && rm -f /var/backups/openbox/restore.dump
```

A real restore stops the writers first (`backend`, `trajectory-worker`), restores into a new database, swaps names,
and starts the services one at a time.

## 11. Drills

Run after release step 10 and then quarterly, in a quiet period, with an internal test account producing traffic.
Every drill is a dry run without `--execute`, restores the service when interrupted (Ctrl-C), and exits non-zero when a
pass criterion fails.

| Drill | Command | Proves | Pass criteria | Impact |
|---|---|---|---|---|
| Worker stop | `drill-worker-stop.sh --minutes 5 --execute` | Business is independent of the worker; the spool buffers | All backend probes pass; spool drains after the restart; `producer_loss_events` 0 | Admin trajectory UI unavailable for the window |
| Blob outage | `drill-blob-outage.sh --minutes 10 --execute` | Ingest survives OSS failures with backoff | All backend probes pass; `blob_put_failures` rises; spool drains after the fault | Content recorded in the window may become `blob_store_unavailable` gaps |
| Spool full | `drill-spool-full.sh --minutes 3 --execute` | The spool budget drops events instead of blocking | All backend probes pass; `gaps_recorded` increases after the restore | Two backend recreations (each waits for 0 active runs) |
| Rebuild | `rebuild-trace-db.sh --limit 50 --execute` | Archived segments in OSS restore the event history | Exit 0: segment sha256, event counts, sequence ranges, event keys and stream digests match | Read-only on `openbox_trace`; a scratch database `openbox_trace_rebuild_*` exists during the run |

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
| PostgreSQL backups | 30 days in OSS; failed local dumps 3 days | lifecycle rule, `pg-backup.sh` |
| Docker images | in use, pinned, and the newest 3 tags per repository | `prune-images.sh` weekly |
| Spool | consumed files deleted after ingest; budget 2 GiB (`TRAJECTORY_SPOOL_MAX_BYTES`) | worker, emitter |
| Worker logs | 5 × 50 MB | compose logging options |

## 13. Troubleshooting

- **Worker restarts or stays unhealthy**: `docker compose logs --tail 200 trajectory-worker`. Typical causes: trace
  migrations (`TRAJECTORY_DATABASE_URL`, `openbox_trace` missing, `pg_trgm` privileges), OSS credentials, memory limit.
- **`"writer": false`**: another process holds the writer lock (a stray `docker compose run` of the worker). Find it:
  `SELECT pid, application_name, backend_start FROM pg_stat_activity WHERE datname = 'openbox_trace'` and
  `SELECT pid FROM pg_locks WHERE locktype = 'advisory'`; stop the extra container.
- **Spool keeps growing**: worker stopped or lagging (`ingest_lag_seconds`), disk full, or quarantined files blocking a
  producer. Business requests are not affected; at the budget the backend records gaps.
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
