"""Which side of the mainland-China line a request comes from.

Mainland visitors sign in through Logto and may pay; everyone else signs in
with e-mail and password and is given plans by an operator. The decision is
made here, from the caller's address, and only when ``REGION_GATE=on``: a
deployment that does not set it treats every request as mainland, which is how
the service behaved before.

The address is the last ``X-Forwarded-For`` entry that is not one of our own
proxies. Entries to its left were supplied by the client and are ignored.
An address we cannot read or classify counts as mainland.
"""
from __future__ import annotations

import bisect
import ipaddress
import os
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException, Request

CN = "cn"
INTL = "intl"
DATA_FILE = Path(__file__).with_name("data") / "cn_cidrs.txt"
PAYMENT_REFUSAL = "PAYMENT_REGION_UNAVAILABLE"


def enabled() -> bool:
    return os.environ.get("REGION_GATE", "").strip().lower() in {"1", "on", "true", "yes"}


@lru_cache(maxsize=1)
def _trusted() -> tuple:
    nets = []
    for item in os.environ.get("REGION_TRUSTED_PROXIES", "").split(","):
        item = item.strip()
        if item:
            try:
                nets.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                continue
    return tuple(nets)


def _ours(address) -> bool:
    """Our own hop: a private or loopback address, or a configured edge proxy."""
    if address.is_private or address.is_loopback or address.is_link_local:
        return True
    return any(address.version == net.version and address in net for net in _trusted())


def _parse(value: str | None):
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped or address


def client_ip(request: Request | None) -> str | None:
    """The caller's public address, or None when only our own hops are visible."""
    if request is None:
        return None
    hops = [_parse(part) for part in (request.headers.get("x-forwarded-for") or "").split(",")]
    hops.append(_parse(request.headers.get("x-real-ip")) if not any(hops) else None)
    hops.append(_parse(request.client.host) if request.client else None)
    for address in reversed([hop for hop in hops if hop is not None]):
        if not _ours(address):
            return str(address)
    return None


@lru_cache(maxsize=1)
def _ranges() -> dict[int, tuple[list[int], list[int]]]:
    """Sorted, non-overlapping (start, end) integers per address family."""
    pairs: dict[int, list[tuple[int, int]]] = {4: [], 6: []}
    for line in DATA_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        net = ipaddress.ip_network(line)
        pairs[net.version].append((int(net.network_address), int(net.broadcast_address)))
    out = {}
    for version, items in pairs.items():
        items.sort()
        out[version] = ([start for start, _ in items], [end for _, end in items])
    return out


def is_mainland(ip: str | None) -> bool:
    address = _parse(ip)
    if address is None:
        return True
    starts, ends = _ranges()[address.version]
    index = bisect.bisect_right(starts, int(address)) - 1
    return index >= 0 and int(address) <= ends[index]


def region_of(request: Request | None) -> str:
    if not enabled():
        return CN
    ip = client_ip(request)
    return CN if ip is None or is_mainland(ip) else INTL


def require_payment_region(request: Request) -> None:
    """Dependency for the routes that start a payment; settlement callbacks never use it."""
    if region_of(request) != CN:
        raise HTTPException(status_code=403, detail={
            "code": PAYMENT_REFUSAL,
            "message": "Online payment is not available in your region; ask an administrator to enable a plan",
        })
