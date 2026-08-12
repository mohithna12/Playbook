"""Job endpoints: poll, stream, cancel.

Every async feature -- import, simulation, trade analysis, explanation --
returns a job handle and is then followed here, so this is built once rather
than four times.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, DbSession, ReadRateLimit
from app.schemas.job import JobStatusResponse
from app.services.job import JobService

router = APIRouter(tags=["jobs"], dependencies=[ReadRateLimit])

# A finished job's status is immutable, so it may be cached hard. An active
# one changes constantly and must not be, or a client polls a stale row.
TERMINAL_CACHE_CONTROL = "private, max-age=3600"
ACTIVE_CACHE_CONTROL = "no-store"


@router.get("/jobs/{job_id}", summary="Poll job status")
async def get_job(
    job_id: uuid.UUID,
    response: Response,
    session: DbSession,
    user: CurrentUser,
) -> JobStatusResponse:
    """Current state of a job. 404 if it is not the caller's."""
    from app.domain.jobs import is_terminal

    job = await JobService(session).get(job_id, user_id=user.id)
    response.headers["Cache-Control"] = (
        TERMINAL_CACHE_CONTROL if is_terminal(job.status) else ACTIVE_CACHE_CONTROL
    )
    return job


@router.get(
    "/jobs/{job_id}/stream",
    summary="Stream job progress (SSE)",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "An event stream of `progress` events terminated by `complete`.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def stream_job(
    job_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> StreamingResponse:
    """Progress events until the job reaches a terminal state.

    ``X-Accel-Buffering: no`` matters as much as the content type: a buffering
    proxy will happily hold an event stream until it has "enough" to send,
    which turns real-time progress into one burst at the end.
    """
    service = JobService(session)
    return StreamingResponse(
        service.stream(job_id, user_id=user.id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete(
    "/jobs/{job_id}",
    summary="Cancel a job",
    status_code=status.HTTP_200_OK,
)
async def cancel_job(
    job_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> JobStatusResponse:
    """Cancel a queued or running job.

    A queued job never starts. A running one stops at its handler's next
    checkpoint -- cooperative rather than pre-emptive, so a job is not killed
    halfway through a write.
    """
    return await JobService(session).cancel(job_id, user_id=user.id)


__all__ = ["router"]
