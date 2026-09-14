#!/usr/bin/env bash
# Install and enable the gw2 operations timers (SPEC §12): trajectory metrics every minute,
# PostgreSQL backups daily at 03:30 Asia/Shanghai, Docker image prune weekly. Idempotent. The units
# run the scripts from /opt/openbox/deploy/gw2, so copy the deploy/gw2 directory there first.
#
#   install-timers.sh [--uninstall] [--dry-run]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

UNIT_DIR=${SYSTEMD_UNIT_DIR:-/etc/systemd/system}
INSTALL_DIR=/opt/openbox/deploy/gw2
TIMERS="openbox-trajectory-metrics.timer openbox-pg-backup.timer openbox-prune-images.timer"
uninstall=0
dry_run=0
while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall)
      uninstall=1
      shift
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

step() {
  if [ "$dry_run" = 1 ]; then
    log "dry-run: $*"
  else
    log "+ $*"
    "$@"
  fi
}

if [ "$dry_run" = 0 ]; then
  [ "$(id -u)" = 0 ] || die "run as root"
  command -v systemctl >/dev/null 2>&1 || die "systemctl is required"
fi

if [ "$uninstall" = 1 ]; then
  # shellcheck disable=SC2086 # one argument per timer
  step systemctl disable --now $TIMERS
  for timer in $TIMERS; do
    step rm -f "$UNIT_DIR/$timer" "$UNIT_DIR/${timer%.timer}.service"
  done
  step systemctl daemon-reload
  exit 0
fi

if [ "$DEPLOY_DIR" != "$INSTALL_DIR" ]; then
  if [ "$dry_run" = 0 ]; then
    die "the units run scripts from $INSTALL_DIR; copy deploy/gw2 there and run its install-timers.sh (this copy: $DEPLOY_DIR)"
  fi
  warn "this copy is $DEPLOY_DIR; the units expect $INSTALL_DIR"
fi

for script in push-metrics.sh pg-backup.sh prune-images.sh; do
  if [ ! -x "$DEPLOY_DIR/scripts/$script" ]; then
    step chmod 0755 "$DEPLOY_DIR/scripts/$script"
  fi
done
if command -v systemd-analyze >/dev/null 2>&1; then
  step systemd-analyze verify "$DEPLOY_DIR"/systemd/openbox-*.service "$DEPLOY_DIR"/systemd/openbox-*.timer
fi
for timer in $TIMERS; do
  for unit in "$timer" "${timer%.timer}.service"; do
    step install -m 0644 "$DEPLOY_DIR/systemd/$unit" "$UNIT_DIR/$unit"
  done
done
step systemctl daemon-reload
# shellcheck disable=SC2086 # one argument per timer
step systemctl enable --now $TIMERS
if [ "$dry_run" = 0 ]; then
  systemctl list-timers 'openbox-*' --no-pager
fi
