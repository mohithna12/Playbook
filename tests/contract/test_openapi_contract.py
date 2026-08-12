"""Contract tests: the API must behave the way its OpenAPI spec claims.

Schemathesis generates requests from the committed spec and checks every
response against it. What this catches that hand-written tests do not is the
gap between the declared contract and the implementation -- a response missing
a required field, a status code the spec never mentions, an input the schema
allows but the handler rejects with a 500.

The spec is loaded from ``openapi/spec.json`` rather than from the live app, so
a stale committed spec fails here as well as in the CI diff job.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import schemathesis
from hypothesis import HealthCheck, settings

from tests.contract.app_under_test import contract_app

pytestmark = pytest.mark.contract

SPEC_PATH = Path(__file__).resolve().parent.parent.parent / "openapi" / "spec.json"

# OpenAPI 3.1 -- which FastAPI emits -- is supported natively as of
# Schemathesis 4; the 3.x releases needed an experimental opt-in.
schema = schemathesis.openapi.from_dict(json.loads(SPEC_PATH.read_text()))

# Dispatch in-process against the ASGI app rather than over a socket to a
# server the suite would have to start and wait for.
schema.app = contract_app

# /health and /ready are Kubernetes probes, not part of the contract a client
# codes against, and including them costs more than it proves. /ready reports
# 503 when a dependency is down -- correct behaviour and documented in the
# spec, but Schemathesis flags any 5xx as a server error. Wiring a live Redis
# in to avoid that is worse: Hypothesis runs each example in its own event
# loop, and a pooled redis-py connection opened on one loop raises
# "attached to a different loop" when reused from the next.
schema = schema.exclude(path_regex=r"^/(health|ready)$")


@schema.parametrize()
@settings(
    max_examples=25,
    deadline=None,
    # Every example opens a database connection; per-example timing variance is
    # not a signal about the contract.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_api_conforms_to_its_spec(case: schemathesis.Case) -> None:
    """Every generated request gets a response the spec describes."""
    case.call_and_validate()
