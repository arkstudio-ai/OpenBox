#!/usr/bin/env python3
"""Verify that a Wuying golden-image source is complete and contains no secrets."""
from __future__ import annotations

import argparse
import pathlib
import sys

from wuying_bootstrap import Desktop


VERIFY_SCRIPT = r"""
set -u
failed=0
check() { if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; failed=1; fi; }

check root_ssh_absent_or_empty '[ ! -e /root/.ssh ] || [ -z "$(find /root/.ssh -mindepth 1 -print -quit)" ]'
check tunnel_disabled '[ "$(systemctl is-enabled openbox-tunnel 2>/dev/null || true)" = disabled ]'
check action_disabled '[ "$(systemctl is-enabled openbox-action-server 2>/dev/null || true)" = disabled ]'
check action_uses_envfile 'grep -q "^EnvironmentFile=/etc/openbox/action.env$" /etc/systemd/system/openbox-action-server.service'
check action_has_no_inline_key '! grep -q "Environment=SESSION_API""_KEY=" /etc/systemd/system/openbox-action-server.service'
check tunnel_uses_envfile 'grep -q "^EnvironmentFile=/etc/openbox/tunnel.env$" /etc/systemd/system/openbox-tunnel.service'
check tunnel_has_no_legacy_port '! grep -q -- "-R 127.0.0.1:18000" /etc/systemd/system/openbox-tunnel.service'
check tunnel_has_no_legacy_relay '! grep -q "47.110.66.89" /etc/systemd/system/openbox-tunnel.service'
check openbox_config_empty '[ -d /etc/openbox ] && [ -z "$(find /etc/openbox -mindepth 1 -print -quit)" ]'
check display_helper '[ -x /usr/local/bin/obx-display ] && grep -q "target=\"1920x1080\"" /usr/local/bin/obx-display'

# Scan for actual secret-bearing files, not documentation or crypto-library
# constants containing words such as "OPENSSH PRIVATE".  A PEM private key's
# first line is authoritative; action secrets are assignments in env/unit
# files.  Build the strings in pieces so Cloud Assistant's own transient copy
# of this verifier cannot match itself.
pem_pattern=$(printf '%s%s' '^-----BEGIN .*' 'PRIVATE KEY-----$')
api_env_pattern=$(printf '%s%s' '^SESSION_API' '_KEY=')
unit_env_pattern=$(printf '%s%s' 'Environment=SESSION_API' '_KEY=')
private_key_hits=$(find / \
  -path /proc -prune -o -path /sys -prune -o -path /dev -prune -o \
  -path /run -prune -o -path /tmp -prune -o -path /var/lib/docker -prune -o \
  -type f -size -2M -exec sh -c '
    first=$(head -n 1 "$1" 2>/dev/null || true)
    printf "%s\\n" "$first" | grep -Eq "$2" && printf "%s\\n" "$1"
  ' sh {} "$pem_pattern" \; 2>/dev/null || true)
inline_key_hits=$(grep -rIl \
  --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev --exclude-dir=run \
  --exclude-dir=tmp --exclude-dir=var/lib/docker \
  -e "$api_env_pattern" -e "$unit_env_pattern" / 2>/dev/null || true)
secret_hits=$(printf '%s\n%s\n' "$private_key_hits" "$inline_key_hits" | sed '/^$/d' | sort -u)
if [ -z "$secret_hits" ]; then
  echo 'PASS no_baked_secrets'
else
  echo 'FAIL no_baked_secrets'
  printf '%s\n' "$secret_hits"
  failed=1
fi

sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' /tmp/openbox-image-baseline.txt | LC_ALL=C sort -u > /tmp/openbox-baseline-packages
dpkg-query -W -f='${binary:Package}\n' | LC_ALL=C sort -u > /tmp/openbox-actual-packages
missing=$(comm -23 /tmp/openbox-baseline-packages /tmp/openbox-actual-packages)
extra=$(comm -13 /tmp/openbox-baseline-packages /tmp/openbox-actual-packages)
if [ -z "$missing" ]; then
  echo "PASS package_inventory_contains_baseline ($(wc -l < /tmp/openbox-baseline-packages) baseline packages)"
  echo "INFO package_inventory_extra_count=$(printf '%s\n' "$extra" | sed '/^$/d' | wc -l)"
else
  echo 'FAIL package_inventory_contains_baseline'
  printf 'missing: %s\n' "$(printf '%s\n' "$missing" | paste -sd, -)"
  failed=1
fi
exit "$failed"
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop-id", required=True)
    parser.add_argument("--region", default="cn-shanghai")
    parser.add_argument("--baseline", type=pathlib.Path, required=True)
    args = parser.parse_args()

    desktop = Desktop(args.desktop_id, args.region)
    desktop.put(args.baseline, "/tmp/openbox-image-baseline.txt", mode="600")
    output = desktop.run(VERIFY_SCRIPT, timeout=600, check=False)
    print(output.rstrip())
    if "FAIL " in output:
        return 1
    print("\nGolden-image verification passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
