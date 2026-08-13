"""Sleeper's shapes -> provider DTOs, against recorded responses.

Driven by the fixtures in ``tests/fixtures/sleeper``, which preserve the
quirks that make this module necessary: points split across two fields, nulls
inside positional starter lists, transactions with null ``adds``, and a
scoring map that is a map rather than a preset.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.services.sleeper import normalizer

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "sleeper"


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class TestScoring:
    def test_the_full_rule_map_survives(self) -> None:
        """Not a preset. Every key the league actually scores must be kept."""
        raw = load("league")["scoring_settings"]

        result = normalizer.normalize_scoring(raw)

        assert result["rules"] == pytest.approx(raw)
        assert len(result["rules"]) == len(raw)

    def test_unfamiliar_keys_are_kept(self) -> None:
        """Sleeper adds categories over time; an allow-list would drop them.

        Silently dropping a category a league scores is exactly the failure
        that makes a fantasy product untrustworthy.
        """
        result = normalizer.normalize_scoring({"some_future_bonus": 2.5})

        assert result["rules"]["some_future_bonus"] == 2.5

    def test_the_envelope_is_versioned(self) -> None:
        """So a ruleset stored under old semantics can be identified later."""
        result = normalizer.normalize_scoring({"rec": 1})

        assert result["schema_version"] == normalizer.SCORING_SCHEMA_VERSION
        assert result["provider"] == "SLEEPER"

    def test_booleans_are_dropped(self) -> None:
        """Sleeper uses booleans for toggles; multiplying a stat by True is meaningless."""
        result = normalizer.normalize_scoring({"rec": 1, "some_toggle": True})

        assert "some_toggle" not in result["rules"]
        assert result["rules"]["rec"] == 1.0

    def test_non_numeric_values_are_dropped(self) -> None:
        result = normalizer.normalize_scoring({"rec": 0.5, "label": "PPR"})

        assert result["rules"] == {"rec": 0.5}

    def test_missing_scoring_yields_an_empty_ruleset(self) -> None:
        """A league with no scoring settings must not crash the import."""
        assert normalizer.normalize_scoring(None)["rules"] == {}

    def test_negative_weights_are_preserved(self) -> None:
        """Interceptions and fumbles are negative; a sign flip is silent and wrong."""
        result = normalizer.normalize_scoring({"pass_int": -2, "fum_lost": -2})

        assert result["rules"]["pass_int"] == -2.0
        assert result["rules"]["fum_lost"] == -2.0


class TestLeague:
    def test_a_recorded_league_normalizes(self) -> None:
        league = normalizer.normalize_league(load("league"))

        assert league.external_id == "1124589302838374400"
        assert league.name == "Sunday Scaries"
        assert league.season == 2026
        assert league.total_teams == 12
        assert league.previous_league_id == "986543210987654321"

    def test_the_season_is_an_int_though_sleeper_sends_a_string(self) -> None:
        """`season` arrives as "2026"; the column is SMALLINT."""
        assert normalizer.normalize_league(load("league")).season == 2026

    def test_roster_positions_keep_their_order_and_repeats(self) -> None:
        """Two RB slots is not the same as one, and order maps to starters."""
        positions = normalizer.normalize_league(load("league")).roster_positions

        assert positions[:6] == ["QB", "RB", "RB", "WR", "WR", "TE"]
        assert positions.count("BN") == 6

    def test_the_playoff_config_is_extracted(self) -> None:
        config = normalizer.normalize_league(load("league")).playoff_config

        assert config["teams"] == 6
        assert config["start_week"] == 15

    def test_a_league_with_no_name_gets_a_placeholder(self) -> None:
        payload = {**load("league"), "name": None}

        assert normalizer.normalize_league(payload).name == "Untitled league"


class TestTeams:
    def test_rosters_and_users_are_joined(self) -> None:
        teams = normalizer.normalize_teams(load("rosters"), load("users"))

        assert len(teams) == 2
        assert teams[0].external_id == "1"
        assert teams[0].owner_name == "gridiron_gary"

    def test_the_custom_team_name_wins_over_the_display_name(self) -> None:
        teams = normalizer.normalize_teams(load("rosters"), load("users"))

        assert teams[0].display_name == "Kelce's Kitchen"

    def test_split_points_are_reassembled(self) -> None:
        """fpts=1234 + fpts_decimal=56 is 1234.56, not 1234."""
        teams = normalizer.normalize_teams(load("rosters"), load("users"))

        assert teams[0].points_for == Decimal("1234.56")
        assert teams[0].points_against == Decimal("1100.04")

    def test_points_are_decimal_not_float(self) -> None:
        """These feed standings and tiebreakers; float drift is not acceptable."""
        teams = normalizer.normalize_teams(load("rosters"), load("users"))

        assert isinstance(teams[0].points_for, Decimal)

    def test_faab_is_converted_from_used_to_remaining(self) -> None:
        """Sleeper reports spend; a manager cares what is left."""
        teams = normalizer.normalize_teams(load("rosters"), load("users"))

        assert teams[0].faab_remaining == 65  # 100 budget - 35 used

    def test_an_orphaned_roster_still_becomes_a_team(self) -> None:
        """Abandoned teams are real, and their players and matchups matter."""
        teams = normalizer.normalize_teams(load("rosters"), load("users"))

        orphan = teams[1]
        assert orphan.external_id == "2"
        assert orphan.display_name == "Team 2"
        assert orphan.owner_external_id is None

    def test_a_team_with_no_faab_settings_reports_none(self) -> None:
        teams = normalizer.normalize_teams(load("rosters"), load("users"))

        assert teams[1].faab_remaining is None


class TestRosters:
    def test_players_are_collected(self) -> None:
        rosters = normalizer.normalize_rosters(load("rosters"))

        assert rosters[0].player_external_ids == ["4034", "6794", "5849", "1234"]

    def test_empty_starter_slots_survive_as_gaps(self) -> None:
        """The list is positional against roster_positions.

        Filtering the null would shift every later player into the wrong slot
        -- a flex player silently becoming a kicker.
        """
        rosters = normalizer.normalize_rosters(load("rosters"))

        assert rosters[0].starter_external_ids == ["4034", "6794", "", "5849"]
        assert len(rosters[0].starter_external_ids) == 4

    def test_reserve_and_taxi_are_separated(self) -> None:
        rosters = normalizer.normalize_rosters(load("rosters"))

        assert rosters[0].reserve_external_ids == ["1234"]
        assert rosters[0].taxi_external_ids == []

    def test_a_roster_missing_optional_lists_normalizes(self) -> None:
        rosters = normalizer.normalize_rosters(load("rosters"))

        assert rosters[1].reserve_external_ids == []


class TestMatchups:
    def test_a_recorded_week_normalizes(self) -> None:
        matchups = normalizer.normalize_matchups(load("matchups_week_8"), week=8)

        assert len(matchups) == 3
        assert all(m.week == 8 for m in matchups)

    def test_custom_points_override_the_computed_total(self) -> None:
        """A commissioner override exists precisely to replace the computed value."""
        matchups = normalizer.normalize_matchups(load("matchups_week_8"), week=8)

        assert matchups[0].points == Decimal("118.94")  # no override
        assert matchups[1].points == Decimal("100.0")  # override wins over 96.5

    def test_a_bye_has_no_matchup_id(self) -> None:
        """None is meaningful -- there is no opponent to pair with."""
        matchups = normalizer.normalize_matchups(load("matchups_week_8"), week=8)

        assert matchups[2].matchup_id is None

    def test_matchup_ids_are_strings(self) -> None:
        matchups = normalizer.normalize_matchups(load("matchups_week_8"), week=8)

        assert matchups[0].matchup_id == "1"

    def test_an_empty_week_yields_nothing(self) -> None:
        """Sleeper returns [] for a week that has not happened."""
        assert normalizer.normalize_matchups([], week=18) == []


class TestTransactions:
    def test_a_waiver_claim_normalizes(self) -> None:
        transactions = normalizer.normalize_transactions(load("transactions_week_8"), week=8)

        waiver = transactions[0]
        assert waiver.kind == "waiver"
        assert waiver.status == "complete"
        assert waiver.adds == {"7788": "1"}
        assert waiver.drops == {"1234": "1"}
        assert waiver.faab_spent == 17

    def test_a_trade_carries_both_rosters(self) -> None:
        """One transaction, several rosters -- that is what makes a trade legible."""
        transactions = normalizer.normalize_transactions(load("transactions_week_8"), week=8)

        trade = transactions[1]
        assert trade.kind == "trade"
        assert sorted(trade.team_external_ids) == ["1", "2"]
        assert len(trade.adds) == 2

    def test_timestamps_convert_from_epoch_milliseconds(self) -> None:
        transactions = normalizer.normalize_transactions(load("transactions_week_8"), week=8)

        created = transactions[0].created_at
        assert created is not None
        assert created.year == 2026
        assert created.tzinfo is not None

    def test_a_null_timestamp_is_tolerated(self) -> None:
        transactions = normalizer.normalize_transactions(load("transactions_week_8"), week=8)

        assert transactions[2].created_at is None

    def test_null_adds_and_drops_become_empty_maps(self) -> None:
        """A failed transaction has neither, and must not crash the import."""
        transactions = normalizer.normalize_transactions(load("transactions_week_8"), week=8)

        assert transactions[2].adds == {}
        assert transactions[2].drops == {}

    def test_the_week_comes_from_leg_when_present(self) -> None:
        payload = [{"transaction_id": "1", "type": "waiver", "status": "complete", "leg": 3}]

        assert normalizer.normalize_transactions(payload, week=8)[0].week == 3

    def test_the_requested_week_is_the_fallback(self) -> None:
        payload = [{"transaction_id": "1", "type": "waiver", "status": "complete"}]

        assert normalizer.normalize_transactions(payload, week=8)[0].week == 8


class TestPlayers:
    def test_the_export_normalizes(self) -> None:
        players = normalizer.normalize_players(load("players"))

        assert len(players) == 4
        by_id = {p.external_id: p for p in players}
        assert by_id["4034"].full_name == "Christian McCaffrey"
        assert by_id["4034"].position == "RB"

    def test_a_team_defense_gets_a_usable_name(self) -> None:
        """DEF entries have no first or last name; the id is the fallback."""
        players = {p.external_id: p for p in normalizer.normalize_players(load("players"))}

        assert players["SF"].full_name == "SF"
        assert players["SF"].position == "DEF"

    def test_a_name_is_assembled_when_full_name_is_absent(self) -> None:
        players = {p.external_id: p for p in normalizer.normalize_players(load("players"))}

        assert players["9999"].full_name == "Retired Guy"

    def test_non_dict_entries_are_skipped(self) -> None:
        """Sleeper has served stray scalars in this map before."""
        assert normalizer.normalize_players({"junk": "not-an-object"}) == []
