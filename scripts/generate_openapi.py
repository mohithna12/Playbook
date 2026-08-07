"""Write the OpenAPI spec to ``openapi/spec.json``.

The spec is committed and CI re-runs this to check the working tree matches
(``--check``). That turns "someone changed a response shape" from something a
frontend engineer discovers at runtime into a diff in code review.

Serialization is deterministic -- sorted keys, fixed indent, trailing newline --
so the diff shows the contract change and nothing else.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.main import create_app

SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi" / "spec.json"


def render() -> str:
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the committed spec differs from the current app.",
    )
    args = parser.parse_args()

    rendered = render()

    if args.check:
        if not SPEC_PATH.exists():
            print(f"{SPEC_PATH} is missing. Run: make openapi")
            return 1
        if SPEC_PATH.read_text() != rendered:
            print(
                f"{SPEC_PATH} is out of date with the application.\n"
                "Run `make openapi` and commit the result."
            )
            return 1
        print(f"{SPEC_PATH} is up to date.")
        return 0

    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPEC_PATH.write_text(rendered)
    print(f"Wrote {SPEC_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
