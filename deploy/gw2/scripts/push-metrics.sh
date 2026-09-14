#!/usr/bin/env bash
# Minute timer (SPEC §12): trajectory and host metrics to CloudMonitor. Host disk usage, the trajectory
# spool (bytes, files, oldest file age, quarantined files), kernel OOM kills of the last hour (all, and
# those of the backend container), the trace database size, the backend container's CPU (percent of one
# core) and memory (percent of its limit) from `docker stats --no-stream`, and the number of
# pg_stat_statements entries of the business database that mention trajectory_ but not
# legacy_trajectory_ (0 while the extension is not installed there) are measured here and piped into
# `python -m trajectory.ops.cms push` in the worker image, which adds the worker's /health and /metrics
# and reports everything. While the worker is stopped a one-off container reports worker_up=0.
# The counter samples of the cms module are kept in /var/lib/openbox-ops/cms-state.json.
#
#   push-metrics.sh [--instance gw2] [--dry-run]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

instance=${OPENBOX_CMS_INSTANCE:-gw2}
dry_run=0
while [ $# -gt 0 ]; do
  case "$1" in
    --instance)
      need_value "$1" $#
      instance=$2
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
[[ $instance =~ ^[A-Za-z0-9._-]{1,64}$ ]] || die "invalid --instance: $instance"
state_dir=${OPENBOX_OPS_STATE_DIR:-/var/lib/openbox-ops}

take_lock metrics
require_overlay
install -d -m 0700 "$state_dir"

number() {
  if [[ ${1:-} =~ ^[0-9]+(\.[0-9]+)?$ ]]; then printf '%s' "$1"; else printf 'null'; fi
}
disk_used_percent() {
  df -P "$1" 2>/dev/null | awk 'NR == 2 { sub("%", "", $5); print $5 }'
}

host_disk=$(disk_used_percent /) || host_disk=
docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null) || docker_root=
docker_disk=
if [ -n "$docker_root" ]; then
  docker_disk=$(disk_used_percent "$docker_root") || docker_disk=
fi

spool_bytes= spool_files= spool_age= quarantined=
if stats=$(spool_stats); then
  read -r spool_bytes spool_files _ spool_age quarantined <<<"$stats"
fi

oom_kills= backend_oom_kills=
if command -v journalctl >/dev/null 2>&1; then
  kernel=$(journalctl -k --since "-1h" --no-pager -o cat 2>/dev/null) || kernel=
  oom_lines=$(printf '%s\n' "$kernel" | grep 'oom-kill:') || oom_lines=
  oom_kills=0
  backend_oom_kills=0
  if [ -n "$oom_lines" ]; then
    oom_kills=$(printf '%s\n' "$oom_lines" | wc -l | tr -d ' ')
    backend=$(container_id backend)
    if [ -n "$backend" ]; then
      # Kernel oom-kill lines name the task's memory cgroup, which carries the full container id.
      backend=$(docker inspect -f '{{.Id}}' "$backend" 2>/dev/null) || backend=
    fi
    if [ -n "$backend" ]; then
      backend_oom_kills=$(printf '%s\n' "$oom_lines" | grep -c -F "$backend") || true
    fi
  fi
fi

trace_db_bytes=$(psql_scalar postgres "SELECT pg_database_size(datname) FROM pg_database WHERE datname = '$OPENBOX_TRACE_DB'" 2>/dev/null) || trace_db_bytes=

backend_cpu= backend_mem=
backend=$(container_id backend)
if [ -n "$backend" ]; then
  usage_line=$(docker stats --no-stream --format '{{.CPUPerc}} {{.MemPerc}}' "$backend" 2>/dev/null | tr -d '%') || usage_line=
  read -r backend_cpu backend_mem _ <<<"$usage_line" || true
fi

# Isolation check of the business database (RUNBOOK.md §5 step 9): the worker owns every trajectory_
# table; legacy_trajectory_* statements come from the business migration and the legacy converter.
business_statements=
extension=$(psql_scalar "$OPENBOX_BUSINESS_DB" "SELECT count(*) FROM pg_extension WHERE extname = 'pg_stat_statements'" 2>/dev/null) || extension=
if [ "$extension" = 0 ]; then
  business_statements=0
elif [ "$extension" = 1 ]; then
  business_statements=$(psql_scalar "$OPENBOX_BUSINESS_DB" "SELECT count(*) FROM pg_stat_statements s JOIN pg_database d ON d.oid = s.dbid WHERE d.datname = '$OPENBOX_BUSINESS_DB' AND s.query ILIKE '%trajectory\_%' AND s.query NOT ILIKE '%legacy\_trajectory\_%'" 2>/dev/null) || business_statements=
fi

host=$(printf '{"host_disk_used_percent":%s,"docker_disk_used_percent":%s,"spool_bytes":%s,"spool_files":%s,"spool_oldest_age_seconds":%s,"spool_quarantined_files":%s,"oom_kills_1h":%s,"backend_oom_kills_1h":%s,"trace_db_bytes":%s,"backend_cpu_percent":%s,"backend_mem_percent":%s,"business_trajectory_statements":%s}' \
  "$(number "$host_disk")" "$(number "$docker_disk")" "$(number "$spool_bytes")" "$(number "$spool_files")" \
  "$(number "$spool_age")" "$(number "$quarantined")" "$(number "$oom_kills")" "$(number "$backend_oom_kills")" \
  "$(number "$trace_db_bytes")" "$(number "$backend_cpu")" "$(number "$backend_mem")" "$(number "$business_statements")")
state_file=$state_dir/cms-state.json
state=$(cat "$state_file" 2>/dev/null) || state=
output=$(mktemp "$state_dir/cms-output.XXXXXX")
trap 'rm -f "$output" "$state_file.tmp"' EXIT

status=0
if [ "$dry_run" = 1 ]; then
  log "host metrics: $host"
  printf '%s\n%s\n' "$host" "$state" | worker_python -m trajectory.ops.cms push --instance "$instance" --dry-run >"$output" || status=$?
else
  printf '%s\n%s\n' "$host" "$state" | worker_python -m trajectory.ops.cms push --instance "$instance" >"$output" || status=$?
  # The cms module prints the next state even when the report failed, so samples are not lost.
  if [ -s "$output" ]; then
    tail -n 1 "$output" >"$state_file.tmp"
    mv "$state_file.tmp" "$state_file"
  fi
fi
exit "$status"
