"""orjson serialization.

The Decimal case is the load-bearing one. Scoring arithmetic is ``Decimal``
end to end (RFC 11.1) and the OpenAPI schema types those fields as numbers, so
emitting ``"12.34"`` where the contract promises ``12.34`` breaks every
generated client -- quietly, because the response still parses as JSON.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import orjson
import pytest

from app.core.json import ORJSONResponse, dumps


class TestDecimal:
    def test_decimal_serializes_as_a_json_number(self) -> None:
        assert dumps({"points": Decimal("12.34")}) == b'{"points":12.34}'

    def test_a_decimal_is_not_quoted(self) -> None:
        """The failure mode is a valid-looking string where a number belongs."""
        assert orjson.loads(dumps({"p": Decimal("0.5")}))["p"] == 0.5
        assert b'"' not in dumps({"p": Decimal("0.5")}).split(b":")[1]

    def test_whole_decimals_survive(self) -> None:
        assert orjson.loads(dumps({"p": Decimal("7")}))["p"] == 7


class TestOtherTypes:
    def test_uuid_serializes_as_a_string(self) -> None:
        value = uuid.uuid4()
        assert orjson.loads(dumps({"id": value}))["id"] == str(value)

    def test_date_serializes_as_iso8601(self) -> None:
        assert orjson.loads(dumps({"d": dt.date(2026, 9, 7)}))["d"] == "2026-09-07"

    def test_time_serializes_as_iso8601(self) -> None:
        assert orjson.loads(dumps({"t": dt.time(13, 25)}))["t"] == "13:25:00"

    def test_datetime_is_handled_natively_by_orjson(self) -> None:
        moment = dt.datetime(2026, 9, 7, 13, 25, tzinfo=dt.UTC)
        assert orjson.loads(dumps({"at": moment}))["at"].startswith("2026-09-07T13:25")

    def test_sets_serialize_as_arrays(self) -> None:
        assert sorted(orjson.loads(dumps({"s": {"b", "a"}}))["s"]) == ["a", "b"]

    def test_an_unserializable_type_raises_rather_than_guessing(self) -> None:
        class Opaque:
            pass

        with pytest.raises(TypeError, match="Opaque"):
            dumps({"x": Opaque()})


class TestResponse:
    def test_the_response_class_uses_the_same_encoder(self) -> None:
        response = ORJSONResponse(content={"points": Decimal("1.5")})
        assert response.body == b'{"points":1.5}'

    def test_media_type_stays_application_json(self) -> None:
        assert ORJSONResponse(content={}).media_type == "application/json"
