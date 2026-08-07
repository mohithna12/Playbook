"""OpenTelemetry setup and trace-context access.

The one thing the rest of the codebase needs from this module is
:func:`current_trace_id`. It is what ties a user-reported problem to a log
query: the same hex string appears in the RFC 7807 body, the ``X-Trace-Id``
response header, and every structlog line emitted while handling the request
(RFC 9.3).

Instrumentation is opt-in via ``OTEL_EXPORTER_OTLP_ENDPOINT``. With no
endpoint configured the SDK is still installed but exports nowhere, so spans
and trace IDs exist in development and tests without requiring a collector.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, TraceIdRatioBased

from app.core.config import get_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

INVALID_TRACE_ID = 0

_configured = False


def current_trace_id() -> str:
    """The active trace ID as 32 lowercase hex chars, or ``""`` outside a span.

    Empty rather than a fabricated ID: a trace_id that does not resolve in the
    tracing backend is worse than an absent one, because it sends whoever is
    debugging on a search for a trace that was never recorded.
    """
    span_context = trace.get_current_span().get_span_context()
    if span_context.trace_id == INVALID_TRACE_ID:
        return ""
    return format(span_context.trace_id, "032x")


def setup_telemetry() -> None:
    """Install the tracer provider. Idempotent -- safe to call per worker boot."""
    global _configured
    if _configured:
        return

    settings = get_settings()
    resource = Resource.create(
        {
            "service.name": "fantasyai-api",
            "service.version": settings.service_version,
            "deployment.environment": settings.environment,
        }
    )

    # Head sampling, parent-respecting: if the frontend or ALB already decided
    # to sample a request, honour that decision so a trace is never half
    # recorded. Sample everything outside production -- the volume is trivial
    # and a missing dev trace costs more than it saves.
    sampler = (
        ParentBased(root=TraceIdRatioBased(settings.otel_sample_ratio))
        if settings.is_production
        else ALWAYS_ON
    )

    provider = TracerProvider(resource=resource, sampler=sampler)

    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    trace.set_tracer_provider(provider)
    _configured = True


def instrument_app(app: FastAPI) -> None:
    """Attach ASGI, SQLAlchemy, Redis, and httpx instrumentation."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    from app.core.db import get_engine

    # Health probes fire every few seconds from kubelet and would otherwise
    # dominate the trace volume without ever being looked at.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready,metrics")
    SQLAlchemyInstrumentor().instrument(engine=get_engine().sync_engine)
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()


__all__ = ["current_trace_id", "instrument_app", "setup_telemetry"]
