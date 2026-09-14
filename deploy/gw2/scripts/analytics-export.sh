#!/usr/bin/env bash
# Daily trajectory analytics export (SPEC §12, analytics CLI contract): runs
# `python -m trajectory.analytics export --date <day>` in the running trajectory-worker container, for
# yesterday in Asia/Shanghai unless --date is given. The export writes
# analytics/trajectories/dt=<day>/ through the trajectory blob store and replaces that day on a rerun.
# Afterwards analytics_export_failed is reported through `python -m trajectory.ops.cms put` (a one-off
# worker container when the worker is stopped): 1 when the worker was not healthy within --wait seconds
# or the export exited non-zero, and the script then exits non-zero; 0 after a successful export, which
# clears the analytics-export alarm.
#
#   analytics-export.sh [--date YYYY-MM-DD] [--instance gw2] [--wait 600] [--dry-run]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

instance=${OPENBOX_CMS_INSTANCE:-gw2}
day=
wait_seconds=600
dry_run=
while [ $# -gt 0 ]; do
  case "$1" in
    --date)
      need_value "$1" $#
      day=$2
      shift 2
      ;;
    --instance)
      need_value "$1" $#
      instance=$2
      shift 2
      ;;
    --wait)
      need_value "$1" $#
      wait_seconds=$2
      shift 2
      ;;
    --dry-run)
      dry_run=--dry-run
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
[[ $wait_seconds =~ ^[0-9]{1,5}$ ]] || die "--wait must be a number of seconds"
if [ -z "$day" ]; then
  # GNU date on the servers, BSD date on developer machines.
  day=$(TZ=Asia/Shanghai date -d yesterday +%Y-%m-%d 2>/dev/null || TZ=Asia/Shanghai date -v-1d +%Y-%m-%d)
fi
[[ $day =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "--date must look like 2026-09-14"

take_lock analytics
require_overlay

status=0
if ! wait_healthy trajectory-worker "$wait_seconds"; then
  warn "trajectory-worker is not healthy; the analytics export for $day did not run"
  status=1
else
  log "exporting trajectory analytics for $day${dry_run:+ (dry run)}"
  compose exec -T trajectory-worker python -m trajectory.analytics export --date "$day" ${dry_run:+"$dry_run"} || status=$?
fi

failed=0
if [ "$status" != 0 ]; then
  failed=1
fi
worker_python -m trajectory.ops.cms put --instance "$instance" --metric "analytics_export_failed=$failed" ${dry_run:+"$dry_run"} ||
  warn "could not report analytics_export_failed=$failed to CloudMonitor"
if [ "$status" != 0 ]; then
  log "ERROR: the analytics export for $day failed (exit $status)"
  exit "$status"
fi
log "analytics export for $day complete"
