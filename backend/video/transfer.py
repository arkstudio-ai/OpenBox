"""Bounded recovery of already-paid video outputs; no provider resubmission."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

MAX_ATTEMPTS = 8
MAX_AGE = timedelta(hours=24)
RETRY_SECONDS = (120, 300, 900, 1800, 3600)


class TransferError(RuntimeError):
    public_message = True

    def __init__(self, stage: str, status_code: int, *, expired: bool = False):
        self.stage = stage
        self.status_code = status_code
        self.expired = expired
        message = (
            "The video was generated, but the provider download link has expired "
            f"(HTTP {status_code}). Automatic recovery stopped; a new download link "
            "from the provider is required. Do not automatically submit another paid generation."
            if expired else
            f"Video {'download' if stage == 'download' else 'storage upload'} failed (HTTP {status_code})."
        )
        super().__init__(message)


def _instant(value) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    except (AttributeError, TypeError, ValueError):
        return None


def signed_url_expired(url: str, now: datetime | None = None) -> bool:
    """Only classify a rejected URL using a recognized, valid expiry field.

    A bare 403 can mean a repairable permission problem. Never retain the URL,
    its credential or signature in the job's recovery metadata or error text.
    """
    try:
        query = {key.lower(): value for key, value in parse_qsl(urlsplit(url).query)}
        expiry = None
        for prefix in ("x-tos-", "x-amz-"):
            if prefix + "date" in query and prefix + "expires" in query:
                stamp = datetime.strptime(query[prefix + "date"], "%Y%m%dT%H%M%SZ")
                expiry = stamp.replace(tzinfo=timezone.utc) + timedelta(seconds=int(query[prefix + "expires"]))
                break
        if expiry is None and query.get("expires", "").isdigit():
            expiry = datetime.fromtimestamp(int(query["expires"]), timezone.utc)
        return expiry is not None and expiry <= (now or datetime.now(timezone.utc))
    except (ValueError, OverflowError, OSError):
        return False


def state(result_data) -> dict:
    value = result_data.get("transfer") if isinstance(result_data, dict) else None
    return dict(value) if isinstance(value, dict) else {}


def attempts(result_data) -> int:
    value = state(result_data).get("attempts", 0)
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0


def exhausted(result_data, now: datetime | None = None) -> bool:
    started = _instant(state(result_data).get("first_attempt_at"))
    return attempts(result_data) >= MAX_ATTEMPTS or bool(
        started and (now or datetime.now(timezone.utc)) - started >= MAX_AGE
    )


def retry_after(job, now: datetime | None = None) -> int:
    if job.status != "transfer_failed":
        return 0
    result_data = getattr(job, "result_data", None)
    if exhausted(result_data, now):
        return 0
    due = _instant(state(result_data).get("next_retry_at"))
    return max(0, math.ceil((due - (now or datetime.now(timezone.utc))).total_seconds())) if due else 0


def begin(result_data, now: datetime) -> dict:
    previous = state(result_data)
    return {"attempts": attempts(result_data) + 1,
            "first_attempt_at": previous.get("first_attempt_at") or now.isoformat(),
            "last_attempt_at": now.isoformat(), "next_retry_at": None}


def failed(attempt: dict, error: Exception, now: datetime) -> dict:
    expired = isinstance(error, TransferError) and error.expired
    stopped = expired or exhausted({"transfer": attempt}, now)
    delay = RETRY_SECONDS[min(max(0, attempt["attempts"] - 1), len(RETRY_SECONDS) - 1)]
    next_retry = now + timedelta(seconds=delay)
    if started := _instant(attempt.get("first_attempt_at")):
        next_retry = min(next_retry, started + MAX_AGE)
    return {**attempt, "next_retry_at": None if stopped else next_retry.isoformat(),
            "stopped": stopped,
            "reason": "source_url_expired" if expired else "retry_limit" if stopped else "temporary_failure",
            "stage": error.stage if isinstance(error, TransferError) else "transfer",
            "http_status": error.status_code if isinstance(error, TransferError) else None}


LIMIT_MESSAGE = (
    "The video was generated, but transfer recovery reached its retry/time limit. "
    "Automatic recovery stopped; operator assistance is required. "
    "Do not automatically submit another paid generation."
)
