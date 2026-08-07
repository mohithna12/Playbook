"""Structured logging configuration.

Uses structlog with JSON output. PII redaction is applied via a processor
that strips known-sensitive fields before emission.
"""

import logging
import sys
from typing import Any

import structlog

PII_DENYLIST = frozenset(
    {
        "email",
        "password",
        "token",
        "authorization",
        "cookie",
        "secret",
        "api_key",
        "auth_subject",
    }
)


def _redact_pii(
    _logger: Any, _method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Replace values of known-sensitive keys with '[REDACTED]'."""
    for key in list(event_dict.keys()):
        if key.lower() in PII_DENYLIST:
            event_dict[key] = "[REDACTED]"
    return event_dict


def setup_logging(log_level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog and stdlib logging."""
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _redact_pii,
    ]

    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    # Quiet noisy loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
