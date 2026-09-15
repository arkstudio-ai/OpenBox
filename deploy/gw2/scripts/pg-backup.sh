#!/usr/bin/env bash
# Daily PostgreSQL backups to OSS (SPEC §12). For each database, pg_dump -Fc runs inside the postgres
# container, pg_restore --list checks the dump, trajectory.ops.backup in the worker image uploads it to
# backups/postgres/<YYYYMMDD, Asia/Shanghai>/<name>-<UTC stamp>.dump through a presigned PUT signed
# with the mounted aliyun credentials and verifies size and sha256 with a signed HEAD, and the local
# copy is removed. A database that does not exist yet is skipped. The run exits non-zero when
# PostgreSQL cannot be queried, a dump or upload fails, or no database was backed up.
#
#   pg-backup.sh [--database NAME]... [--local-dir DIR] [--keep-local]
#
# Defaults: databases openbox and openbox_trace, local directory /var/backups/openbox/postgres.
# Local dumps left by failed uploads or --keep-local are deleted by a run 3 days later; only file
# names this script creates are matched. Old trajectory recordings are not kept: the dump of the
# business database has the schema but no rows of public.session_trajectories, public.trajectory_*
# and public.legacy_trajectory_* (once the business migration has dropped them, these match nothing).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

databases=
keep_local=0
local_dir=${OPENBOX_BACKUP_DIR:-/var/backups/openbox/postgres}
while [ $# -gt 0 ]; do
  case "$1" in
    --database)
      need_value "$1" $#
      databases="$databases $2"
      shift 2
      ;;
    --local-dir)
      need_value "$1" $#
      local_dir=$2
      shift 2
      ;;
    --keep-local)
      keep_local=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done
if [ -z "$databases" ]; then
  databases="$OPENBOX_BUSINESS_DB $OPENBOX_TRACE_DB"
fi
for database in $databases; do
  valid_identifier "$database" || die "invalid database name: $database"
done

take_lock pg-backup
require_overlay
install -d -m 0700 "$local_dir"
# Dumps of earlier runs. The pattern matches this script's file names only, so other files in a
# --local-dir (a preflight.dump, say) are never deleted.
find "$local_dir" -maxdepth 1 -type f \
  \( -name '*-????????T??????Z.dump' -o -name '*-????????T??????Z.dump.partial' \) -mtime +3 -delete

day=$(TZ=Asia/Shanghai date +%Y%m%d)
stamp=$(date -u +%Y%m%dT%H%M%SZ)

# backup DATABASE
backup() {
  local database=$1 schema= file key size digest
  file="$local_dir/$database-$stamp.dump"
  key="backups/postgres/$day/$database-$stamp.dump"
  if [ "$database" = "$OPENBOX_BUSINESS_DB" ]; then
    schema=public
  fi
  log "dumping $database to $file"
  # ${schema:+...} adds the quoted --exclude-table-data options for the business database only (safe with
  # set -u on bash 3.2); the trace database keeps every row.
  if ! (umask 077 && compose exec -T postgres pg_dump -U "$OPENBOX_PG_USER" -Fc \
    ${schema:+"--exclude-table-data=$schema.session_trajectories"} \
    ${schema:+"--exclude-table-data=$schema.trajectory_*"} \
    ${schema:+"--exclude-table-data=$schema.legacy_trajectory_*"} \
    "$database" >"$file.partial"); then
    rm -f "$file.partial"
    warn "pg_dump of $database failed"
    return 1
  fi
  mv "$file.partial" "$file"
  if ! compose exec -T postgres pg_restore --list <"$file" >/dev/null; then
    warn "pg_restore --list cannot read $file"
    return 1
  fi
  size=$(wc -c <"$file" | tr -d ' ')
  digest=$(sha256sum "$file" | cut -d ' ' -f 1)
  log "uploading $size bytes to $key"
  if ! worker_python -m trajectory.ops.backup upload --key "$key" --size "$size" --sha256 "$digest" <"$file"; then
    warn "upload of $file failed; the local copy is kept"
    return 1
  fi
  if [ "$keep_local" = 0 ]; then
    rm -f "$file"
  fi
}

failed=0
backed_up=0
for database in $databases; do
  status=0
  database_exists "$database" || status=$?
  case "$status" in
    0) ;;
    1)
      log "skipping $database: the database does not exist"
      continue
      ;;
    *)
      warn "cannot query PostgreSQL for $database"
      failed=$((failed + 1))
      continue
      ;;
  esac
  result=0
  backup "$database" || result=$?
  if [ "$result" = 0 ]; then
    backed_up=$((backed_up + 1))
  else
    failed=$((failed + 1))
  fi
done
[ "$failed" = 0 ] || die "$failed backup(s) failed"
[ "$backed_up" -gt 0 ] || die "no database was backed up (databases:$databases)"
log "backups complete"
