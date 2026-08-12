"""The single application instance the contract suite drives.

It lives in its own module so ``conftest.py`` can override its dependencies
without importing the test module -- importing that would pull Schemathesis and
Hypothesis in during conftest collection, which Hypothesis warns about.

Clerk is configured with placeholder values before the app is built. Without
them ``get_verifier()`` cannot construct, and every authenticated endpoint
answers 503 "authentication is not configured" -- a real response, but the
wrong one to be checking a contract against. With them, Schemathesis's
generated bearer tokens fail verification and produce the 401 the spec
documents, which is the behaviour a client will actually meet.
"""

from __future__ import annotations

import os

# Set before app.core.config is imported: get_settings() is cached for the
# process, so a later assignment would have no effect.
os.environ.setdefault("CLERK_JWKS_URL", "https://clerk.contract-tests.invalid/jwks.json")
os.environ.setdefault("CLERK_ISSUER", "https://clerk.contract-tests.invalid")
os.environ.setdefault("CLERK_AUDIENCE", "playbook-contract-tests")

from app.main import create_app

# Built at import time, not in a fixture: @schema.parametrize() runs at
# collection, before any fixture exists.
contract_app = create_app()

__all__ = ["contract_app"]
