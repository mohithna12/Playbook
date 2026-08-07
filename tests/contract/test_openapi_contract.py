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

# FastAPI emits OpenAPI 3.1; Schemathesis 3.x supports it behind a flag. The
# alternative -- downgrading the version the app declares -- would make the
# committed spec lie about itself to satisfy a test dependency.
schemathesis.experimental.OPEN_API_3_1.enable()

schema = schemathesis.openapi.from_dict(json.loads(SPEC_PATH.read_text()))


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
    response = case.call_asgi(app=contract_app)
    case.validate_response(response)
