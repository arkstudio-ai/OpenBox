#!/usr/bin/env bash
# Drill (SPEC §12): stop the trajectory worker and prove that recording is decoupled from the
# business. While the worker is stopped the backend must pass every health probe and the spool may
# only grow; after the restart the spool must drain without producer loss. The admin trajectory UI is
# unavailable while the worker is stopped. Interrupting the drill starts the worker again.
# Dry run unless --execute.
#
#   drill-worker-stop.sh [--minutes 5] [--drain-timeout 900] [--execute]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

minutes=5
drain_timeout=900
while [ $# -gt 0 ]; do
  case "$1" in
    --minutes)
      need_value "$1" $#
      minutes=$2
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
if [ "$EXECUTE" != 1 ]; then
  log "dry run: would stop trajectory-worker for $minutes min, probe the backend every 10 s, start the worker and wait up to $drain_timeout s for the spool to drain; re-run with --execute"
  exit 0
fi

require_overlay
wait_healthy trajectory-worker 60 || die "trajectory-worker is not healthy; fix it before the drill"

start_worker() {
  log "starting trajectory-worker"
  compose up -d --no-deps trajectory-worker >/dev/null ||
    warn "could not start trajectory-worker; run: cd $OPENBOX_DIR && docker compose up -d --no-deps trajectory-worker"
}
trap start_worker EXIT

log "stopping trajectory-worker for $minutes min"
compose stop trajectory-worker >/dev/null
probes=0
failures=0
peak_bytes=0
end=$(($(date +%s) + minutes * 60))
while [ "$(date +%s)" -lt "$end" ]; do
  probes=$((probes + 1))
  if ! backend_healthy; then
    failures=$((failures + 1))
    warn "backend health probe failed"
  fi
  if stats=$(spool_stats); then
    read -r bytes files closed age _ <<<"$stats"
    if [ "$bytes" -gt "$peak_bytes" ]; then
      peak_bytes=$bytes
    fi
    log "spool: $bytes bytes in $files file(s), $closed closed, oldest $age s"
  fi
  sleep 10
done

trap - EXIT
start_worker
restarted=$(date +%s)
wait_healthy trajectory-worker 180 || die "trajectory-worker did not become healthy after the restart"
wait_drained "$drain_timeout" || die "the spool did not drain within $drain_timeout s"
drain_seconds=$(($(date +%s) - restarted))
gaps=$(worker_value gaps_recorded)
loss=$(worker_value producer_loss_events)
log "report: backend probes failed $failures of $probes; spool peak $peak_bytes bytes; drained $drain_seconds s after the restart; since the restart gaps_recorded=${gaps:-unknown} producer_loss_events=${loss:-unknown}"
[ "$failures" = 0 ] || die "the backend failed health probes while the worker was stopped"
case "${loss:-0}" in
  0 | 0.0) ;;
  *) die "the worker reported producer loss after the restart" ;;
esac
log "drill passed"
