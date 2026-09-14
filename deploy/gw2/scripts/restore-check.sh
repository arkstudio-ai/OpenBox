#!/usr/bin/env bash
# Backup restore check (RUNBOOK.md §10). Restores a pg_dump -Fc backup into a new scratch database
# openbox_restore_check_<UTC stamp>, checks that it holds every table of the dump's table of contents,
# reports its size and drops it again, also when the check fails or is interrupted. Live databases are
# never restored into or dropped: the scratch name is generated here and must differ from the business,
# trace and system database names. The dump is a local file (--file) or a backup object (--key)
# downloaded into --local-dir through `python -m trajectory.ops.backup download` in the worker image,
# which checks the size and the sha256 recorded at upload; a downloaded dump is deleted afterwards
# unless --keep-file. The restore needs free disk space of about the database's size.
# Dry run unless --execute.
#
#   restore-check.sh (--file PATH | --key backups/postgres/<YYYYMMDD>/<name>.dump)
#                    [--local-dir /var/backups/openbox/restore-check] [--keep-file] [--execute]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

file=
key=
keep_file=0
local_dir=${OPENBOX_RESTORE_DIR:-/var/backups/openbox/restore-check}
while [ $# -gt 0 ]; do
  case "$1" in
    --file)
      need_value "$1" $#
      file=$2
      shift 2
      ;;
    --key)
      need_value "$1" $#
      key=$2
      shift 2
      ;;
    --local-dir)
      need_value "$1" $#
      local_dir=$2
      shift 2
      ;;
    --keep-file)
      keep_file=1
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
if [ -n "$file" ] && [ -n "$key" ]; then
  die "use either --file or --key"
fi
[ -n "$file" ] || [ -n "$key" ] || die "--file or --key is required"
if [ -n "$key" ]; then
  [[ $key =~ ^backups/[A-Za-z0-9._/-]+$ && $key != *..* && $key != *//* && $key != */ ]] ||
    die "--key must be an object key under backups/: $key"
fi
if [ -n "$file" ] && [ ! -f "$file" ]; then
  die "no such file: $file"
fi
scratch=openbox_restore_check_$(date -u +%Y%m%dt%H%M%Sz)
valid_identifier "$scratch" || die "invalid scratch database name: $scratch"
for live in "$OPENBOX_BUSINESS_DB" "$OPENBOX_TRACE_DB" postgres template0 template1; do
  [ "$scratch" != "$live" ] || die "refusing to restore into $live"
done
if [ "$EXECUTE" != 1 ]; then
  log "dry run: would ${key:+download $key into $local_dir, }restore ${file:-it} into the new database $scratch, compare its tables with the dump and drop $scratch; re-run with --execute"
  exit 0
fi

require_overlay
service_running postgres || die "the postgres service is not running in $OPENBOX_DIR"
work=$(mktemp -d "${TMPDIR:-/tmp}/openbox-restore-check.XXXXXX")
created=0
downloaded=
cleanup() {
  if [ "$created" = 1 ]; then
    log "dropping $scratch"
    psql_run postgres "DROP DATABASE IF EXISTS \"$scratch\" WITH (FORCE)" >/dev/null ||
      warn "could not drop $scratch; drop it with: DROP DATABASE \"$scratch\" WITH (FORCE)"
  fi
  if [ -n "$downloaded" ]; then
    rm -f "$downloaded.partial"
    if [ "$keep_file" = 0 ]; then
      rm -f "$downloaded"
    fi
  fi
  rm -rf "$work"
}
trap cleanup EXIT

if [ -n "$key" ]; then
  install -d -m 0700 "$local_dir"
  downloaded=$local_dir/$scratch.dump
  file=$downloaded
  log "downloading $key to $file"
  (umask 077 && worker_python -m trajectory.ops.backup download --key "$key" >"$file.partial") ||
    die "the download of $key failed"
  mv "$file.partial" "$file"
fi

compose exec -T postgres pg_restore --list <"$file" >"$work/toc" || die "pg_restore --list cannot read $file"
# Table entries of the table of contents: "<id>; <catalog oid> <oid> TABLE <schema> <name> <owner>";
# TABLE DATA and TABLE ATTACH entries belong to tables counted already.
expected=$(awk '$4 == "TABLE" && $5 != "DATA" && $5 != "ATTACH" { count++ } END { print count + 0 }' "$work/toc")
[ "$expected" -gt 0 ] || die "$file lists no tables"

log "creating $scratch"
psql_run postgres "CREATE DATABASE \"$scratch\" OWNER \"$OPENBOX_PG_USER\"" >/dev/null
created=1
log "restoring $(wc -c <"$file" | tr -d ' ') bytes into $scratch"
compose exec -T postgres pg_restore -U "$OPENBOX_PG_USER" -d "$scratch" --no-owner --no-privileges --exit-on-error <"$file" ||
  die "pg_restore into $scratch failed"
restored=$(psql_scalar "$scratch" "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_toast%'")
size=$(psql_scalar postgres "SELECT pg_size_pretty(pg_database_size('$scratch'))")
log "report: $restored of $expected tables restored into $scratch ($size)"
[ "$restored" = "$expected" ] || die "the restored database has $restored tables, the dump lists $expected"
log "restore check passed"
