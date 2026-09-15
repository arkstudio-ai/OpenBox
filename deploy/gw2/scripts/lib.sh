#!/usr/bin/env bash
# Shared helpers of the gw2 trajectory operations scripts (RUNBOOK.md); sourced, not executed.
# Kept compatible with bash 3.2 so argument handling and dry runs can be tested on developer
# machines; the servers run bash 4.4 or newer and GNU coreutils/findutils.
set -euo pipefail

OPENBOX_DIR=${OPENBOX_DIR:-/opt/openbox}
OPENBOX_PG_USER=${OPENBOX_PG_USER:-openbox}
OPENBOX_BUSINESS_DB=${OPENBOX_BUSINESS_DB:-openbox}
OPENBOX_TRACE_DB=${OPENBOX_TRACE_DB:-openbox_trace}
# Login role of the trajectory worker (TRAJECTORY_DATABASE_URL in docker-compose.trajectory.yml).
OPENBOX_TRACE_ROLE=${OPENBOX_TRACE_ROLE:-openbox_trace}
SPOOL_MOUNT=/var/lib/openbox/trajectory-spool
DEPLOY_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
EXECUTE=0

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
warn() { log "WARNING: $*"; }
die() {
  log "ERROR: $*"
  exit 1
}

# Print the comment block at the top of the calling script.
usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; }

need_value() { [ "$2" -ge 2 ] || die "$1 needs a value"; }
valid_identifier() { [[ ${1:-} =~ ^[a-z_][a-z0-9_]{0,62}$ ]]; }
positive_integer() { [[ ${1:-} =~ ^[1-9][0-9]{0,8}$ ]]; }
# number_greater A B: 0 when the number A is greater than the number B (worker metrics print as 3 or 3.0).
number_greater() { awk -v a="${1:-}" -v b="${2:-}" 'BEGIN { exit !(a + 0 > b + 0) }'; }

# These names are quoted into SQL identifiers.
for identifier in "$OPENBOX_PG_USER" "$OPENBOX_BUSINESS_DB" "$OPENBOX_TRACE_DB" "$OPENBOX_TRACE_ROLE"; do
  valid_identifier "$identifier" || die "invalid database identifier in the environment: $identifier"
done

compose() { (cd "$OPENBOX_DIR" && docker compose "$@"); }

# env_value NAME: the last NAME= assignment in $OPENBOX_DIR/.env, without surrounding quotes.
env_value() {
  local line
  line=$(grep -E "^$1=" "$OPENBOX_DIR/.env" 2>/dev/null | tail -n 1) || line=
  line=${line#*=}
  line=${line#\"}
  line=${line%\"}
  line=${line#\'}
  line=${line%\'}
  printf '%s' "$line"
}

compose_files() {
  local files=${COMPOSE_FILE:-}
  if [ -z "$files" ]; then
    files=$(env_value COMPOSE_FILE)
  fi
  [ -n "$files" ] || die "COMPOSE_FILE is not set in $OPENBOX_DIR/.env (RUNBOOK.md, Compose files)"
  printf '%s' "$files"
}

# compose_with_override FILE ARGS...: docker compose with FILE added after the project's files.
compose_with_override() {
  local override=$1 files
  shift
  files=$(compose_files)
  (cd "$OPENBOX_DIR" && COMPOSE_FILE="$files:$override" docker compose "$@")
}

require_overlay() {
  local services
  services=$(compose config --services) || die "docker compose config failed in $OPENBOX_DIR"
  case $'\n'"$services"$'\n' in
    *$'\n'trajectory-worker$'\n'*) ;;
    *) die "trajectory-worker is not part of the compose project in $OPENBOX_DIR; set COMPOSE_FILE in .env (RUNBOOK.md)" ;;
  esac
}

# take_lock NAME: exit quietly while another run holds the lock (timers may overlap).
take_lock() {
  local file="${OPENBOX_LOCK_DIR:-/run/lock}/openbox-$1.lock"
  if ! command -v flock >/dev/null 2>&1; then
    warn "flock not found, running without a lock"
    return 0
  fi
  exec 9>"$file" || die "cannot open $file"
  if ! flock -n 9; then
    log "another $1 run is in progress"
    exit 0
  fi
}

container_id() {
  local ids
  ids=$(compose ps -q "$1" 2>/dev/null) || ids=
  printf '%s' "${ids%%$'\n'*}"
}

service_running() {
  local id
  id=$(container_id "$1")
  [ -n "$id" ] && [ "$(docker inspect -f '{{.State.Running}}' "$id" 2>/dev/null)" = true ]
}

# wait_healthy SERVICE [SECONDS]: 0 once healthy, or running when the service has no healthcheck.
wait_healthy() {
  local service=$1 deadline state id
  deadline=$(($(date +%s) + ${2:-180}))
  while :; do
    state=
    id=$(container_id "$service")
    if [ -n "$id" ]; then
      state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}-no-healthcheck{{end}}' "$id" 2>/dev/null) || state=
    fi
    case "$state" in
      healthy | running-no-healthcheck) return 0 ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then
      warn "$service is not healthy (state: ${state:-no container})"
      return 1
    fi
    sleep 5
  done
}

psql_run() { compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U "$OPENBOX_PG_USER" -d "$1" -c "$2"; }
psql_scalar() { compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U "$OPENBOX_PG_USER" -d "$1" -tAc "$2"; }
# psql_stdin DATABASE: runs the SQL read from stdin, so values such as passwords stay out of every
# process's arguments (docker passes stdin through its API stream).
psql_stdin() { compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U "$OPENBOX_PG_USER" -d "$1"; }

# trace_db_password: OPENBOX_TRACE_DB_PASSWORD from $OPENBOX_DIR/.env. It is embedded in
# TRAJECTORY_DATABASE_URL and quoted into SQL, so only URL- and SQL-safe characters are accepted.
trace_db_password() {
  local password
  password=$(env_value OPENBOX_TRACE_DB_PASSWORD)
  [ -n "$password" ] || die "OPENBOX_TRACE_DB_PASSWORD is not set in $OPENBOX_DIR/.env (generate one with: openssl rand -hex 32)"
  [[ $password =~ ^[A-Za-z0-9._~-]{16,128}$ ]] ||
    die "OPENBOX_TRACE_DB_PASSWORD in $OPENBOX_DIR/.env must be 16 to 128 letters, digits, '.', '_', '~' or '-'"
  printf '%s' "$password"
}

# database_exists NAME: 0 when the database exists, 1 when it does not, 2 when PostgreSQL cannot be
# queried. A failed query must never read as a missing database (backups would be skipped silently).
database_exists() {
  local found
  valid_identifier "$1" || return 2
  found=$(psql_scalar postgres "SELECT 1 FROM pg_database WHERE datname = '$1'") || return 2
  [ "$found" = 1 ]
}

# Runs holding a live execution lease (the release gate of docs/DEPLOY.md).
active_runs() {
  psql_scalar "$OPENBOX_BUSINESS_DB" "SELECT count(*) FROM session_executions WHERE run_id IS NOT NULL AND lease_until > now()"
}

# wait_for_idle SECONDS: 0 when no run holds a lease, checking every 30 s until SECONDS pass.
wait_for_idle() {
  local deadline count
  deadline=$(($(date +%s) + ${1:-0}))
  while :; do
    count=$(active_runs) || return 1
    if [ "$count" = 0 ]; then
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      warn "$count run(s) hold an execution lease"
      return 1
    fi
    log "$count run(s) hold an execution lease, checking again in 30 s"
    sleep 30
  done
}

# worker_python ARGS...: python in the worker image with the worker's environment, inside the
# running worker or, while it is stopped, in a one-off container of the same service.
worker_python() {
  if service_running trajectory-worker; then
    compose exec -T trajectory-worker python "$@"
  else
    compose run --rm --no-deps -T --entrypoint python trajectory-worker "$@"
  fi
}

backend_healthy() {
  compose exec -T backend curl -fsS -o /dev/null --max-time 5 http://127.0.0.1:8080/health >/dev/null 2>&1
}

# worker_value NAME: a number from the worker's /metrics (counters, gauges or flat), empty if unavailable.
worker_value() {
  compose exec -T trajectory-worker python -c '
import json, sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    body = json.load(opener.open("http://127.0.0.1:8090/metrics", timeout=5))
except Exception:
    sys.exit(0)
for section in (body.get("counters"), body.get("gauges"), body):
    value = section.get(sys.argv[1]) if isinstance(section, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        print(value)
        break
' "$1" 2>/dev/null || true
}

# Host directory of the trajectory-spool volume, read from the backend's or the worker's mounts.
spool_host_dir() {
  local service id dir
  for service in backend trajectory-worker; do
    id=$(container_id "$service")
    if [ -z "$id" ]; then
      continue
    fi
    dir=$(docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$SPOOL_MOUNT\"}}{{.Source}}{{end}}{{end}}" "$id" 2>/dev/null) || dir=
    if [ -n "$dir" ]; then
      printf '%s' "$dir"
      return 0
    fi
  done
  return 1
}

# spool_stats: "BYTES FILES CLOSED OLDEST_AGE_SECONDS QUARANTINED". BYTES is the allocated size of every regular file
# under producers/, blobs/ and quarantine/, as the emitter counts it against TRAJECTORY_SPOOL_MAX_BYTES; FILES, CLOSED
# and the age are about the non-empty producer data files; QUARANTINED counts data files, not their .reason sidecars
# or the blobs kept for them (<name>.blob-<sha256>).
spool_stats() {
  local dir now
  dir=$(spool_host_dir) || return 1
  now=$(date +%s)
  # The starting point (%H) comes last: the volume path may contain spaces, spool file names do not.
  { find "$dir/producers" "$dir/blobs" "$dir/quarantine" -type f -printf '%s %b %T@ %f %H\n' 2>/dev/null || true; } |
    awk -v now="$now" '
      { bytes += ($2 * 512 > $1) ? $2 * 512 : $1 }
      $NF ~ /\/producers$/ && $1 > 0 && $4 ~ /\.jsonl(\.part)?$/ {
        files++; if ($4 ~ /\.jsonl$/) closed++; if (oldest == "" || $3 < oldest) oldest = $3
      }
      $NF ~ /\/quarantine$/ && $4 !~ /^\./ && $4 !~ /\.reason$/ && $4 !~ /\.blob-/ { quarantined++ }
      END {
        age = (oldest == "" || now < oldest) ? 0 : now - oldest
        printf "%.0f %d %d %.0f %d\n", bytes, files, closed, age, quarantined
      }'
}

# wait_drained SECONDS: 0 once no closed spool file waits and the oldest open file is fresh.
wait_drained() {
  local deadline stats bytes files closed age
  deadline=$(($(date +%s) + $1))
  while :; do
    if stats=$(spool_stats); then
      read -r bytes files closed age _ <<<"$stats"
      if [ "$closed" = 0 ] && [ "$age" -lt 10 ]; then
        return 0
      fi
      log "draining: $closed closed of $files file(s), $bytes bytes, oldest $age s"
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      return 1
    fi
    sleep 10
  done
}
