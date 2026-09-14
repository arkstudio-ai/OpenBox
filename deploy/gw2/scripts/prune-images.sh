#!/usr/bin/env bash
# Remove old Docker images from the host (SPEC §12). Keeps every image used by a container
# (running or stopped), every image the compose project references, and the newest --keep tags of
# each repository by image creation time; dangling images are pruned too. Dry run unless
# --execute; the weekly timer passes --execute. With --execute nothing is removed when the compose
# files in OPENBOX_DIR cannot be read, because the images they pin could not be protected.
#
#   prune-images.sh [--keep 3] [--execute]
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

keep=3
while [ $# -gt 0 ]; do
  case "$1" in
    --keep)
      need_value "$1" $#
      keep=$2
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
positive_integer "$keep" || die "--keep must be a positive integer"
take_lock prune-images

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
: >"$work/protected"

containers=$(docker ps -aq)
if [ -n "$containers" ]; then
  # shellcheck disable=SC2086 # one argument per container id
  docker inspect --format '{{.Image}}' $containers >>"$work/protected"
fi
# A stopped or removed service must still be able to start from its pinned image.
if [ -d "$OPENBOX_DIR" ]; then
  if referenced=$(compose config --images 2>/dev/null); then
    for image in $referenced; do
      docker image inspect --format '{{.Id}}' "$image" >>"$work/protected" 2>/dev/null || true
    done
  elif [ "$EXECUTE" = 1 ]; then
    die "docker compose config failed in $OPENBOX_DIR, so the images it pins cannot be protected; nothing was removed"
  else
    warn "docker compose config failed in $OPENBOX_DIR: images pinned only by the compose files are not protected in this listing"
  fi
fi

docker image ls --no-trunc --format '{{.Repository}} {{.Tag}} {{.ID}}' >"$work/images"
while read -r repository tag id; do
  if [ "$repository" = "<none>" ] || [ "$tag" = "<none>" ]; then
    continue
  fi
  created=$(docker image inspect --format '{{.Created}}' "$id")
  created=${created%%.*}
  printf '%s %s %s %s\n' "$repository" "${created%Z}" "$tag" "$id"
done <"$work/images" | sort -k1,1 -k2,2r -k3,3r >"$work/sorted"
awk -v keep="$keep" '{ if ($1 != repository) { repository = $1; rank = 0 } if (++rank > keep) print }' "$work/sorted" >"$work/candidates"

count=0
while read -r repository created tag id; do
  if grep -qxF "$id" "$work/protected"; then
    log "keep $repository:$tag (used by a container or the compose project)"
    continue
  fi
  if [ "$EXECUTE" = 1 ]; then
    if docker image rm "$repository:$tag" >/dev/null; then
      count=$((count + 1))
      log "removed $repository:$tag (created $created)"
    else
      warn "could not remove $repository:$tag"
    fi
  else
    count=$((count + 1))
    log "would remove $repository:$tag (created $created)"
  fi
done <"$work/candidates"

dangling=$(docker image ls -q --filter dangling=true | wc -l | tr -d ' ')
if [ "$EXECUTE" = 1 ]; then
  docker image prune -f >/dev/null
  log "removed $count tag(s) and pruned $dangling dangling image(s)"
else
  log "dry run: $count tag(s) and $dangling dangling image(s) would be removed; re-run with --execute"
fi
