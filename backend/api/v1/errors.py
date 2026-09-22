"""Error contract for ``/v1``: ``{"error": {code, message, request_id}}``."""
from __future__ import annotations

from fastapi import HTTPException

#: Default code per status for errors raised by shared internals that only
#: carry a status and a prose detail.
CODE_BY_STATUS = {
    400: "INVALID_REQUEST",
    401: "UNAUTHORIZED",
    402: "INSUFFICIENT_CREDITS",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "INVALID_REQUEST",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    502: "LLM_UNAVAILABLE",
    503: "SERVICE_UNAVAILABLE",
}


class ApiError(HTTPException):
    """An HTTP error with a stable machine-readable code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        details: dict | None = None,
    ):
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.details = details or {}


def code_and_message(exc: HTTPException) -> tuple[str, str]:
    """Normalise any HTTPException into the public (code, message) pair.

    Internal helpers raise ``detail`` as either prose or a ``{"code", ...}``
    dict; both shapes are folded into the contract here so the routes can
    reuse them unchanged.
    """
    if isinstance(exc, ApiError):
        return exc.code, str(exc.detail)
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code") or CODE_BY_STATUS.get(exc.status_code, "ERROR"))
        message = str(detail.get("message") or detail.get("detail") or code)
        return code, message
    header_code = (exc.headers or {}).get("X-Error-Code") if exc.headers else None
    code = header_code or CODE_BY_STATUS.get(exc.status_code, "ERROR")
    return code, str(detail) if detail else code


def error_body(code: str, message: str, request_id: str, details: dict | None = None) -> dict:
    error = {"code": code, "message": message, "request_id": request_id}
    if details:
        error["details"] = details
    return {"error": error}
