"""The single application instance the contract suite drives.

It lives in its own module so ``conftest.py`` can override its dependencies
without importing the test module -- importing that would pull Schemathesis and
Hypothesis in during conftest collection, which Hypothesis warns about.
"""

from __future__ import annotations

from app.main import create_app

# Built at import time, not in a fixture: @schema.parametrize() runs at
# collection, before any fixture exists.
contract_app = create_app()

__all__ = ["contract_app"]
