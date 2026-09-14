#!/usr/bin/env bash
# Daily PostgreSQL backups to OSS (SPEC §12). For each database, pg_dump -Fc runs inside the postgres
# container, pg_restore --list checks the dump, trajectory.ops.backup in the worker image uploads it to
# backups/postgres/<YYYYMMDD, Asia/Shanghai>/<name>-<UTC stamp>.dump through a presigned PUT signed
# with the mounted aliyun credentials and verifies size and sha256 with a signed HEAD, and the local
# copy is removed. A database that does not exist yet is skipped.
#
#   pg-backup.sh [--database NAME]... [--legacy-trajectory-tables] [--local-dir DIR] [--keep-local]
#
# Defaults: databases openbox and openbox_trace, local directory /var/backups/openbox/postgres.
# --legacy-trajectory-tables dumps only public.legacy_trajectory_* of the business database
# (release step 7, before migrate_legacy --finalize-drop).
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
# Dumps left behind by failed runs.
find "$local_dir" -maxdepth 1 -type f \( -name '*.dump' -o -name '*.dump.partial' \) -mtime +3 -delete

day=$(TZ=Asia/Shanghai date +%Y%m%d)
stamp=$(date -u +%Y%m%dT%H%M%SZ)

# backup DATABASE NAME [pg_dump options...]
backup() {
  local database=$1 name=$2 file key size digest
  shift 2
  file="$local_dir/$name-$stamp.dump"
  key="backups/postgres/$day/$name-$stamp.dump"
  log "dumping $database to $file"
  if ! (umask 077 && compose exec -T postgres pg_dump -U "$OPENBOX_PG_USER" -Fc "$@" "$database" >"$file.partial"); then
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
for database in $databases; do
  if ! database_exists "$database"; then
    log "skipping $database: the database does not exist"
    continue
  fi
  if [ "$legacy" = 1 ]; then
    backup "$database" "$database-legacy-trajectory" "--table=public.legacy_trajectory_*" || failed=$((failed + 1))
  else
    backup "$database" "$database" || failed=$((failed + 1))
  fi
done
[ "$failed" = 0 ] || die "$failed backup(s) failed"
log "backups complete"
