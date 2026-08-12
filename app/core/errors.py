"""RFC 7807 ``application/problem+json`` errors and the exception taxonomy.

Every error leaving the API has the same shape, including unhandled ones, so a
client never has to branch on whether a failure was anticipated (RFC 9.3).

``type`` is a stable, dereferenceable URI under ``ERROR_TYPE_BASE``. It is the
field clients should branch on -- ``title`` and ``detail`` are prose and may be
reworded without a version bump; ``type`` may not.

The exception classes below are raised from services and repositories. Routers
do not construct HTTP responses for failure cases; they let these propagate,
which is what keeps business logic out of the API layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.telemetry import current_trace_id

if TYPE_CHECKING:
    from fastapi.responses import Response

logger = structlog.get_logger()

ERROR_TYPE_BASE = "https://api.playbook.dev/errors"

PROBLEM_JSON = "application/problem+json"


class FieldError(BaseModel):
    """One field-level validation failure, derived from Pydantic's error dict."""

    loc: list[str | int]
    msg: str
    type: str


class ProblemDetail(BaseModel):
    """RFC 7807 problem document, plus the ``trace_id`` extension member."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    instance: str = ""
    trace_id: str = ""
    errors: list[FieldError] = Field(default_factory=list)


class AppError(Exception):
    """Base for every error the API knows how to render.

    Subclasses set ``status_code``, ``title``, and ``slug``; ``slug`` becomes
    the last path segment of the ``type`` URI.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    title: str = "Internal Server Error"
    slug: str = "internal"

    def __init__(
        self,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        errors: list[FieldError] | None = None,
    ) -> None:
        self.detail = detail or self.title
        self.headers = headers or {}
        self.errors = errors or []
        super().__init__(self.detail)

    @property
    def type_uri(self) -> str:
        return f"{ERROR_TYPE_BASE}/{self.slug}"


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    title = "Validation Error"
    slug = "validation-failed"


class UnauthorizedError(AppError):
    """No credential, or one that does not verify. Never says which."""

    status_code = status.HTTP_401_UNAUTHORIZED
    title = "Unauthorized"
    slug = "unauthorized"

    def __init__(self, detail: str | None = None, **kwargs: Any) -> None:
        super().__init__(detail, **kwargs)
        self.headers.setdefault("WWW-Authenticate", "Bearer")


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    title = "Forbidden"
    slug = "forbidden"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    title = "Not Found"
    slug = "not-found"


class ConflictError(AppError):
    """State conflict -- the request is well formed but the resource is not ready."""

    status_code = status.HTTP_409_CONFLICT
    title = "Conflict"
    slug = "conflict"


class LeagueNotSyncedError(ConflictError):
    slug = "league-not-synced"
    title = "League has not completed its initial sync"


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    title = "Too Many Requests"
    slug = "rate-limited"

    def __init__(self, retry_after: int, detail: str | None = None, **kwargs: Any) -> None:
        super().__init__(detail, **kwargs)
        self.retry_after = retry_after
        self.headers.setdefault("Retry-After", str(retry_after))


class UpstreamError(AppError):
    """A provider or third-party API failed or is circuit-broken."""

    status_code = status.HTTP_502_BAD_GATEWAY
    title = "Upstream Unavailable"
    slug = "upstream-unavailable"


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    title = "Service Unavailable"
    slug = "service-unavailable"


class TimeoutError(AppError):  # noqa: A001 - deliberately shadows the builtin in this namespace
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    title = "Gateway Timeout"
    slug = "timeout"


# Starlette raises bare HTTPExceptions for routing failures (404 on an unknown
# path, 405 on a bad method) that never pass through our exception classes.
# Mapping them here is what makes "all errors are problem+json" true rather
# than mostly true.
_STATUS_SLUGS: dict[int, tuple[str, str]] = {
    400: ("bad-request", "Bad Request"),
    401: ("unauthorized", "Unauthorized"),
    403: ("forbidden", "Forbidden"),
    404: ("not-found", "Not Found"),
    405: ("method-not-allowed", "Method Not Allowed"),
    409: ("conflict", "Conflict"),
    413: ("payload-too-large", "Payload Too Large"),
    422: ("validation-failed", "Validation Error"),
    429: ("rate-limited", "Too Many Requests"),
    500: ("internal", "Internal Server Error"),
    502: ("upstream-unavailable", "Upstream Unavailable"),
    503: ("service-unavailable", "Service Unavailable"),
    504: ("timeout", "Gateway Timeout"),
}


def _problem_response(
    problem: ProblemDetail,
    headers: dict[str, str] | None = None,
) -> Response:
    from app.core.json import ORJSONResponse

    merged = {"X-Trace-Id": problem.trace_id} if problem.trace_id else {}
    merged.update(headers or {})
    return ORJSONResponse(
        status_code=problem.status,
        content=problem.model_dump(),
        media_type=PROBLEM_JSON,
        headers=merged,
    )


def register_error_handlers(app: FastAPI) -> None:
    """Attach the problem+json exception handlers to the application."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> Response:
        return _problem_response(
            ProblemDetail(
                type=exc.type_uri,
                title=exc.title,
                status=exc.status_code,
                detail=exc.detail,
                instance=request.url.path,
                trace_id=current_trace_id(),
                errors=exc.errors,
            ),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> Response:
        return _problem_response(
            ProblemDetail(
                type=f"{ERROR_TYPE_BASE}/validation-failed",
                title="Validation Error",
                status=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Request validation failed",
                instance=request.url.path,
                trace_id=current_trace_id(),
                errors=[
                    FieldError(loc=list(e["loc"]), msg=e["msg"], type=e["type"])
                    for e in exc.errors()
                ],
            )
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        slug, title = _STATUS_SLUGS.get(exc.status_code, ("error", "Error"))
        return _problem_response(
            ProblemDetail(
                type=f"{ERROR_TYPE_BASE}/{slug}",
                title=title,
                status=exc.status_code,
                detail=str(exc.detail) if exc.detail else title,
                instance=request.url.path,
                trace_id=current_trace_id(),
            ),
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        trace_id = current_trace_id()
        # exc_info gives the traceback; the message stays out of the response
        # because an unhandled exception's text is not a contract and may
        # contain internals.
        await logger.aerror(
            "unhandled_exception",
            exc_info=exc,
            exc_type=type(exc).__name__,
            path=request.url.path,
            method=request.method,
            trace_id=trace_id,
        )
        return _problem_response(
            ProblemDetail(
                type=f"{ERROR_TYPE_BASE}/internal",
                title="Internal Server Error",
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An unexpected error occurred. Quote the trace_id when reporting it.",
                instance=request.url.path,
                trace_id=trace_id,
            )
        )


__all__ = [
    "PROBLEM_JSON",
    "AppError",
    "ConflictError",
    "FieldError",
    "ForbiddenError",
    "LeagueNotSyncedError",
    "NotFoundError",
    "ProblemDetail",
    "RateLimitedError",
    "ServiceUnavailableError",
    "TimeoutError",
    "UnauthorizedError",
    "UpstreamError",
    "ValidationError",
    "register_error_handlers",
]
