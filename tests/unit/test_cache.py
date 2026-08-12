"""Redis cache wrapper.

The claim under test is that the cache is an optimization, never a dependency:
if Redis is unreachable, requests get slower, not broken. Every read, write,
and delete swallows connection errors, and a read reports a miss.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
import redis.asyncio as redis

from app.core import cache


class FakeRedis:
    """Just enough Redis for the wrapper: get, set, delete, ping."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += self.store.pop(key, None) is not None
        return removed

    async def ping(self) -> bool:
        return True


class BrokenRedis:
    """Every operation fails the way an unreachable server does."""

    async def get(self, key: str) -> bytes | None:
        raise redis.ConnectionError("redis is down")

    async def set(self, *args: Any, **kwargs: Any) -> None:
        raise redis.ConnectionError("redis is down")

    async def delete(self, *keys: str) -> int:
        raise redis.ConnectionError("redis is down")

    async def ping(self) -> bool:
        raise redis.ConnectionError("redis is down")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()
    monkeypatch.setattr(cache, "get_redis", lambda: client)
    return client


@pytest.fixture
def broken(monkeypatch: pytest.MonkeyPatch) -> BrokenRedis:
    client = BrokenRedis()
    monkeypatch.setattr(cache, "get_redis", lambda: client)
    return client


class TestKeys:
    def test_keys_are_namespaced_and_versioned(self) -> None:
        assert cache.cache_key("meta", "nfl-state") == "fai:v1:meta:nfl-state"

    def test_parts_are_joined_with_colons(self) -> None:
        assert cache.cache_key("proj", 2026, 8, "abc") == "fai:v1:proj:2026:8:abc"

    def test_the_version_segment_is_the_global_invalidation_lever(self) -> None:
        """Bumping CACHE_VERSION must retire every key at once."""
        assert cache.CACHE_VERSION in cache.cache_key("x", 1)


class TestRoundTrip:
    async def test_a_miss_returns_none(self, fake: FakeRedis) -> None:
        assert await cache.get_json("fai:v1:x:missing") is None

    async def test_written_values_read_back(self, fake: FakeRedis) -> None:
        await cache.set_json("fai:v1:x:1", {"a": 1, "b": [2, 3]}, ttl_seconds=60)
        assert await cache.get_json("fai:v1:x:1") == {"a": 1, "b": [2, 3]}

    async def test_the_ttl_is_passed_through(self, fake: FakeRedis) -> None:
        await cache.set_json("fai:v1:x:2", {"a": 1}, ttl_seconds=300)
        assert fake.ttls["fai:v1:x:2"] == 300

    async def test_domain_types_survive_the_round_trip(self, fake: FakeRedis) -> None:
        """The cache uses the same encoder as responses, so Decimal works."""
        await cache.set_json(
            "fai:v1:x:3",
            {"points": Decimal("12.34"), "id": uuid.uuid4(), "d": dt.date(2026, 9, 7)},
            ttl_seconds=60,
        )
        value = await cache.get_json("fai:v1:x:3")
        assert value is not None
        assert value["points"] == 12.34
        assert value["d"] == "2026-09-07"

    async def test_delete_removes_a_key(self, fake: FakeRedis) -> None:
        await cache.set_json("fai:v1:x:4", {"a": 1}, ttl_seconds=60)
        await cache.delete("fai:v1:x:4")
        assert await cache.get_json("fai:v1:x:4") is None

    async def test_delete_with_no_keys_is_a_noop(self, fake: FakeRedis) -> None:
        await cache.delete()
        assert fake.store == {}


class TestUndecodableValues:
    async def test_a_value_written_under_another_schema_is_dropped(self, fake: FakeRedis) -> None:
        """Serving or repeatedly failing on it is worse than evicting it."""
        fake.store["fai:v1:x:bad"] = b"not json at all"

        assert await cache.get_json("fai:v1:x:bad") is None
        assert "fai:v1:x:bad" not in fake.store


class TestFailsOpen:
    async def test_a_read_against_a_dead_redis_reports_a_miss(self, broken: BrokenRedis) -> None:
        assert await cache.get_json("fai:v1:x:1") is None

    async def test_a_write_against_a_dead_redis_does_not_raise(self, broken: BrokenRedis) -> None:
        await cache.set_json("fai:v1:x:1", {"a": 1}, ttl_seconds=60)

    async def test_a_delete_against_a_dead_redis_does_not_raise(self, broken: BrokenRedis) -> None:
        await cache.delete("fai:v1:x:1")

    async def test_ping_reports_false_rather_than_raising(self, broken: BrokenRedis) -> None:
        """`/ready` turns this into a 503; it must not turn it into a 500."""
        assert await cache.ping() is False

    async def test_ping_reports_true_when_redis_answers(self, fake: FakeRedis) -> None:
        assert await cache.ping() is True
