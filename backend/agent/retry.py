"""Retry logic with exponential backoff."""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import re
from typing import Any, Callable, Awaitable

from bus import bus
from bus.events import SESSION_STATUS
from core.log import create_logger

log = create_logger("agent.retry")

RETRY_INITIAL_DELAY = 2.0  # seconds
RETRY_BACKOFF_FACTOR = 2
RETRY_MAX_DELAY = 30.0  # seconds
MAX_RETRIES = 10
# Without jitter every caller that hit the same rate limit retries on the same
# tick and stampedes the provider again. Spreads each delay over [1-f, 1+f].
RETRY_JITTER_FACTOR = 0.25

# Context overflow error patterns
OVERFLOW_PATTERNS = [
    r"prompt is too long",
    r"input is too long for requested model",
    r"exceeds the context window",
    r"input token count.*exceeds the maximum",
    r"maximum prompt length is \d+",
    r"reduce the length of the messages",
    r"maximum context length is \d+ tokens",
    r"exceeds the limit of \d+",
    r"exceeds the available context size",
    r"greater than the context length",
    r"context window exceeds limit",
    r"exceeded model token limit",
    r"context[_ ]length[_ ]exceeded",
]


class RetryableError(Exception):
    """Error that can be retried."""
    def __init__(self, message: str, status_code: int | None = None, headers: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


class ContextOverflowError(Exception):
    """Context window exceeded - do not retry, trigger compaction."""
    pass


class MaxRetriesExceeded(Exception):
    """Maximum retry attempts exhausted."""
    pass


def _error_status_code(error: Exception) -> int | None:
    """Best-effort HTTP status extraction across SDK exception shapes."""
    value = getattr(error, "status_code", None)
    if value is None:
        value = getattr(getattr(error, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _error_headers(error: Exception) -> dict[str, str]:
    """Normalize Retry-After headers from OpenAI/LiteLLM/httpx errors."""
    candidates = (
        getattr(error, "headers", None),
        getattr(getattr(error, "response", None), "headers", None),
    )
    normalized: dict[str, str] = {}
    for headers in candidates:
        if not headers:
            continue
        try:
            items = headers.items()
        except AttributeError:
            continue
        for key, value in items:
            normalized[str(key).lower()] = str(value)
    return normalized


def _retry_after_seconds(value: str) -> float | None:
    """Parse either delta-seconds or the HTTP-date form of Retry-After."""
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def is_context_overflow(message: str) -> bool:
    """Check if an error message indicates context overflow."""
    return any(re.search(p, message, re.IGNORECASE) for p in OVERFLOW_PATTERNS)


# Transient failures worth another attempt. Ported from opencode's
# RETRYABLE_MESSAGE_PATTERNS, which covers considerably more ground than
# matching a handful of substrings did — in particular the shapes that
# OpenAI-compatible gateways return, where the real cause is wrapped in the
# proxy's own message rather than surfacing as a status code.
RETRYABLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Rate limited", re.compile(
        r"\b429\b|rate[ _-]?limit|rate increased too quickly|too many requests", re.I)),
    ("Service unavailable", re.compile(
        r"\b(500|502|503|504|524)\b|overloaded|service[ _-]?unavailable"
        r"|internal (server )?error|server[ _-]?error|bad[ _-]?gateway"
        r"|provider[ _-]?returned[ _-]?error", re.I)),
    ("Network error", re.compile(
        r"terminated|fetch failed|failed to fetch|network error|upstream connect"
        r"|connection (error|refused|reset|lost|aborted)|socket hang up"
        r"|reset before headers|getaddrinfo|remote (end )?closed"
        r"|enotfound|eai_again|econnrefused|econnreset|etimedout", re.I)),
    ("Timeout", re.compile(
        r"^timeout$|\b(request|response|connection|network|stream|read)[ _-]?"
        r"(timeout|timed out|time out)\b|read timeout", re.I)),
    ("Resource exhausted", re.compile(
        r"try your request again|retry your request|resource[ _-]?exhausted", re.I)),
]


def is_retryable(error: Exception) -> str | None:
    """Check if an error is retryable. Returns a display message or None."""
    if isinstance(error, ContextOverflowError):
        return None

    msg = str(error).lower()

    status_code = _error_status_code(error)
    if status_code in (429, 503, 529):
        return "Rate limited" if status_code == 429 else "Service unavailable"
    if status_code in (500, 502, 504, 524):
        if status_code == 502:
            return "Bad gateway"
        return "Service unavailable"

    if isinstance(error, RetryableError):
        if "overloaded" in msg:
            return "Provider is overloaded"
        return str(error)

    for label, pattern in RETRYABLE_PATTERNS:
        if pattern.search(msg):
            return label

    return None


def _jitter(seconds: float, rand: float) -> float:
    """Spread a delay over [1-f, 1+f] of its nominal value."""
    return seconds * (1.0 + RETRY_JITTER_FACTOR * (2.0 * rand - 1.0))


def retry_delay(attempt: int, error: Exception | None = None, rand: float | None = None) -> float:
    """Calculate retry delay in seconds.

    `rand` is injectable so the schedule can be asserted in tests; it defaults
    to random.random(). A server-supplied retry-after is honoured verbatim —
    jitter is only applied to delays we invented ourselves.
    """
    if rand is None:
        rand = random.random()
    headers = _error_headers(error) if error is not None else {}
    if headers:
        # 1. retry-after-ms
        if "retry-after-ms" in headers:
            try:
                return max(0.0, float(headers["retry-after-ms"]) / 1000.0)
            except (TypeError, ValueError):
                pass
        # 2. retry-after (seconds)
        if "retry-after" in headers:
            seconds = _retry_after_seconds(headers["retry-after"])
            if seconds is not None:
                return seconds
        # Preserve the existing custom RetryableError contract: an API that
        # supplied headers but omitted Retry-After gets an uncapped schedule.
        if isinstance(error, RetryableError) and error.headers:
            return _jitter(
                RETRY_INITIAL_DELAY * (RETRY_BACKOFF_FACTOR ** (attempt - 1)),
                rand,
            )

    # No headers, exponential backoff with cap
    return _jitter(
        min(
            RETRY_INITIAL_DELAY * (RETRY_BACKOFF_FACTOR ** (attempt - 1)),
            RETRY_MAX_DELAY,
        ),
        rand,
    )


async def with_retry(
    fn: Callable[[], Awaitable[Any]],
    session_id: str = "",
    max_retries: int = MAX_RETRIES,
    user_id: str = "default",
) -> Any:
    """Execute a function with retry logic."""
    for attempt in range(1, max_retries + 1):
        try:
            return await fn()
        except ContextOverflowError:
            raise  # Never retry context overflow
        except Exception as e:
            retry_msg = is_retryable(e)
            if retry_msg is None or attempt >= max_retries:
                raise

            delay = retry_delay(attempt, e)
            log.warning(f"Attempt {attempt}/{max_retries} failed: {retry_msg}. Retrying in {delay:.1f}s")

            if session_id:
                payload = {
                    "userId": user_id,
                    "sessionId": session_id,
                    "status": "retry",
                }
                from agent.driver import current_run_fence

                fence = current_run_fence()
                if fence is not None and fence[0] == session_id:
                    payload["generation"] = fence[2]
                bus.publish(SESSION_STATUS, payload)

            await asyncio.sleep(delay)

    raise MaxRetriesExceeded(f"Failed after {max_retries} attempts")
