"""Job kinds and their execution policy.

The policy table drives three separate decisions -- how long a job may run,
how many times it may be retried, and whether the reaper may replay it -- so a
wrong entry misbehaves in a way that only shows up under failure. These pin
the values the RFC specifies.
"""

from __future__ import annotations

import pytest

from app.domain import jobs


class TestPolicyTable:
    def test_every_kind_has_a_policy(self) -> None:
        """A kind with no policy would be invisible to the reaper."""
        assert set(jobs.POLICIES) == set(jobs.JobKind)

    @pytest.mark.parametrize("kind", list(jobs.JobKind))
    def test_timeouts_and_attempts_are_positive(self, kind: jobs.JobKind) -> None:
        policy = jobs.policy_for(kind)
        assert policy.timeout_seconds > 0
        assert policy.max_attempts >= 1

    @pytest.mark.parametrize("kind", list(jobs.JobKind))
    def test_the_estimate_fits_inside_the_timeout(self, kind: jobs.JobKind) -> None:
        """An estimate above the timeout means the kind is reaped by design."""
        policy = jobs.policy_for(kind)
        assert policy.estimated_seconds < policy.timeout_seconds

    def test_rfc_timeouts(self) -> None:
        """RFC 7.2's table. A silent change here changes reaping behaviour."""
        assert jobs.policy_for(jobs.JobKind.LEAGUE_FULL_IMPORT).timeout_seconds == 180
        assert jobs.policy_for(jobs.JobKind.LEAGUE_INCREMENTAL_SYNC).timeout_seconds == 60
        assert jobs.policy_for(jobs.JobKind.SIMULATION_RUN).timeout_seconds == 30
        assert jobs.policy_for(jobs.JobKind.TRADE_ANALYZE).timeout_seconds == 60
        assert jobs.policy_for(jobs.JobKind.EXPLANATION_GENERATE).timeout_seconds == 45
        assert jobs.policy_for(jobs.JobKind.CACHE_WARM_LEAGUE).timeout_seconds == 120

    def test_explanation_is_the_only_non_idempotent_kind(self) -> None:
        """Idempotency is what makes replaying a reaped job safe."""
        non_idempotent = {k for k, p in jobs.POLICIES.items() if not p.idempotent}
        assert non_idempotent == {jobs.JobKind.EXPLANATION_GENERATE}

    def test_retries_only_where_the_rfc_allows_them(self) -> None:
        assert jobs.policy_for(jobs.JobKind.LEAGUE_FULL_IMPORT).max_attempts == 3
        assert jobs.policy_for(jobs.JobKind.SIMULATION_RUN).max_attempts == 1
        assert jobs.policy_for(jobs.JobKind.TRADE_ANALYZE).max_attempts == 1

    def test_an_unregistered_kind_raises(self) -> None:
        """Not a permissive default -- an unknown kind must not run untimed."""
        with pytest.raises(ValueError, match="not a valid JobKind"):
            jobs.policy_for("nope")


class TestTerminalStatuses:
    @pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "PARTIAL", "CANCELLED"])
    def test_terminal_statuses(self, status: str) -> None:
        assert jobs.is_terminal(status)

    @pytest.mark.parametrize("status", ["QUEUED", "RUNNING"])
    def test_active_statuses_are_not_terminal(self, status: str) -> None:
        assert not jobs.is_terminal(status)

    def test_partial_counts_as_terminal(self) -> None:
        """A partial import is finished, not still running -- the stream ends."""
        assert jobs.is_terminal("PARTIAL")


class TestHeartbeatWindow:
    def test_the_timeout_is_several_intervals(self) -> None:
        """Scheduling jitter must not look like a dead worker."""
        assert jobs.HEARTBEAT_TIMEOUT_SECONDS >= jobs.HEARTBEAT_INTERVAL_SECONDS * 3
