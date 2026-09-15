#!/usr/bin/env bash
# Create the trajectory trace database, its login role and extensions (SPEC §12, release step 4).
# Idempotent. The role openbox_trace (LOGIN) gets its password from OPENBOX_TRACE_DB_PASSWORD in
# $OPENBOX_DIR/.env and the role defaults statement_timeout = '5s' and work_mem = '32MB'. The database
# openbox_trace is created owned by the role (an existing one is handed over to it) and gets pg_trgm.
# pg_stat_statements is created in the business database openbox once postgres runs with
# shared_preload_libraries=pg_stat_statements (the trajectory overlay), so re-run the script after
# postgres has been recreated with the overlay.
# The password never appears in a process's arguments: the role statements reach psql on stdin, in a
# session that neither logs statements nor tracks them in pg_stat_statements. The script refuses to run
# without a password of 16 to 128 letters, digits, ".", "_", "~" or "-" (openssl rand -hex 32).
#
#   create-trace-db.sh [--database openbox_trace] [--dry-run]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

database=$OPENBOX_TRACE_DB
role=$OPENBOX_TRACE_ROLE
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
password=$(trace_db_password)

sql() {
  if [ "$dry_run" = 1 ]; then
    log "dry-run ($1): $2"
  else
    log "$1: $2"
    psql_run "$1" "$2"
  fi
}

# role_sql PASSWORD: the role statements, printed with builtins only (no argument lists, no temp files).
role_sql() {
  printf '%s\n' \
    "SET log_statement = 'none';" \
    "SET log_min_error_statement = 'panic';" \
    "SET pg_stat_statements.track = 'none';" \
    "SET password_encryption = 'scram-sha-256';" \
    "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$role') THEN CREATE ROLE \"$role\" LOGIN; END IF; END \$\$;" \
    "ALTER ROLE \"$role\" WITH LOGIN PASSWORD '$1';" \
    "ALTER ROLE \"$role\" SET statement_timeout = '5s';" \
    "ALTER ROLE \"$role\" SET work_mem = '32MB';"
}

service_running postgres || die "the postgres service is not running in $OPENBOX_DIR"

status=0
database_exists "$database" || status=$?
case "$status" in
  0 | 1) ;;
  *) die "cannot query PostgreSQL in $OPENBOX_DIR; nothing was changed" ;;
esac

if [ "$dry_run" = 1 ]; then
  while IFS= read -r statement; do
    log "dry-run (postgres, stdin): $statement"
  done <<<"$(role_sql '***')"
else
  log "postgres: role $role (LOGIN, password from .env, statement_timeout 5s, work_mem 32MB)"
  role_sql "$password" | psql_stdin postgres
fi

if [ "$status" = 1 ]; then
  # CREATE DATABASE cannot run in a transaction; psql -c runs it on its own.
  sql postgres "CREATE DATABASE \"$database\" OWNER \"$role\""
else
  log "database $database exists"
  owner=$(psql_scalar postgres "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '$database'")
  if [ "$owner" != "$role" ]; then
    sql postgres "ALTER DATABASE \"$database\" OWNER TO \"$role\""
  fi
  # Tables created by an earlier run as another role stay theirs; the trace migrations would then fail.
  foreign=$(psql_scalar "$database" "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_toast%' AND pg_get_userbyid(c.relowner) <> '$role' AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')")
  if [ "$foreign" != 0 ]; then
    warn "$foreign relation(s) in $database belong to another role; the trace migrations run as $role and must own them (ALTER TABLE ... OWNER TO $role)"
  fi
fi
sql "$database" "CREATE EXTENSION IF NOT EXISTS pg_trgm"

preload=$(psql_scalar postgres "SHOW shared_preload_libraries" | tr -d ' ')
case ",$preload," in
  *,pg_stat_statements,*)
    sql "$OPENBOX_BUSINESS_DB" "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"
    ;;
  *)
    log "pg_stat_statements is not in shared_preload_libraries yet; re-run after postgres is recreated with the trajectory overlay"
    ;;
esac

if [ "$dry_run" = 0 ]; then
  log "$role settings: $(psql_scalar postgres "SELECT coalesce(array_to_string(rolconfig, ', '), 'none') FROM pg_roles WHERE rolname = '$role'")"
  log "$database extensions: $(psql_scalar "$database" "SELECT string_agg(extname || ' ' || extversion, ', ' ORDER BY extname) FROM pg_extension")"
fi
