#!/bin/bash
# Build the backend and frontend images from a git ref and ship them to light-test.
#
#   deploy/light-test/scripts/release.sh [ref]          # default HEAD
#   deploy/light-test/scripts/release.sh --ship <tag>   # re-run the machine side for an already built tag
#   deploy/light-test/scripts/release.sh --build-only [ref]  # build and stage only; ship later with --ship
#                                                        # (the ship restarts the backend: wait until no one is mid-turn)
#
# Steps (README「发布」): git archive → docker buildx linux/amd64 → docker save | gzip -1 →
# oss://bossip/_deploy-tmp/<tag>/ → signed links → on the machine: curl + sha256sum -c +
# docker load → swap the three image lines in docker-compose.override.yml (backup kept) →
# docker compose up -d (backend runs alembic on start) → health, alembic head, probes →
# delete the OSS staging folder. Tag: YYYYMMDD-light-<sha8>.
#
# Only build from light-mode or one of its PR branches, never from main.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(git -C "$HERE" rev-parse --show-toplevel)
BUCKET=oss://bossip/_deploy-tmp
ENDPOINT=oss-cn-shanghai.aliyuncs.com

BUILD_ONLY=
if [ "${1:-}" = "--build-only" ]; then BUILD_ONLY=1; shift; fi
if [ "${1:-}" = "--ship" ]; then
  TAG=${2:?usage: release.sh --ship <tag>}
else
  REF=${1:-HEAD}
  SHA=$(git -C "$ROOT" rev-parse --short=8 "$REF")
  TAG="$(date +%Y%m%d)-light-$SHA"
fi
WORK="${TMPDIR:-/tmp}/light-release/$TAG"
mkdir -p "$WORK/out"
echo "[release] tag $TAG  work $WORK"

if [ "${1:-}" != "--ship" ]; then
  rm -rf "$WORK/src" && mkdir -p "$WORK/src"
  git -C "$ROOT" archive "$REF" backend container frontend-v2 | tar -x -C "$WORK/src"
  echo "[release] $(date +%T) build backend"
  (cd "$WORK/src" && docker buildx build --platform linux/amd64 --load -f backend/Dockerfile \
     -t "openbox-backend:$TAG" . 2>&1 | tail -3)
  echo "[release] $(date +%T) build frontend"
  (cd "$WORK/src" && docker buildx build --platform linux/amd64 --load --build-arg "VITE_BUILD_ID=$TAG" \
     -t "openbox-frontend-v2:$TAG" frontend-v2 2>&1 | tail -3)
  echo "[release] $(date +%T) save"
  docker save "openbox-backend:$TAG" | gzip -1 > "$WORK/out/backend.tgz"
  docker save "openbox-frontend-v2:$TAG" | gzip -1 > "$WORK/out/frontend.tgz"
  (cd "$WORK/out" && shasum -a 256 backend.tgz frontend.tgz > SHA256SUMS && cat SHA256SUMS)
  echo "[release] $(date +%T) upload"
  for f in SHA256SUMS frontend.tgz backend.tgz; do
    aliyun oss cp "$WORK/out/$f" "$BUCKET/$TAG/$f" --endpoint "$ENDPOINT" \
      --parallel 4 --part-size 8388608 --force 2>&1 | tail -1
  done
  [ -z "$BUILD_ONLY" ] || { echo "[release] staged $TAG; ship with: release.sh --ship $TAG"; exit 0; }
fi

sign() {
  aliyun oss sign "$BUCKET/$TAG/$1" --timeout 7200 --endpoint "$ENDPOINT" 2>/dev/null | grep -m1 '^https://'
}
URL_B=$(sign backend.tgz); URL_F=$(sign frontend.tgz); URL_S=$(sign SHA256SUMS)
[ -n "$URL_B" ] && [ -n "$URL_F" ] && [ -n "$URL_S" ] || { echo "[release] signing failed" >&2; exit 1; }

# The machine-side script carries short-lived signed links: it stays in the work dir, never in git.
cat > "$WORK/remote.sh" <<EOF
#!/bin/bash
set -uo pipefail
TAG="$TAG"
mkdir -p /opt/openbox/releases/\$TAG && cd /opt/openbox/releases/\$TAG || exit 1
curl -fsSL -o SHA256SUMS '$URL_S' && curl -fsSL -o backend.tgz '$URL_B' && curl -fsSL -o frontend.tgz '$URL_F' || { echo "download failed"; exit 1; }
sha256sum -c SHA256SUMS || exit 1
docker load -q -i backend.tgz && docker load -q -i frontend.tgz || exit 1
rm -f backend.tgz frontend.tgz
cd /opt/openbox
cp docker-compose.override.yml "docker-compose.override.yml.bak-\$(date +%Y%m%d%H%M%S)"
sed -i -E "s#openbox-backend:[A-Za-z0-9._-]+#openbox-backend:\$TAG#g; s#openbox-frontend-v2:[A-Za-z0-9._-]+#openbox-frontend-v2:\$TAG#g" docker-compose.override.yml
grep -n 'image' docker-compose.override.yml
docker compose up -d 2>&1 | tail -6
for i in \$(seq 1 60); do
  S=\$(docker ps --format '{{.Names}} {{.Status}}' | grep -E 'openbox-(backend|trajectory-worker|frontend)-1')
  [ "\$(echo "\$S" | grep -c healthy)" = 3 ] && break
  sleep 5
done
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
echo "alembic: \$(docker compose exec -T backend alembic current 2>/dev/null | tail -1)"
echo "environment: \$(curl -s http://127.0.0.1/api/environment)"
echo "app-build: \$(curl -s http://127.0.0.1/index.html | grep -o '<meta name="app-build"[^>]*>' | head -1)"
echo "anon light entries: \$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/api/light/entries)"
docker compose logs --no-color --since 3m backend 2>/dev/null | grep -iE 'traceback|error' | grep -viE 'gateway timing' | tail -5 || true
EOF
echo "[release] $(date +%T) ship to light-test"
"$HERE/ecsrun.sh" "$WORK/remote.sh" 900
echo "[release] $(date +%T) clean OSS staging"
aliyun oss rm "$BUCKET/$TAG/" -r -f --endpoint "$ENDPOINT" >/dev/null 2>&1 || echo "[release] OSS cleanup failed; remove $BUCKET/$TAG/ by hand"
echo "[release] done $TAG"
