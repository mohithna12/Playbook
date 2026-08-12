"""Server-Sent Events: wire formatting and the job event channel.

SSE rather than WebSockets because the traffic is one-directional
(server -> client), it crosses proxies and load balancers with no upgrade
negotiation, and browsers reconnect on their own (RFC 12.4).

Worker and API run in different processes, and the client's connection can
land on any API pod, so progress travels over a Redis pub/sub channel per job
(``job:{id}:events``) rather than in-process. Whichever pod holds the
connection subscribes; the publishing worker neither knows nor cares which.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import orjson
import redis.asyncio as redis
import structlog

from app.core.cache import get_redis
from app.core.json import dumps

if TYPE_CHECKING:
    import uuid
    from collections.abc import AsyncIterator

logger = structlog.get_logger()

# The ALB idle timeout is 120s. A comment line every 15s keeps the connection
# from being reaped between progress events on a slow job, and costs 8 bytes.
HEARTBEAT_INTERVAL_SECONDS = 15

# How long a subscriber waits for a message before emitting a heartbeat.
POLL_TIMEOUT_SECONDS = 1.0


def channel_for(job_id: uuid.UUID | str) -> str:
    return f"job:{job_id}:events"


def format_event(event: str, data: Any, event_id: str | None = None) -> str:
    """Render one SSE frame.

    The trailing blank line is the message delimiter -- without it the client
    buffers the frame forever waiting for more, which looks exactly like a
    hung server.
    """
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {dumps(data).decode()}")
    return "\n".join(lines) + "\n\n"


def heartbeat() -> str:
    """A comment frame. Clients ignore it; proxies see traffic."""
    return ": ping\n\n"


async def publish(job_id: uuid.UUID | str, event: str, data: Any) -> None:
    """Publish a job event. Never raises.

    A failed publish costs the client a live update, not the job -- the work
    continues and the terminal state is still readable by polling. Letting a
    Redis blip kill a running import would be a far worse trade.
    """
    try:
        await get_redis().publish(channel_for(job_id), dumps({"event": event, "data": data}))
    except redis.RedisError as exc:
        await logger.awarning("sse_publish_failed", job_id=str(job_id), error=str(exc))


async def subscribe(job_id: uuid.UUID | str) -> AsyncIterator[tuple[str, Any]]:
    """Yield ``(event, data)`` for a job, or nothing if Redis is unavailable.

    Yields nothing rather than raising: the caller falls back to polling the
    job row, so a Redis outage degrades live progress to eventual progress
    instead of failing the request.
    """
    try:
        client = get_redis()
        pubsub = client.pubsub()
        await pubsub.subscribe(channel_for(job_id))
    except redis.RedisError as exc:
        await logger.awarning("sse_subscribe_failed", job_id=str(job_id), error=str(exc))
        return

    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=POLL_TIMEOUT_SECONDS
            )
            if message is None:
                # Timeout, not end-of-stream: hand control back so the caller
                # can emit a heartbeat and re-check the job's status.
                yield ("_idle", None)
                continue
            try:
                payload = orjson.loads(message["data"])
            except orjson.JSONDecodeError:
                await logger.awarning("sse_message_undecodable", job_id=str(job_id))
                continue
            yield (payload.get("event", "message"), payload.get("data"))
    finally:
        # pragma: no cover - teardown only
        with contextlib.suppress(redis.RedisError, RuntimeError):
            await pubsub.aclose()  # type: ignore[attr-defined]  # runtime has it; stubs lag


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "channel_for",
    "format_event",
    "heartbeat",
    "publish",
    "subscribe",
]
