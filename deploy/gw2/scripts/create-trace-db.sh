#!/usr/bin/env bash
# Create the trajectory trace database and its extensions (SPEC §12, release step 4).
# Idempotent: an existing database or extension is left as it is. pg_trgm is created in the trace
# database; pg_stat_statements in the business and trace databases once postgres runs with
# shared_preload_libraries=pg_stat_statements (the trajectory overlay), so re-run the script after
# postgres has been recreated with the overlay.
#
#   create-trace-db.sh [--database openbox_trace] [--dry-run]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

database=$OPENBOX_TRACE_DB
dry_run=0
while [ $# -gt 0 ]; do
  case "$1" in
    --database)
      need_value "$1" $#
      database=$2
      shift 2
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
valid_identifier "$database" || die "invalid database name: $database"

sql() {
  if [ "$dry_run" = 1 ]; then
    log "dry-run ($1): $2"
  else
    log "$1: $2"
    psql_run "$1" "$2"
  fi
}

service_running postgres || die "the postgres service is not running in $OPENBOX_DIR"

if database_exists "$database"; then
  log "database $database exists"
else
  # CREATE DATABASE cannot run in a transaction; psql -c runs it on its own.
  sql postgres "CREATE DATABASE \"$database\" OWNER \"$OPENBOX_PG_USER\""
fi
sql "$database" "CREATE EXTENSION IF NOT EXISTS pg_trgm"

preload=$(psql_scalar postgres "SHOW shared_preload_libraries" | tr -d ' ')
case ",$preload," in
  *,pg_stat_statements,*)
    for name in "$OPENBOX_BUSINESS_DB" "$database"; do
      sql "$name" "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"
    done
    ;;
  *)
    log "pg_stat_statements is not in shared_preload_libraries yet; re-run after postgres is recreated with the trajectory overlay"
    ;;
esac

if database_exists "$database"; then
  log "$database extensions: $(psql_scalar "$database" "SELECT string_agg(extname || ' ' || extversion, ', ' ORDER BY extname) FROM pg_extension")"
fi
