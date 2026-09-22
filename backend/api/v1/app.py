"""The ``/v1`` sub-application: routers, error contract, request ids."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from api.v1.errors import code_and_message, error_body
from core.identifier import ascending
from core.log import create_logger

log = create_logger("api.v1")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Stamp every response with ``X-Request-Id`` and the rate-limit headers."""

    async def dispatch(self, request: Request, call_next):
        request.state.request_id = (request.headers.get("X-Request-Id") or "")[:128] or ascending("req")
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        info = getattr(request.state, "rate_limit", None)
        if info is not None:
            for name, value in info.headers().items():
                response.headers.setdefault(name, value)
        return response


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


def _validation_message(exc: RequestValidationError) -> tuple[str, dict]:
    errors = exc.errors()
    if not errors:
        return "Invalid request", {}
    first = errors[0]
    field = ".".join(str(p) for p in first.get("loc", []) if p not in ("body", "query", "path"))
    message = first.get("msg", "Invalid request")
    if field:
        message = f"{field}: {message}"
    return message, {"field": field} if field else {}


def create_v1_app() -> FastAPI:
    app = FastAPI(
        title="OpenBox Harness API",
        version="1.0",
        description="API-key authenticated, polling-first access to OpenBox sessions.",
    )
    app.add_middleware(RequestContextMiddleware)
    # Bearer-only API: no cookies are ever read here, so any origin may call it.
    # Partners' browser-side test consoles depend on this; their servers do not.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "X-RateLimit-Limit", "X-RateLimit-Remaining",
                        "X-RateLimit-Reset", "Retry-After", "Location"],
        max_age=600,
    )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        code, message = code_and_message(exc)
        details = getattr(exc, "details", None)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(code, message, _request_id(request), details),
            headers=exc.headers or None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        message, details = _validation_message(exc)
        return JSONResponse(
            status_code=400,
            content=error_body("INVALID_REQUEST", message, _request_id(request), details),
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        log.error(f"Unhandled /v1 error request_id={_request_id(request)}: {exc!r}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content=error_body("INTERNAL_ERROR", "Internal error", _request_id(request)),
        )

    from api.v1.files import router as files_router
    from api.v1.messages import router as messages_router
    from api.v1.questions import router as questions_router
    from api.v1.sessions import router as sessions_router

    app.include_router(sessions_router)
    app.include_router(messages_router)
    app.include_router(questions_router)
    app.include_router(files_router)
    return app
