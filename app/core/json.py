"""orjson response class.

FastAPI's default ``JSONResponse`` uses the stdlib encoder, which is roughly an
order of magnitude slower and -- more importantly here -- cannot serialize
``Decimal``, ``UUID``, or ``datetime`` without a custom default hook. Scoring
arithmetic is ``Decimal`` end to end (RFC 11.1), so every projection payload
would otherwise need manual coercion at the boundary.

Decimals serialize as JSON *numbers*, not strings: the OpenAPI schema types
them as numbers, and emitting ``"12.34"`` where the contract promises ``12.34``
breaks generated clients.
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from typing import Any

import orjson
from fastapi.responses import JSONResponse

_OPTIONS = orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS


def _default(obj: Any) -> Any:
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, dt.date | dt.time):
        return obj.isoformat()
    if isinstance(obj, set | frozenset):
        return list(obj)
    raise TypeError(f"Type is not JSON serializable: {type(obj).__name__}")


def dumps(obj: Any) -> bytes:
    return orjson.dumps(obj, default=_default, option=_OPTIONS)


class ORJSONResponse(JSONResponse):
    """JSON responses encoded with orjson."""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return dumps(content)


__all__ = ["ORJSONResponse", "dumps"]
