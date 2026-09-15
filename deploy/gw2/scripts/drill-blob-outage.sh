#!/usr/bin/env bash
# Drill (SPEC §12): object storage outage. The worker is recreated with TRAJECTORY_BLOB_FAULT
# (default put:1.0, every blob upload fails) for --minutes (default 30): ingest must retry with backoff while the
# backend passes every health probe. The worker is then recreated without the fault and the spool
# must drain. Content that could not be stored for 10 attempts is recorded as blob_store_unavailable
# with a gap, so run the drill against internal test traffic; without a failed blob upload during the
# fault the drill cannot pass. Interrupting the drill recreates the worker without the fault.
# Dry run unless --execute.
#
#   drill-blob-outage.sh [--minutes 30] [--fault put:1.0] [--drain-timeout 1800] [--execute]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

minutes=30
fault=put:1.0
drain_timeout=1800
while [ $# -gt 0 ]; do
  case "$1" in
    --minutes)
      need_value "$1" $#
      minutes=$2
      shift 2
      ;;
    --fault)
      need_value "$1" $#
      fault=$2
      shift 2
      ;;
    --drain-timeout)
      need_value "$1" $#
      drain_timeout=$2
      shift 2
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
positive_integer "$minutes" || die "--minutes must be a positive integer"
positive_integer "$drain_timeout" || die "--drain-timeout must be a positive integer"
[[ $fault =~ ^[a-z_]+:[0-9]+(\.[0-9]+)?$ ]] || die "--fault must look like put:1.0"
if [ "$EXECUTE" != 1 ]; then
  log "dry run: would recreate trajectory-worker with TRAJECTORY_BLOB_FAULT=$fault for $minutes min, probe the backend, recreate the worker without the fault and wait up to $drain_timeout s for the spool to drain; re-run with --execute"
  exit 0
fi

require_overlay
wait_healthy trajectory-worker 60 || die "trajectory-worker is not healthy; fix it before the drill"
override=$(mktemp "${TMPDIR:-/tmp}/openbox-drill-blob-outage.XXXXXX")
cat >"$override" <<EOF
services:
  trajectory-worker:
    environment:
      TRAJECTORY_BLOB_FAULT: "$fault"
EOF

restore_worker() {
  rm -f "$override"
  log "recreating trajectory-worker without TRAJECTORY_BLOB_FAULT"
  compose up -d --no-deps trajectory-worker >/dev/null ||
    warn "could not recreate trajectory-worker; run: cd $OPENBOX_DIR && docker compose up -d --no-deps trajectory-worker"
}
trap restore_worker EXIT

log "recreating trajectory-worker with TRAJECTORY_BLOB_FAULT=$fault for $minutes min"
compose_with_override "$override" up -d --no-deps trajectory-worker >/dev/null
wait_healthy trajectory-worker 180 || die "trajectory-worker did not become healthy with the fault"
probes=0
failures=0
put_failures=
end=$(($(date +%s) + minutes * 60))
while [ "$(date +%s)" -lt "$end" ]; do
  probes=$((probes + 1))
  if ! backend_healthy; then
    failures=$((failures + 1))
    warn "backend health probe failed"
  fi
  # Keep the last reading: one unreadable sample must not hide the failures counted so far.
  sample=$(worker_value blob_put_failures)
  if [ -n "$sample" ]; then
    put_failures=$sample
  fi
  lag=$(worker_value ingest_lag_seconds)
  spool=unknown
  if stats=$(spool_stats); then
    read -r bytes files _ age _ <<<"$stats"
    spool="$bytes bytes in $files file(s), oldest $age s"
  fi
  log "blob_put_failures=${sample:-unknown} ingest_lag_seconds=${lag:-unknown} spool: $spool"
  sleep 30
done

trap - EXIT
restore_worker
restarted=$(date +%s)
wait_healthy trajectory-worker 180 || die "trajectory-worker did not become healthy without the fault"
wait_drained "$drain_timeout" || die "the spool did not drain within $drain_timeout s"
gaps=$(worker_value gaps_recorded)
log "report: backend probes failed $failures of $probes; blob_put_failures during the fault=${put_failures:-unknown}; drained $(($(date +%s) - restarted)) s after the fault ended; gaps_recorded since then=${gaps:-unknown}"
[ "$failures" = 0 ] || die "the backend failed health probes during the blob outage"
[ -n "$put_failures" ] || die "blob_put_failures could not be read from the worker's /metrics during the fault: the drill is inconclusive"
number_greater "$put_failures" 0 ||
  die "no blob upload failed during the fault: repeat the drill while an internal test account records content"
log "drill passed"
