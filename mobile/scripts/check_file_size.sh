#!/usr/bin/env bash
# Gate: no Dart source file may exceed 800 lines (project rule, mirrors
# frontend-v2's scripts/ gate style). Run from mobile/.
set -euo pipefail

LIMIT=800
fail=0

while IFS= read -r file; do
  lines=$(wc -l <"$file" | tr -d ' ')
  if [ "$lines" -gt "$LIMIT" ]; then
    echo "FAIL: $file has $lines lines (limit $LIMIT)"
    fail=1
  fi
done < <(find lib test -name '*.dart' -not -path '*/generated/*' 2>/dev/null)

if [ "$fail" -eq 0 ]; then
  echo "OK: all Dart files within $LIMIT lines"
fi
exit "$fail"
