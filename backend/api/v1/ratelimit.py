"""Per-key request rate limiting on a fixed window over the shared cache.

``"60/minute"`` (the ``rate_limit_api`` config format) becomes 60 requests
per 60-second bucket. The window is fixed rather than sliding because the
cache interface only offers ``incr`` with a TTL; the contract promises the
``X-RateLimit-*`` headers and a ``Retry-After`` on 429, which a fixed window
delivers exactly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from api.v1.errors import ApiError

_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


@dataclass(frozen=True)
class RateInfo:
    limit: int
    remaining: int
    reset: int  # unix seconds when the current window ends

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset),
        }


def parse_rate(spec: str | None) -> tuple[int, int] | None:
    """``"60/minute"`` → ``(60, 60)``; ``None``/blank/``"0/..."`` disables."""
    if not spec:
        return None
    count, _, unit = spec.strip().partition("/")
    unit = unit.strip().lower().rstrip("s") or "minute"
    try:
        limit = int(count)
    except ValueError as exc:
        raise ValueError(f"invalid rate limit {spec!r}") from exc
    if unit not in _UNITS:
        raise ValueError(f"invalid rate limit unit in {spec!r}")
    if limit <= 0:
        return None
    return limit, _UNITS[unit]


async def check(cache, subject: str, spec: str | None, *, now: float | None = None) -> RateInfo | None:
    """Count one request for ``subject``; raise 429 past the limit.

    Returns the window state for the response headers, or ``None`` when
    limiting is off (no cache, or no limit configured).
    """
    parsed = parse_rate(spec)
    if cache is None or parsed is None:
        return None
    limit, window = parsed
    moment = time.time() if now is None else now
    bucket = int(moment // window)
    reset = (bucket + 1) * window
    key = f"v1:rl:{subject}:{window}:{bucket}"
    used = await cache.incr(key, ttl=window + 1)
    remaining = max(0, limit - used)
    info = RateInfo(limit=limit, remaining=remaining, reset=reset)
    if used > limit:
        retry_after = max(1, reset - int(moment))
        raise ApiError(
            429, "RATE_LIMITED", f"Rate limit of {spec} exceeded; retry in {retry_after}s",
            headers={"Retry-After": str(retry_after), **info.headers()},
        )
    return info
