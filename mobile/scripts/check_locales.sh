#!/usr/bin/env bash
# Thin wrapper; the gate lives in check_locales.py.
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/check_locales.py" "$@"
