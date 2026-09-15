# w3-ops report

Everything for w3-ops is done and committed on `wp/w3-ops`; nothing was pushed. After the coordinator's message I ran no more tests.

**Branch:** `wp/w3-ops`, based on `7c6b5ba`.
**Head:** `b6bc24eae78f81d475cafd1d017935da36fced9d` (`fd4e6f9` code and tests, `b6bc24e` docs).

## Summary
1. **Compose examples:** the base, override and `env.example` now match the gw2 files from the discovery output. Every item on the task's list is covered, with placeholders only.
2. **Trace role:**
   - `create-trace-db.sh` creates role `openbox_trace` with its password from `.env`, sent to psql on stdin, plus the `5s` / `32MB` defaults.
   - It creates the `openbox_trace` database owned by that role, `pg_trgm` in it, and `pg_stat_statements` in `openbox`.
   - It refuses to run without a password.
   - The overlay connects the worker as that role and sets `DATABASE_URL: ""`. The AKS example blanks `DATABASE_URL` on the worker, and both k8s manifests show the role URL.
3. **Metrics and alarms:**
   - `push-metrics.sh` and `trajectory.ops.cms` now report `backend_cpu_percent` and `backend_mem_percent` (from `docker stats`) and `business_trajectory_statements`. They also pass through the contract 5 worker names.
   - `setup-alarms.sh` has the 7 new rules, 16 in total.
4. **Analytics timer:** `analytics-export.sh` runs daily at 04:00 Asia/Shanghai with catch-up. It reports `analytics_export_failed=1` and exits non-zero on failure. `install-timers.sh` installs and removes the units.
5. **Drills:**
   - `drill-delete-session.sh`: tokens go to curl on stdin only; the OSS and tombstone checks run through the new `trajectory.ops.deletion`.
   - `restore-check.sh`: restores into a scratch database, then drops it.
   - Worker-stop drill: 15 minutes, spool must drain within 5 minutes of the restart. Blob-outage drill: 30 minutes.
6. **Docs:** RUNBOOK, DEPLOY.md section 五 and SPEC §12 carry all corrections from task 6. DEPLOY.md sections 二 and 三 got short notes that `OPENBOX_IMAGE_TAG` is stale.

## Changed files
All under `/Users/wang/workspace/OpenBox/.claude/worktrees/agent-adbe536a346f547df/`:
- `deploy/gw2/`:
  - `RUNBOOK.md`, `docker-compose.base.example.yml`, `docker-compose.override.example.yml`, `docker-compose.trajectory.yml`, `env.example`
  - `scripts/{lib,create-trace-db,push-metrics,setup-alarms,install-timers,drill-worker-stop,drill-blob-outage}.sh`
  - new: `scripts/{analytics-export,drill-delete-session,restore-check}.sh`
  - new: `systemd/openbox-trajectory-analytics.{service,timer}`
- `backend/trajectory/ops/{__init__,cms,backup}.py`; new: `deletion.py`, `latency.py`
- `backend/tests/unit/test_trajectory_ops_{deploy_assets,cms,backup}.py`; new: `test_trajectory_ops_{deletion,latency}.py`
- `k8s/base.yaml`, `k8s/aks.yaml`, `docs/DEPLOY.md`, `docs/trajectory-rearch/SPEC.md` (§12 only)

## Checks
| Command | Result |
|---|---|
| `uv run pytest tests/unit/test_trajectory_ops_*.py -q` | 204 passed, 0 failed, 1 skipped (`systemd-analyze` not installed) |
| `uv run pytest tests/unit -q` | 2820 passed, 0 failed, 1 skipped |
| `docker compose` with base + overlay + override `config --quiet` | OK, with the `env.example` values in the environment (no `.env` written) |
| `bash -n` on every changed script | OK |

## New settings and keys
- **`.env`:** `OPENBOX_TRACE_DB_PASSWORD` (16–128 characters from `A-Za-z0-9._~-`; use `openssl rand -hex 32`). `COMPOSE_FILE` is now listed in `env.example`.
- **Script environment:** `OPENBOX_TRACE_ROLE` (default `openbox_trace`), `OPENBOX_RESTORE_DIR` (default `/var/backups/openbox/restore-check`).
- **Metrics:** `backend_cpu_percent`, `backend_mem_percent`, `business_trajectory_statements`, `analytics_export_failed`.
- **New commands:**
  - `python -m trajectory.ops.backup download`
  - `python -m trajectory.ops.deletion precheck|verify`
  - `python -m trajectory.ops.latency`

## Deviations
- **Additions beyond the spec:**
  - **`trajectory.ops.latency`:** a Python helper for the p95/CPU comparison, because awk differs between the server's mawk and BSD awk.
  - **`backup download`:** lets `restore-check.sh --key` fetch the dump on gw2 itself, with size and sha256 checks.
- **Password handling:**
  - The password character set is restricted.
  - The role statements run with statement logging and `pg_stat_statements` tracking switched off, so the password stays out of the server log.
  - `pg_stat_statements` is no longer created in `openbox_trace`.
  - For an existing trace database, the script hands the database to the role but only warns about tables owned by another role.
- **Analytics script:**
  - It also reports `0` on success, which clears the alarm.
  - It waits up to 600 s for a healthy worker.
  - It reports failures from a one-off container, so a stopped worker still gets reported.
- **Drill safeguards:** both new scripts are dry runs unless `--execute`. The deletion drill deletes nothing unless the admin API answers 200 first and objects exist under the prefix, since an empty prefix would prove nothing.
- **Thresholds I chose:** archive lag ≥ 50000 events for 15 minutes; `events-ingested-24h` at level INFO.
- **Overlay:** the password is plain `${OPENBOX_TRACE_DB_PASSWORD}` interpolation rather than `:?`, so a missing value can't break every compose command. The worker's `depends_on` uses `service_healthy`.
- **Purge step (RUNBOOK):**
  - It uses a temporary compose file to mount `/legacy-blobs` writable; I checked the merge locally with `docker compose config`.
  - It passes the business URL, because the wave 2 converter requires it in every mode.

## Open issues
1. **`hot_partitions` > 10 may alarm permanently.** If the gauge counts every partition (7 hot days plus 8 created ahead, about 15), the rule never clears. Confirm the w3-harden-worker definition at integration.
2. **`rebuild.py` and the 5 s timeout.** It now reads the live trace database as `openbox_trace` with the 5 s default and sets no `SET LOCAL statement_timeout`. I didn't modify it, per instructions.
3. **Empty `DATABASE_URL` in the worker.** The worker and `trajectory.ops.deletion` build the blob store through `core.config.get_config()`. If that rejects an empty `DATABASE_URL`, both fail.
4. **Analytics memory.** The export runs inside the worker container and shares its 1 GiB limit.
5. **Log format for the comparison.** `trajectory.ops.latency` expects a `"METHOD PATH HTTP/x" STATUS` field followed by `rt=<seconds>`. The w3-frontend format isn't in my base; adjust the parser if it differs.
6. **Purge semantics.** I assumed `--purge-legacy-blobs` resolves files relative to `--legacy-blob-path`; this is untested against w3-harden-worker.
7. **Not verified:** the `nginx:1.31.5-alpine` tag, and whether the real compose file inlines the database password.
8. **Isolation check persistence.** `business_trajectory_statements` is cumulative, so the alarm stays on until `pg_stat_statements_reset()` (documented in the RUNBOOK).
9. **Remaining role access.** `openbox_trace` can still connect to `openbox` through the default PUBLIC CONNECT privilege, though it can't read any tables. I did not revoke it.
