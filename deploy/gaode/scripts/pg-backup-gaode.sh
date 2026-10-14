#!/usr/bin/env bash
# Daily PostgreSQL backup of the Gaode production host (no trajectory overlay there): pg_dump -Fc of the
# business database inside the postgres container, pg_restore --list to check it, upload to OSS
# backups/postgres-gaode/<YYYYMMDD Asia/Shanghai>/openbox-<UTC stamp>.dump through the backend image's
# trajectory.ops.backup (presigned PUT with the mounted aliyun credentials, size + sha256 verified by a
# signed HEAD), then the local copy is removed. Local dumps older than 3 days (failed uploads or
# --keep-local) are deleted. Exits non-zero when the dump, the check or the upload fails.
#
#   pg-backup-gaode.sh [--keep-local] [--local-dir /var/backups/openbox/postgres]
set -euo pipefail
OPENBOX_DIR=${OPENBOX_DIR:-/opt/openbox}
PG_USER=${OPENBOX_PG_USER:-openbox}
DATABASE=${OPENBOX_BUSINESS_DB:-openbox}
keep_local=0
local_dir=${OPENBOX_BACKUP_DIR:-/var/backups/openbox/postgres}
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-local) keep_local=1; shift ;;
    --local-dir) local_dir=$2; shift 2 ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done
log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
compose() { (cd "$OPENBOX_DIR" && docker compose "$@"); }

exec 9>/run/lock/openbox-pg-backup.lock
flock -n 9 || { log "another backup is in progress"; exit 0; }
install -d -m 0700 "$local_dir"
find "$local_dir" -maxdepth 1 -type f \( -name '*-????????T??????Z.dump' -o -name '*-????????T??????Z.dump.partial' \) -mtime +3 -delete

day=$(TZ=Asia/Shanghai date +%Y%m%d)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
file="$local_dir/$DATABASE-$stamp.dump"
key="backups/postgres-gaode/$day/$DATABASE-$stamp.dump"

log "dumping $DATABASE to $file"
if ! (umask 077 && compose exec -T postgres pg_dump -U "$PG_USER" -Fc \
      --exclude-table-data='public.session_trajectories' --exclude-table-data='public.trajectory_*' \
      --exclude-table-data='public.legacy_trajectory_*' "$DATABASE" >"$file.partial"); then
  rm -f "$file.partial"; log "ERROR: pg_dump failed"; exit 1
fi
mv "$file.partial" "$file"
tables=$(compose exec -T postgres pg_restore --list <"$file" | grep -c 'TABLE DATA' || true)
[ "$tables" -gt 0 ] || { log "ERROR: pg_restore --list shows no table data in $file"; exit 1; }
size=$(wc -c <"$file" | tr -d ' ')
digest=$(sha256sum "$file" | cut -d ' ' -f 1)
log "dump ok: $tables tables, $size bytes, sha256 $digest"
log "uploading to $key"
if ! compose exec -T backend python -m trajectory.ops.backup upload --key "$key" --size "$size" --sha256 "$digest" <"$file"; then
  log "ERROR: upload failed; the local copy is kept at $file"; exit 1
fi
if [ "$keep_local" = 0 ]; then rm -f "$file"; fi
log "backup complete: $key"
