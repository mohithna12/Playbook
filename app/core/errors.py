"""RFC 7807 error responses and exception handlers.

All API errors return application/problem+json with a trace_id.
"""

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class ProblemDetail(BaseModel):
    """RFC 7807 problem+json response."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    instance: str = ""
    trace_id: str = ""
    errors: list[dict[str, Any]] = []


class AppError(Exception):
    """Base application error mapped to an HTTP status."""

    def __init__(
        self,
        status_code: int,
        title: str,
        detail: str,
        error_type: str = "about:blank",
    ) -> None:
        self.status_code = status_code
        self.title = title
        self.detail = detail
        self.error_type = error_type
        super().__init__(detail)


class NotFoundError(AppError):
    def __init__(self, detail: str = "Resource not found") -> None:
        super().__init__(status.HTTP_404_NOT_FOUND, "Not Found", detail)


class ConflictError(AppError):
    def __init__(self, detail: str = "Conflict") -> None:
        super().__init__(status.HTTP_409_CONFLICT, "Conflict", detail)


class ForbiddenError(AppError):
    def __init__(self, detail: str = "Forbidden") -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, "Forbidden", detail)


def _get_trace_id(request: Request) -> str:
    return request.headers.get("x-request-id", "")


def register_error_handlers(app: FastAPI) -> None:
    """Attach exception handlers to the FastAPI application."""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ProblemDetail(
                type=exc.error_type,
                title=exc.title,
                status=exc.status_code,
                detail=exc.detail,
                instance=str(request.url.path),
                trace_id=_get_trace_id(request),
            ).model_dump(),
            media_type="application/problem+json",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=ProblemDetail(
                title="Validation Error",
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Request validation failed",
                instance=str(request.url.path),
                trace_id=_get_trace_id(request),
                errors=[
                    {"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()
                ],
            ).model_dump(),
            media_type="application/problem+json",
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        import structlog

        logger = structlog.get_logger()
        await logger.aerror(
            "unhandled_exception",
            exc_type=type(exc).__name__,
            exc_detail=str(exc),
            path=str(request.url.path),
            trace_id=_get_trace_id(request),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=ProblemDetail(
                title="Internal Server Error",
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="An unexpected error occurred",
                instance=str(request.url.path),
                trace_id=_get_trace_id(request),
            ).model_dump(),
            media_type="application/problem+json",
        )
