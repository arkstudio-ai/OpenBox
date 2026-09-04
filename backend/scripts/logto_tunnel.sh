#!/bin/bash
# Opens a local tunnel to the self-hosted Logto admin console.
#
#   browser -> localhost:3002 -> gw 127.0.0.1:3012 (docker) -> logto admin :3002
#           -> localhost:3011 -> gw 127.0.0.1:3011 (docker) -> logto core  :3001
#
# The Logto admin console is pinned to ADMIN_ENDPOINT=http://localhost:3002, so
# it ONLY works when reached at localhost:3002 — hence the local port must be
# 3002, not the host's 3012. The public domain (auth.bossipai.com.cn) serves the
# OIDC/sign-in flows but NOT the admin console.
#
# Open http://localhost:3002/console after this is up.  See docs/LOGTO_PROD.md.
set -euo pipefail

# Override any of these via the environment.
HOST="${LOGTO_TUNNEL_HOST:-root@47.116.181.123}"   # bossip-gw-1
KEY="${LOGTO_TUNNEL_KEY:-$HOME/.ssh/id_ed25519}"

if [ ! -f "$KEY" ]; then
  echo "error: ssh key not found at $KEY (set LOGTO_TUNNEL_KEY)" >&2
  exit 1
fi

if lsof -nP -iTCP:3002 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port 3002 already listening — tunnel appears to be up already"
  exit 0
fi

echo "tunnel up: http://localhost:3002/console (Admin Console) + localhost:3011 (OIDC) -> $HOST"
echo "(ctrl-c to stop)"
while true; do
  ssh -N -T \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
    -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 \
    -i "$KEY" \
    -L 127.0.0.1:3002:127.0.0.1:3012 \
    -L 127.0.0.1:3011:127.0.0.1:3011 \
    "$HOST" || true
  echo "[$(date +%H:%M:%S)] tunnel dropped, reconnecting in 5s" >&2
  sleep 5
done
