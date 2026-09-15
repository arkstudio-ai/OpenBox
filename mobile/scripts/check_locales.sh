#!/usr/bin/env bash
# Gate: the Flutter locale bundle is a byte-for-byte mirror of frontend-v2,
# except for the namespaces of web-only features listed below.
# Run from anywhere in the repository.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
WEB_DIR="$ROOT_DIR/frontend-v2/src/locales"
MOBILE_DIR="$ROOT_DIR/mobile/assets/locales"
I18N_FILE="$ROOT_DIR/mobile/lib/shared/i18n/i18n.dart"

# Session trajectory monitoring is too dense to read on a phone, so the app
# does not ship it or its strings.
WEB_ONLY_NAMESPACES=(admin-trajectories)

diff_args=()
for namespace in "${WEB_ONLY_NAMESPACES[@]}"; do
  diff_args+=("--exclude=$namespace.json")
done

if ! diff -qr "${diff_args[@]}" "$WEB_DIR" "$MOBILE_DIR"; then
  echo "FAIL: mobile locales differ from frontend-v2; copy them verbatim" >&2
  exit 1
fi

while IFS= read -r locale_file; do
  namespace="$(basename "$locale_file" .json)"
  if [[ " ${WEB_ONLY_NAMESPACES[*]} " == *" $namespace "* ]]; then
    continue
  fi
  if ! grep -Eq "^[[:space:]]*'$namespace',[[:space:]]*$" "$I18N_FILE"; then
    echo "FAIL: namespace '$namespace' is not registered in i18n.dart" >&2
    exit 1
  fi
done < <(find "$WEB_DIR/en-US" -maxdepth 1 -type f -name '*.json' | sort)

echo "OK: mobile locales match frontend-v2 byte for byte"
