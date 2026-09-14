#!/usr/bin/env bash
# Drill (SPEC §12): exhausted spool budget. The backend is recreated with a tiny
# TRAJECTORY_SPOOL_MAX_BYTES (default 1) for --minutes, so the emitter drops new events with reason
# spool_full while business requests keep working; the backend is then recreated with its normal
# configuration and the drops appear as recording.gap events. Recreating the backend interrupts
# running agent loops, so both recreations wait (up to --idle-wait seconds) until no run holds an
# execution lease. Use the product with an internal test account during the window.
# Dry run unless --execute.
#
#   drill-spool-full.sh [--minutes 3] [--max-bytes 1] [--idle-wait 600] [--execute]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

minutes=3
max_bytes=1
idle_wait=600
while [ $# -gt 0 ]; do
  case "$1" in
    --minutes)
      need_value "$1" $#
      minutes=$2
      shift 2
      ;;
    --max-bytes)
      need_value "$1" $#
      max_bytes=$2
      shift 2
      ;;
    --idle-wait)
      need_value "$1" $#
      idle_wait=$2
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
positive_integer "$max_bytes" || die "--max-bytes must be a positive integer"
[[ $idle_wait =~ ^[0-9]{1,6}$ ]] || die "--idle-wait must be a number of seconds"
if [ "$EXECUTE" != 1 ]; then
  log "dry run: would wait until no run is active, recreate backend with TRAJECTORY_SPOOL_MAX_BYTES=$max_bytes for $minutes min, then recreate it with its normal configuration and report recording gaps; re-run with --execute"
  exit 0
fi

require_overlay
wait_for_idle "$idle_wait" || die "runs are still active; nothing was changed, retry later"
gaps_before=$(worker_value gaps_recorded)
override=$(mktemp "${TMPDIR:-/tmp}/openbox-drill-spool-full.XXXXXX")
cat >"$override" <<EOF
services:
  backend:
    environment:
      TRAJECTORY_SPOOL_MAX_BYTES: "$max_bytes"
EOF

restore_backend() {
  rm -f "$override"
  if ! wait_for_idle "$idle_wait"; then
    warn "runs are active: backend still runs with TRAJECTORY_SPOOL_MAX_BYTES=$max_bytes; when idle run: cd $OPENBOX_DIR && docker compose up -d --no-deps backend"
    return 1
  fi
  log "recreating backend with its normal configuration"
  compose up -d --no-deps backend >/dev/null && wait_healthy backend 180
}
trap restore_backend EXIT

log "recreating backend with TRAJECTORY_SPOOL_MAX_BYTES=$max_bytes"
compose_with_override "$override" up -d --no-deps backend >/dev/null
wait_healthy backend 180 || die "backend did not become healthy with the drill configuration"
log "the spool budget is exhausted for $minutes min; use the product with an internal test account now"
probes=0
failures=0
end=$(($(date +%s) + minutes * 60))
while [ "$(date +%s)" -lt "$end" ]; do
  probes=$((probes + 1))
  if ! backend_healthy; then
    failures=$((failures + 1))
    warn "backend health probe failed"
  fi
  sleep 10
done

trap - EXIT
restore_backend || die "the backend was not restored (see above)"
log "waiting 60 s for the gap controls to be ingested"
sleep 60
gaps_after=$(worker_value gaps_recorded)
log "report: backend probes failed $failures of $probes; gaps_recorded ${gaps_before:-unknown} -> ${gaps_after:-unknown}"
[ "$failures" = 0 ] || die "the backend failed health probes while the spool was full"
if [ -n "$gaps_before" ] && [ "$gaps_before" = "$gaps_after" ]; then
  warn "no recording gap was reported: was there any traffic during the window?"
fi
log "drill finished"
