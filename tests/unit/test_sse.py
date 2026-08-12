"""SSE wire format.

The framing is fiddly in a way that fails silently: a client given a
well-formed-looking frame with the wrong delimiter simply waits, which is
indistinguishable from a slow server. These assert the bytes.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.core import sse


class TestFraming:
    def test_a_frame_ends_with_a_blank_line(self) -> None:
        """The delimiter. Without it the client buffers the event forever."""
        assert sse.format_event("progress", {"pct": 10}).endswith("\n\n")

    def test_a_frame_carries_event_and_data_lines(self) -> None:
        frame = sse.format_event("progress", {"pct": 10, "step": "x"})
        lines = frame.strip().split("\n")

        assert lines[0] == "event: progress"
        assert lines[1] == 'data: {"pct":10,"step":"x"}'

    def test_data_is_a_single_line(self) -> None:
        """A newline inside data would split one event into two malformed ones."""
        frame = sse.format_event("complete", {"status": "SUCCEEDED", "result_ref": {"a": 1}})
        data_lines = [ln for ln in frame.strip().split("\n") if ln.startswith("data: ")]

        assert len(data_lines) == 1

    def test_an_event_id_is_emitted_first_when_given(self) -> None:
        """Clients replay from `Last-Event-ID`, so the id must precede the event."""
        frame = sse.format_event("progress", {"pct": 1}, event_id="7")
        assert frame.startswith("id: 7\nevent: progress\n")

    def test_no_id_line_when_none_is_given(self) -> None:
        assert "id:" not in sse.format_event("progress", {"pct": 1})

    def test_domain_types_serialize(self) -> None:
        """The same encoder as responses, so Decimal is a number, not a string."""
        frame = sse.format_event("complete", {"points": Decimal("12.34")})
        assert 'data: {"points":12.34}' in frame

    def test_heartbeat_is_a_comment_frame(self) -> None:
        """Clients ignore comments; proxies count them as traffic."""
        assert sse.heartbeat() == ": ping\n\n"


class TestChannel:
    def test_the_channel_is_namespaced_per_job(self) -> None:
        job_id = uuid.uuid4()
        assert sse.channel_for(job_id) == f"job:{job_id}:events"

    def test_string_and_uuid_forms_agree(self) -> None:
        """The worker publishes with a UUID, the API subscribes with one too."""
        job_id = uuid.uuid4()
        assert sse.channel_for(job_id) == sse.channel_for(str(job_id))

    def test_channels_do_not_collide_across_jobs(self) -> None:
        assert sse.channel_for(uuid.uuid4()) != sse.channel_for(uuid.uuid4())
