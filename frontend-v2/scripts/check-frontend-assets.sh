#!/bin/sh
# Run inside a serving frontend container, including an unpublished candidate.
set -eu
origin=http://127.0.0.1
html=$(wget -T 2 -qO- "$origin/app")
assets=$(printf '%s\n' "$html" | grep -Eo '(src|href)="/assets/[^" ?]+\.(js|css)"' | cut -d '"' -f 2 | sort -u)
printf '%s\n' "$assets" | grep -q '\.js$'
printf '%s\n' "$assets" | grep -q '\.css$'
for asset in $assets; do
    case "$asset" in
        *.css) mime='text/css' ;;
        *.js) mime='(application|text)/javascript' ;;
    esac
    if ! headers=$(wget -T 2 -S --spider "$origin$asset" 2>&1) ||
       ! printf '%s\n' "$headers" | grep -Eiq "Content-Type:[[:space:]]*$mime([;[:space:]]|$)"; then
        echo "Cannot serve $asset with the expected MIME type" >&2
        exit 1
    fi
done
