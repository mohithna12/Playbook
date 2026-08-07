"""Cross-cutting HTTP middleware: trace context, ETags, rate-limit headers.

Ordering matters and is set in :mod:`app.main`. Starlette runs middleware in
reverse registration order on the way in, so the trace-binding middleware is
registered last to run first -- everything downstream, including error
handlers, then has a trace ID to attach.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, StreamingResponse
from starlette.status import HTTP_200_OK as HTTP_OK
from starlette.status import HTTP_304_NOT_MODIFIED

from app.core.telemetry import current_trace_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from starlette.requests import Request

logger = structlog.get_logger()

API_VERSION = "v1"

# Bodies above this are not buffered for ETag computation. SSE streams and
# large exports must not be pulled into memory to hash something no client
# will conditionally request anyway.
MAX_ETAG_BODY_BYTES = 1024 * 1024


class TraceContextMiddleware(BaseHTTPMiddleware):
    """Bind ``trace_id`` into the log context and echo it back to the client.

    Binding into structlog's contextvars means every log line emitted while
    handling this request carries the trace ID without any call site passing
    it, which is the whole point: a user quotes the ID from an error response
    and one query returns the request's entire log trail.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        structlog.contextvars.clear_contextvars()
        trace_id = current_trace_id()
        structlog.contextvars.bind_contextvars(
            trace_id=trace_id,
            path=request.url.path,
            method=request.method,
        )
        response = await call_next(request)
        if trace_id:
            response.headers["X-Trace-Id"] = trace_id
        response.headers["X-API-Version"] = API_VERSION
        return response


class RateLimitHeaderMiddleware(BaseHTTPMiddleware):
    """Copy the request's rate-limit verdict onto the response.

    The dependency stashes its result on ``request.state``; a 429 already
    carries the headers from the exception, and this covers the successful
    responses that also need to advertise remaining budget (RFC 12.1).
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        result = getattr(request.state, "rate_limit", None)
        if result is not None:
            for header, value in result.headers().items():
                response.headers.setdefault(header, value)
        return response


class ETagMiddleware(BaseHTTPMiddleware):
    """Strong ETags on GET responses, with ``304`` on a matching request.

    The saving is bandwidth, not database work -- the handler has already run
    by the time the hash is computed. That is the correct trade here: these
    payloads are projection tables that clients poll, and the alternative
    (a cache lookup before the handler) would need a key that captures the
    caller's league membership, which is exactly the kind of cache key that
    goes wrong quietly.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)

        if request.method != "GET" or response.status_code != HTTP_OK:
            return response
        if "etag" in response.headers:
            return response
        # Server-Sent Events must not be buffered -- that is the whole point of
        # the endpoint. Checked by media type because everything reaching a
        # BaseHTTPMiddleware is a streaming response (below).
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            return response

        # `call_next` always hands back a streaming response, even when the
        # route returned a plain one -- so the body has to be drained to hash
        # it, and a new response built. Checking for `.body` here (the obvious
        # implementation) silently disables ETags entirely, because the
        # attribute is never present.
        body = b""
        oversized = False
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            body += chunk if isinstance(chunk, bytes) else chunk.encode()
            if len(body) > MAX_ETAG_BODY_BYTES:
                oversized = True
                break

        if oversized:
            # Give up on the ETag, but the drained bytes still have to be
            # delivered -- replay what was read, then the rest of the stream.
            return StreamingResponse(
                _replay(body, response.body_iterator),  # type: ignore[attr-defined]
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        headers = dict(response.headers)
        if body:
            etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
            headers["etag"] = etag

            if request.headers.get("if-none-match") == etag:
                not_modified = Response(status_code=HTTP_304_NOT_MODIFIED)
                # RFC 9110: a 304 carries the headers that would have been sent
                # on a 200 for the same resource, so a revalidating cache does
                # not lose them.
                for name in ("etag", "cache-control", "x-trace-id", "x-api-version"):
                    if name in headers:
                        not_modified.headers[name] = headers[name]
                return not_modified

        # Content-Length is recomputed by the Response constructor.
        headers.pop("content-length", None)
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )


async def _replay(buffered: bytes, rest: AsyncIterator[bytes | str]) -> AsyncIterator[bytes]:
    """Yield already-drained bytes, then the remainder of a stream."""
    yield buffered
    async for chunk in rest:
        yield chunk if isinstance(chunk, bytes) else chunk.encode()


__all__ = [
    "API_VERSION",
    "ETagMiddleware",
    "RateLimitHeaderMiddleware",
    "TraceContextMiddleware",
]
