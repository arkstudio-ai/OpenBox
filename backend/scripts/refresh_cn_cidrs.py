"""Regenerate core/data/cn_cidrs.txt from APNIC's public allocation table.

    curl -o /tmp/delegated-apnic-latest https://ftp.apnic.net/stats/apnic/delegated-apnic-latest
    python backend/scripts/refresh_cn_cidrs.py /tmp/delegated-apnic-latest

Only records registered to CN are kept (Hong Kong, Macau and Taiwan have their
own codes). Allocations change slowly; a refresh every few months is plenty.
"""
from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "core" / "data" / "cn_cidrs.txt"


def main(source: str) -> None:
    v4, v6, serial = [], [], "unknown"
    for line in Path(source).read_text().splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 3 and parts[0] == "2" and parts[1] == "apnic":
            serial = parts[2]
        if len(parts) < 7 or parts[1] != "CN" or parts[6] not in {"allocated", "assigned"}:
            continue
        if parts[2] == "ipv4":
            start = ipaddress.IPv4Address(parts[3])
            v4.extend(ipaddress.summarize_address_range(start, start + int(parts[4]) - 1))
        elif parts[2] == "ipv6":
            v6.append(ipaddress.IPv6Network(f"{parts[3]}/{parts[4]}"))
    nets = list(ipaddress.collapse_addresses(v4)) + list(ipaddress.collapse_addresses(v6))
    if len(nets) < 1000:
        raise SystemExit(f"only {len(nets)} networks parsed from {source}; refusing to overwrite")
    TARGET.write_text(
        f"# Mainland China (CN) address space from APNIC delegated-apnic-latest serial {serial}.\n"
        "# Regenerate: backend/scripts/refresh_cn_cidrs.py. Hong Kong, Macau and Taiwan are not in this list.\n"
        + "\n".join(str(net) for net in nets) + "\n")
    print(f"{TARGET}: {len(nets)} networks, serial {serial}")


if __name__ == "__main__":
    main(sys.argv[1])
