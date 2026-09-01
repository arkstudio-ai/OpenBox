#!/bin/bash
# Run the local development backend or tunnel against the isolated WUYING
# desktop without changing backend/.env (the production profile).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEV_ENV="${WUYING_DEV_ENV_FILE:-$BACKEND_DIR/.env.wuying-dev}"
# Keep the dev launcher aligned with frontend-v2's /api and /ws proxy.  A
# different port remains available as an explicit override for side-by-side
# diagnostics, but the supported acceptance path is one dev backend on 8080.
BACKEND_PORT="${OPENBOX_DEV_BACKEND_PORT:-8080}"

if [ ! -f "$DEV_ENV" ]; then
  echo "error: development WUYING profile not found: $DEV_ENV" >&2
  exit 1
fi

# Make the development profile explicit for every Python entrypoint. The
# shared acceptance desktop is intentionally trusted single-user until dynamic
# one-user/one-desktop provisioning exists, so never inherit a production JWT
# secret from the shell or backend/.env.
export OPENBOX_ENV_FILE="$DEV_ENV"
export JWT_SECRET=""
if [ -f "$BACKEND_DIR/.env" ]; then
  # Reuse the user's model/database/storage configuration.  The selected dev
  # profile still overrides every WUYING value, and the explicit empty JWT
  # keeps this shared acceptance desktop in trusted single-user mode.
  export OPENBOX_BASE_ENV_FILE="$BACKEND_DIR/.env"
fi

while IFS='=' read -r key value; do
  case "$key" in
    SANDBOX_PROVIDER|WUYING_ENDPOINT|WUYING_API_KEY|WUYING_DESKTOP_ID|WUYING_RELAY|WUYING_TUNNEL_PORT|WUYING_TUNNEL_KEY|WUYING_REGION_ID|WUYING_END_USER_ID)
      export "$key=$value"
      ;;
  esac
done < <(grep -E '^[[:space:]]*(SANDBOX_PROVIDER|WUYING_[A-Z0-9_]+)=' "$DEV_ENV" | sed 's/^[[:space:]]*//')

mode="${1:-backend}"
if [ "$#" -gt 0 ]; then
  shift
fi

case "$mode" in
  tunnel)
    WUYING_ENV_FILE="$DEV_ENV" exec "$SCRIPT_DIR/wuying_tunnel.sh" "$@"
    ;;
  backend)
    cd "$BACKEND_DIR"
    exec uv run python scripts/backend_entrypoint.py --reload --host 0.0.0.0 --port "$BACKEND_PORT" "$@"
    ;;
  *)
    echo "usage: $0 {tunnel|backend} [arguments...]" >&2
    exit 2
    ;;
esac
