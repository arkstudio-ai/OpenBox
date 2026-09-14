#!/usr/bin/env bash
# Drill (SPEC §8.11, §12): delete a session and prove that its trajectory content is gone. The internal
# test session --session-id is deleted through the business API as its owner (bearer token read from
# --user-token-file). The drill then waits until the worker reports the tombstone, which the admin API
# (bearer token from --admin-token-file) shows by answering 404 or 410 for the session, and until
# `python -m trajectory.ops.deletion verify` in the worker image finds the trajectory tombstoned, no
# garbage collection pending for its prefix and no object left under it in the trajectory blob store.
# Nothing is deleted unless the admin API shows the session first and `trajectory.ops.deletion precheck`
# finds stored objects under its prefix (use a session idle for more than 5 minutes, whose events are
# archived). The deletion cannot be undone. The tokens reach curl on stdin, never as arguments.
# Dry run unless --execute.
#
#   drill-delete-session.sh --session-id ID --user-token-file FILE --admin-token-file FILE
#                           [--workspace-id ID] [--api-url http://127.0.0.1:8080]
#                           [--admin-url http://127.0.0.1] [--timeout 900] [--execute]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

session_id=
workspace_id=
user_token_file=
admin_token_file=
api_url=http://127.0.0.1:8080
admin_url=http://127.0.0.1
timeout=900
while [ $# -gt 0 ]; do
  case "$1" in
    --session-id)
      need_value "$1" $#
      session_id=$2
      shift 2
      ;;
    --workspace-id)
      need_value "$1" $#
      workspace_id=$2
      shift 2
      ;;
    --user-token-file)
      need_value "$1" $#
      user_token_file=$2
      shift 2
      ;;
    --admin-token-file)
      need_value "$1" $#
      admin_token_file=$2
      shift 2
      ;;
    --api-url)
      need_value "$1" $#
      api_url=$2
      shift 2
      ;;
    --admin-url)
      need_value "$1" $#
      admin_url=$2
      shift 2
      ;;
    --timeout)
      need_value "$1" $#
      timeout=$2
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
[[ $session_id =~ ^[A-Za-z0-9_-]{1,128}$ ]] || die "--session-id is required: letters, digits, '_' or '-'"
if [ -n "$workspace_id" ]; then
  [[ $workspace_id =~ ^[A-Za-z0-9_-]{1,128}$ ]] || die "invalid --workspace-id: $workspace_id"
fi
for url in "$api_url" "$admin_url"; do
  [[ $url =~ ^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] || die "URLs are scheme://host[:port] without a path: $url"
done
positive_integer "$timeout" || die "--timeout must be a positive integer"
[ -n "$user_token_file" ] && [ -n "$admin_token_file" ] || die "--user-token-file and --admin-token-file are required"
for token_file in "$user_token_file" "$admin_token_file"; do
  [ -r "$token_file" ] && [ -s "$token_file" ] || die "cannot read a token from $token_file"
done
if [ "$EXECUTE" != 1 ]; then
  log "dry run: would check that $session_id has a trajectory with stored objects, delete it with DELETE $api_url/api/agent/session/$session_id, then wait up to $timeout s for $admin_url/api/admin/trajectories/sessions/$session_id to answer 404 or 410 and for no object to remain under its trajectory prefix; re-run with --execute"
  exit 0
fi

# read_token FILE: the first line of FILE without whitespace, read with builtins only.
read_token() {
  local token=
  IFS= read -r token <"$1" || true
  token=${token//[[:space:]]/}
  [[ $token =~ ^[A-Za-z0-9._~+/=-]+$ ]] || die "$1 does not hold a bearer token"
  printf '%s' "$token"
}

require_overlay
wait_healthy trajectory-worker 60 || die "trajectory-worker is not healthy; fix it before the drill"
user_token=$(read_token "$user_token_file")
admin_token=$(read_token "$admin_token_file")
work=$(mktemp -d "${TMPDIR:-/tmp}/openbox-drill-delete-session.XXXXXX")
trap 'rm -rf "$work"' EXIT

# request METHOD URL TOKEN [HEADER]: prints the HTTP status (000 when there is no answer); the body is
# kept in $work/body. The authorization header reaches curl on stdin.
request() {
  local method=$1 url=$2 token=$3 header=${4:-} code
  code=$(
    {
      printf 'Authorization: Bearer %s\n' "$token"
      if [ -n "$header" ]; then
        printf '%s\n' "$header"
      fi
    } | curl -sS --max-time 30 -o "$work/body" -w '%{http_code}' -X "$method" -H @- "$url"
  ) || true
  printf '%s' "${code:-000}"
}
admin_status() {
  request GET "$admin_url/api/admin/trajectories/sessions/$session_id" "$admin_token"
}

code=$(admin_status)
[ "$code" = 200 ] || die "the admin API answers HTTP $code for $session_id (expected 200 before the deletion); nothing was deleted"
worker_python -m trajectory.ops.deletion precheck --session-id "$session_id" ||
  die "the trajectory of $session_id cannot show the deletion (see above); nothing was deleted"

log "deleting session $session_id through the business API"
code=$(request DELETE "$api_url/api/agent/session/$session_id" "$user_token" ${workspace_id:+"X-Workspace-Id: $workspace_id"})
[ "$code" = 200 ] || die "the business API answered HTTP $code to the deletion: $(head -c 300 "$work/body" 2>/dev/null || true)"
deleted_at=$(date +%s)
deadline=$((deleted_at + timeout))

while :; do
  code=$(admin_status)
  case "$code" in
    404 | 410) break ;;
  esac
  [ "$(date +%s)" -lt "$deadline" ] ||
    die "the admin API still answers HTTP $code for $session_id after $timeout s: the worker has not applied the tombstone"
  log "admin API answers HTTP $code; waiting for the tombstone"
  sleep 10
done
log "admin API answers HTTP $code $(($(date +%s) - deleted_at)) s after the deletion"

remaining=$((deadline - $(date +%s)))
if [ "$remaining" -lt 1 ]; then
  remaining=1
fi
worker_python -m trajectory.ops.deletion verify --session-id "$session_id" --timeout "$remaining" ||
  die "the trajectory of $session_id is not fully deleted: see the state above (tombstone, pending garbage collection, objects under its prefix)"
log "report: session $session_id deleted; admin API HTTP $code; trajectory tombstoned with no object under its prefix $(($(date +%s) - deleted_at)) s after the deletion"
log "drill passed"
