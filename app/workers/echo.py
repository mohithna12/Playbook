"""A trivial job kind, for exercising the whole pipeline end to end.

Nothing in production calls it. It exists so the enqueue -> claim -> progress
-> SSE -> terminal path can be tested without dragging in Sleeper, the model
registry, or the simulator -- and so a deployed environment can be smoke
tested with a job that cannot corrupt anything.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.domain.jobs import JobKind
from app.workers.base import JobContext, register

DEFAULT_STEPS = 3
MAX_STEPS = 20
PROGRESS_MAX = 100


@register(JobKind.ECHO)
async def echo(context: JobContext, params: dict[str, Any]) -> dict[str, Any]:
    """Report progress across a few steps, then return the message.

    ``delay_seconds`` is capped rather than trusted: this is reachable by any
    authenticated user, and an uncapped sleep would be a way to pin a worker.
    """
    message = str(params.get("message", ""))
    steps = max(1, min(int(params.get("steps", DEFAULT_STEPS)), MAX_STEPS))
    delay = max(0.0, min(float(params.get("delay_seconds", 0.0)), 1.0))

    for step in range(1, steps + 1):
        await context.check_cancelled()
        if delay:
            await asyncio.sleep(delay)
        await context.report_progress(
            pct=int(step / steps * PROGRESS_MAX), step=f"step_{step}_of_{steps}"
        )

    return {"message": message, "steps": steps}


__all__ = ["echo"]
