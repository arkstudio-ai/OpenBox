#!/usr/bin/env bash
# Daily PostgreSQL backups to OSS (SPEC §12). For each database, pg_dump -Fc runs inside the postgres
# container, pg_restore --list checks the dump, trajectory.ops.backup in the worker image uploads it to
# backups/postgres/<YYYYMMDD, Asia/Shanghai>/<name>-<UTC stamp>.dump through a presigned PUT signed
# with the mounted aliyun credentials and verifies size and sha256 with a signed HEAD, and the local
# copy is removed. A database that does not exist yet is skipped. The run exits non-zero when
# PostgreSQL cannot be queried, a dump or upload fails, or no database was backed up.
#
#   pg-backup.sh [--database NAME]... [--legacy-trajectory-tables] [--local-dir DIR] [--keep-local]
#
# Defaults: databases openbox and openbox_trace, local directory /var/backups/openbox/postgres.
# Local dumps left by failed uploads or --keep-local are deleted by a run 3 days later; only file
# names this script creates are matched. --legacy-trajectory-tables dumps only
# public.legacy_trajectory_* of the business database (release step 7, before
# migrate_legacy --finalize-drop).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

databases=
legacy=0
keep_local=0
local_dir=${OPENBOX_BACKUP_DIR:-/var/backups/openbox/postgres}
while [ $# -gt 0 ]; do
  case "$1" in
    --database)
      need_value "$1" $#
      databases="$databases $2"
      shift 2
      ;;
    --legacy-trajectory-tables)
      legacy=1
      shift
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
  if [ "$legacy" = 1 ]; then
    databases=$OPENBOX_BUSINESS_DB
  else
    databases="$OPENBOX_BUSINESS_DB $OPENBOX_TRACE_DB"
  fi
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

# backup DATABASE NAME [TABLE_PATTERN]
backup() {
  local database=$1 name=$2 tables=${3:-} file key size digest
  file="$local_dir/$name-$stamp.dump"
  key="backups/postgres/$day/$name-$stamp.dump"
  log "dumping $database to $file"
  # ${tables:+...} adds the quoted --table option only when a pattern is given (safe with set -u on bash 3.2).
  if ! (umask 077 && compose exec -T postgres pg_dump -U "$OPENBOX_PG_USER" -Fc ${tables:+"--table=$tables"} "$database" >"$file.partial"); then
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
  if [ "$legacy" = 1 ]; then
    result=0
    backup "$database" "$database-legacy-trajectory" "public.legacy_trajectory_*" || result=$?
  else
    result=0
    backup "$database" "$database" || result=$?
  fi
  if [ "$result" = 0 ]; then
    backed_up=$((backed_up + 1))
  else
    failed=$((failed + 1))
  fi
done
[ "$failed" = 0 ] || die "$failed backup(s) failed"
[ "$backed_up" -gt 0 ] || die "no database was backed up (databases:$databases)"
log "backups complete"
