#!/usr/bin/env bash
# Install and enable the operations timers (SPEC §12): trajectory metrics every minute, PostgreSQL
# backups daily at 03:30 Asia/Shanghai, Docker image prune weekly. Idempotent. The units run the
# scripts from /opt/openbox/deploy/gw2, so copy the deploy/gw2 directory there first. --instance sets
# the CloudMonitor instance dimension of the metrics through a drop-in of the metrics service: keep
# the default gw2 on the production host and give every other host (the AWS development host) its
# own name, or its metrics raise the gw2 alarms.
#
#   install-timers.sh [--instance gw2] [--uninstall] [--dry-run]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

UNIT_DIR=${SYSTEMD_UNIT_DIR:-/etc/systemd/system}
INSTALL_DIR=/opt/openbox/deploy/gw2
TIMERS="openbox-trajectory-metrics.timer openbox-pg-backup.timer openbox-prune-images.timer"
DROPIN_DIR=$UNIT_DIR/openbox-trajectory-metrics.service.d
instance=${OPENBOX_CMS_INSTANCE:-gw2}
uninstall=0
dry_run=0
while [ $# -gt 0 ]; do
  case "$1" in
    --instance)
      need_value "$1" $#
      instance=$2
      shift 2
      ;;
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
[[ $instance =~ ^[A-Za-z0-9._-]{1,64}$ ]] || die "invalid --instance: $instance"

step() {
  if [ "$dry_run" = 1 ]; then
    log "dry-run: $*"
  else
    log "+ $*"
    "$@"
  fi
}

# write_file PATH TEXT: PATH (mode 0644) holding TEXT, replaced atomically.
write_file() {
  if [ "$dry_run" = 1 ]; then
    log "dry-run: write $1: $2"
  else
    log "+ write $1"
    printf '%s\n' "$2" >"$1.tmp"
    chmod 0644 "$1.tmp"
    mv "$1.tmp" "$1"
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
  step rm -rf "$DROPIN_DIR"
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
log "metrics instance: $instance"
step install -d -m 0755 "$DROPIN_DIR"
write_file "$DROPIN_DIR/instance.conf" "$(printf '[Service]\nEnvironment=OPENBOX_CMS_INSTANCE=%s' "$instance")"
step systemctl daemon-reload
# shellcheck disable=SC2086 # one argument per timer
step systemctl enable --now $TIMERS
if [ "$dry_run" = 0 ]; then
  systemctl list-timers 'openbox-*' --no-pager
fi
