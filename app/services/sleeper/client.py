"""HTTP client for the Sleeper API.

Sleeper's read API is public and unauthenticated, which is why the MVP stores
no third-party user credentials at all (RFC 20.3). What it does have is a rate
limit of roughly 1000 requests/minute per IP, shared by every worker pod, and
the usual third-party failure modes.

Three layers of protection, each for a different failure:

* a **token bucket** paces our own calls so eight pods running imports cannot
  collectively exceed the upstream budget;
* **retries with backoff** handle the transient 5xx and the honest 429;
* a **circuit breaker** stops us queueing behind an upstream that is simply
  down, and stops us adding to its load.

Errors are mapped to the app's own taxonomy at this boundary, so nothing
downstream has to know what an ``httpx.HTTPStatusError`` is.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from app.core.circuit_breaker import CircuitBreaker
from app.core.errors import NotFoundError, UpstreamError
from app.core.token_bucket import TokenBucket

if TYPE_CHECKING:
    from types import TracebackType

logger = structlog.get_logger()

BASE_URL = "https://api.sleeper.app/v1"

# 800/min against Sleeper's ~1000, leaving headroom for the nightly contract
# test and any manual poking while an import is running (RFC 8.2).
RATE_LIMIT_PER_MINUTE = 800
BUCKET_CAPACITY = 100

# A full import is ~25 calls; a per-league semaphore of 5 keeps one league's
# burst from starving another's while still finishing quickly.
DEFAULT_CONCURRENCY = 5

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 8.0

HTTP_TOO_MANY_REQUESTS = 429
HTTP_NOT_FOUND = 404
HTTP_SERVER_ERROR_FLOOR = 500


class SleeperClient:
    """A pooled, rate-limited, breaker-protected client for one Sleeper host."""

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            # Sleeper is one host, so a small pool is plenty and keeps
            # connections warm across the ~25 calls of an import.
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers={"User-Agent": "playbook/0.1 (+https://github.com/mohithna12/Playbook)"},
            transport=transport,
            follow_redirects=True,
        )
        self._bucket = TokenBucket(
            "sleeper:global", per_minute=RATE_LIMIT_PER_MINUTE, capacity=BUCKET_CAPACITY
        )
        self._breaker = CircuitBreaker("sleeper")
        self._semaphore = asyncio.Semaphore(concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> SleeperClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def get(self, path: str) -> Any:
        """GET ``path`` and return the decoded JSON.

        Raises :class:`NotFoundError` for a 404 -- an unknown league is the
        caller's problem, not the upstream's -- and :class:`UpstreamError` for
        anything that says Sleeper is unhealthy. The distinction is what keeps
        a mistyped league id from tripping the circuit breaker.
        """
        await self._breaker.check()

        async with self._semaphore:
            # Pace against the shared budget before spending a connection.
            # A timeout here means the cluster is saturated; proceeding is
            # better than failing the import, and Sleeper will 429 us if we
            # are genuinely over.
            await self._bucket.acquire()
            return await self._get_with_retries(path)

    async def _get_with_retries(self, path: str) -> Any:
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._client.get(path)
            except httpx.TimeoutException as exc:
                last_error = exc
                await self._breaker.record_failure()
                await logger.awarning("sleeper_timeout", path=path, attempt=attempt)
            except httpx.HTTPError as exc:
                last_error = exc
                await self._breaker.record_failure()
                await logger.awarning(
                    "sleeper_transport_error", path=path, attempt=attempt, error=str(exc)
                )
            else:
                outcome = await self._handle(response, path, attempt)
                if outcome is not _RETRY:
                    return outcome
                last_error = UpstreamError(f"Sleeper returned {response.status_code}")

            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(self._backoff(attempt))

        raise UpstreamError(
            f"Sleeper request to {path} failed after {MAX_ATTEMPTS} attempts: {last_error}"
        )

    async def _handle(self, response: httpx.Response, path: str, attempt: int) -> Any:
        """Turn one response into JSON, a raised error, or a retry signal."""
        status = response.status_code

        if status == HTTP_NOT_FOUND:
            # Not an upstream health signal: the resource does not exist.
            # Recorded as a success so it cannot trip the breaker.
            await self._breaker.record_success()
            raise NotFoundError(f"Sleeper has no resource at {path}")

        if status == HTTP_TOO_MANY_REQUESTS:
            await self._breaker.record_failure()
            retry_after = self._retry_after(response)
            await logger.awarning(
                "sleeper_rate_limited", path=path, attempt=attempt, retry_after=retry_after
            )
            if retry_after is not None and attempt < MAX_ATTEMPTS:
                await asyncio.sleep(min(retry_after, BACKOFF_MAX_SECONDS))
            return _RETRY

        if status >= HTTP_SERVER_ERROR_FLOOR:
            await self._breaker.record_failure()
            await logger.awarning("sleeper_server_error", path=path, status=status)
            return _RETRY

        if status >= HTTP_TOO_MANY_REQUESTS or not response.is_success:
            # Any other 4xx: our request is wrong and retrying will not fix it.
            await self._breaker.record_success()
            raise UpstreamError(f"Sleeper rejected the request to {path} with {status}")

        try:
            payload = response.json()
        except ValueError as exc:
            # A 200 whose body is not JSON is a broken upstream, not a broken
            # request -- Sleeper has served HTML error pages under load.
            await self._breaker.record_failure()
            raise UpstreamError(f"Sleeper returned a non-JSON body for {path}") from exc

        await self._breaker.record_success()
        return payload

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            # The HTTP-date form. Not worth parsing; the caller's own backoff
            # covers it.
            return None

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with full jitter.

        Jitter matters more than the exponent here: without it, every worker
        that failed at the same moment retries at the same moment, and the
        upstream sees the same thundering herd that knocked it over.
        """
        ceiling = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_MAX_SECONDS)
        return random.uniform(0, ceiling)  # noqa: S311 - jitter, not cryptography


# Sentinel distinguishing "retry this" from a legitimate ``None`` body, which
# Sleeper returns for an empty week of transactions.
_RETRY: Any = object()


__all__ = [
    "BASE_URL",
    "DEFAULT_CONCURRENCY",
    "MAX_ATTEMPTS",
    "RATE_LIMIT_PER_MINUTE",
    "SleeperClient",
]
