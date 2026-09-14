#!/usr/bin/env bash
# Drill (SPEC §12): restore archived trajectory segments into a scratch trace database and compare
# digests (backend/trajectory/ops/rebuild.py). Creates openbox_trace_rebuild_<UTC stamp>, migrates it
# with the worker image's trace alembic chain, runs the rebuild in a one-off worker container (the
# live openbox_trace is only read), writes the JSON report to /opt/openbox/backups/rebuild-drill/ and
# drops the scratch database unless --keep. Exit status 1 means a mismatch. Dry run unless --execute.
#
#   rebuild-trace-db.sh [--only TRAJECTORY_ID]... [--limit N] [--keep] [--execute]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

only=
limit=
keep=0
while [ $# -gt 0 ]; do
  case "$1" in
    --only)
      need_value "$1" $#
      [[ $2 =~ ^trj_[A-Za-z0-9_-]{1,64}$ ]] || die "invalid trajectory id: $2"
      only="$only $2"
      shift 2
      ;;
    --limit)
      need_value "$1" $#
      positive_integer "$2" || die "--limit must be a positive integer"
      limit=$2
      shift 2
      ;;
    --keep)
      keep=1
      shift
      ;;
    --execute)
      EXECUTE=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
scratch=openbox_trace_rebuild_$(date -u +%Y%m%dt%H%M%Sz)
if [ "$EXECUTE" != 1 ]; then
  log "dry run: would create $scratch, migrate it, rebuild ${only:-every live trajectory}${limit:+ (at most $limit)} from the archived segments and compare digests; re-run with --execute"
  exit 0
fi

require_overlay
service_running postgres || die "the postgres service is not running"
database_exists "$OPENBOX_TRACE_DB" || die "$OPENBOX_TRACE_DB does not exist"
password=$(env_value OPENBOX_DB_PASSWORD)
[ -n "$password" ] || die "OPENBOX_DB_PASSWORD is not set in $OPENBOX_DIR/.env"
# Exported and handed to docker by name, so the password stays out of process arguments.
export REBUILD_SCRATCH_URL="postgresql+asyncpg://$OPENBOX_PG_USER:$password@postgres:5432/$scratch"
report_dir=$OPENBOX_DIR/backups/rebuild-drill
report=$report_dir/$scratch.json

log "creating $scratch"
psql_run postgres "CREATE DATABASE \"$scratch\" OWNER \"$OPENBOX_PG_USER\""
cleanup() {
  if [ "$keep" = 1 ]; then
    log "kept $scratch; drop it later with: DROP DATABASE \"$scratch\""
  else
    log "dropping $scratch"
    psql_run postgres "DROP DATABASE IF EXISTS \"$scratch\" WITH (FORCE)" || warn "could not drop $scratch"
  fi
}
trap cleanup EXIT
psql_run "$scratch" "CREATE EXTENSION IF NOT EXISTS pg_trgm"

log "migrating $scratch with the trace alembic chain"
(
  export TRAJECTORY_DATABASE_URL=$REBUILD_SCRATCH_URL
  compose run --rm --no-deps -T -e TRAJECTORY_DATABASE_URL --entrypoint alembic trajectory-worker \
    -c alembic_trajectory.ini upgrade head
) || die "the trace migrations failed on $scratch"

set -- -m trajectory.ops.rebuild
if [ -n "$only" ]; then
  # shellcheck disable=SC2086 # validated ids, one argument each
  set -- "$@" --only $only
fi
if [ -n "$limit" ]; then
  set -- "$@" --limit "$limit"
fi
install -d -m 0700 "$report_dir"
log "rebuilding; report: $report"
status=0
compose run --rm --no-deps -T -e REBUILD_SCRATCH_URL --entrypoint python trajectory-worker "$@" >"$report" || status=$?
case "$status" in
  0) log "drill passed: every rebuilt trajectory matches its source" ;;
  1) warn "mismatches found, see $report" ;;
  *) warn "the rebuild could not run (exit $status)" ;;
esac
exit "$status"
