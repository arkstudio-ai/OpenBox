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

private_key_pattern=$(printf '%s%s' 'OPENSSH' ' PRIVATE')
inline_key_pattern=$(printf '%s%s' 'SESSION_API' '_KEY=')
secret_hits=$(grep -rIl \
  --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev --exclude-dir=run \
  --exclude-dir=tmp --exclude-dir=var/lib/docker \
  -e "$private_key_pattern" -e "$inline_key_pattern" / 2>/dev/null || true)
if [ -z "$secret_hits" ]; then
  echo 'PASS no_baked_secrets'
else
  echo 'FAIL no_baked_secrets'
  printf '%s\n' "$secret_hits"
  failed=1
fi

echo __OPENBOX_PACKAGES_BEGIN__
dpkg-query -W -f='${binary:Package}\n' | LC_ALL=C sort -u
echo __OPENBOX_PACKAGES_END__
exit "$failed"
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop-id", required=True)
    parser.add_argument("--region", default="cn-shanghai")
    parser.add_argument("--baseline", type=pathlib.Path, required=True)
    args = parser.parse_args()

    desktop = Desktop(args.desktop_id, args.region)
    output = desktop.run(VERIFY_SCRIPT, timeout=600, check=False)
    before, marker, rest = output.partition("__OPENBOX_PACKAGES_BEGIN__\n")
    package_text, end_marker, tail = rest.partition("__OPENBOX_PACKAGES_END__")
    print(before.rstrip())
    if not marker or not end_marker:
        print("FAIL package_inventory_missing")
        return 1

    actual = {line.strip() for line in package_text.splitlines() if line.strip()}
    baseline = {
        line.strip()
        for line in args.baseline.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    missing = sorted(baseline - actual)
    extra = sorted(actual - baseline)
    if missing or extra:
        print("FAIL package_inventory_matches")
        if missing:
            print("  missing: " + ", ".join(missing))
        if extra:
            print("  extra: " + ", ".join(extra))
        return 1
    print(f"PASS package_inventory_matches ({len(actual)} packages)")
    if "FAIL " in before or tail.strip():
        return 1
    print("\nGolden-image verification passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
