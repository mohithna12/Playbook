"""Regenerate the frontend's TypeScript types from the committed OpenAPI spec.

Types are generated from ``openapi/spec.json`` rather than from a running
server, so the artifact the frontend compiles against is exactly the one CI
diffs -- there is no window where the two disagree.

Skips with a message rather than failing when node_modules is absent: a
backend-only checkout should still be able to run ``make openapi``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = PROJECT_ROOT / "openapi" / "spec.json"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
OUTPUT_PATH = FRONTEND_DIR / "src" / "lib" / "api" / "types.ts"


def main() -> int:
    if not SPEC_PATH.exists():
        print(f"{SPEC_PATH} is missing. Run scripts/generate_openapi.py first.")
        return 1

    if not (FRONTEND_DIR / "node_modules").exists():
        print("frontend/node_modules is absent; skipping. Run `make frontend-install` first.")
        return 0

    npm = shutil.which("npm")
    if npm is None:
        print("npm is not on PATH; skipping TypeScript client generation.")
        return 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [npm, "run", "generate:api"],
        cwd=FRONTEND_DIR,
        check=False,
    )
    if result.returncode != 0:
        print("openapi-typescript failed.")
        return result.returncode

    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
