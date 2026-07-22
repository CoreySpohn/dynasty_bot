"""Tests for the analytics cog utilities."""

import pytest

from cogs.analytics import (
    calculate_optimal_lineup,
    generate_power_rankings_sync,
    FLEX_POSITIONS,
)


class TestCalculateOptimalLineup:
    """Test suite for optimal lineup calculation."""

    def test_simple_lineup(self):
        """Test optimal lineup with simple positions."""
        roster_players = [
            {"position": "QB", "points": 25.0},
            {"position": "RB", "points": 18.0},
            {"position": "WR", "points": 15.0},
        ]
        roster_positions = ["QB", "RB", "WR", "BN"]
        
        result = calculate_optimal_lineup(roster_players, roster_positions)
        
        # Should be 25 + 18 + 15 = 58
        assert result == 58.0

    def test_flex_position_takes_best_eligible(self):
        """Test that FLEX position takes the best eligible player."""
        roster_players = [
            {"position": "QB", "points": 20.0},
            {"position": "RB", "points": 15.0},
            {"position": "WR", "points": 22.0},  # Best flex option
            {"position": "TE", "points": 10.0},
        ]
        roster_positions = ["QB", "FLEX", "BN", "BN"]
        
        result = calculate_optimal_lineup(roster_players, roster_positions)
        
        # QB (20) + WR in FLEX (22) = 42
        assert result == 42.0

    def test_super_flex_can_take_qb(self):
        """Test that SUPER_FLEX can take a QB."""
        roster_players = [
            {"position": "QB", "points": 30.0},
            {"position": "QB", "points": 25.0},  # Second QB for superflex
            {"position": "RB", "points": 15.0},
        ]
        roster_positions = ["QB", "SUPER_FLEX", "RB", "BN"]
        
        result = calculate_optimal_lineup(roster_players, roster_positions)
        
        # QB1 (30) + QB2 in SFLEX (25) + RB (15) = 70
        assert result == 70.0

    def test_bench_players_not_counted(self):
        """Test that BN positions are excluded from points."""
        roster_players = [
            {"position": "QB", "points": 20.0},
            {"position": "RB", "points": 50.0},  # High scoring bench player
        ]
        roster_positions = ["QB", "BN"]
        
        result = calculate_optimal_lineup(roster_players, roster_positions)
        
        # Only QB counts, BN is excluded
        assert result == 20.0

    def test_ir_players_not_counted(self):
        """Test that IR positions are excluded from points."""
        roster_players = [
            {"position": "QB", "points": 20.0},
            {"position": "RB", "points": 50.0},
        ]
        roster_positions = ["QB", "IR"]
        
        result = calculate_optimal_lineup(roster_players, roster_positions)
        
        # Only QB counts
        assert result == 20.0

    def test_greedy_assignment(self):
        """Test that greedy algorithm assigns highest scorers first."""
        roster_players = [
            {"position": "WR", "points": 30.0},
            {"position": "WR", "points": 20.0},
            {"position": "WR", "points": 10.0},
            {"position": "RB", "points": 15.0},
        ]
        roster_positions = ["WR", "WR", "FLEX", "BN"]
        
        result = calculate_optimal_lineup(roster_players, roster_positions)
        
        # WR1 (30) + WR2 (20) + RB in FLEX (15) = 65
        # Not WR3 (10) because RB is higher
        assert result == 65.0

    def test_missing_position(self):
        """Test handling when a required position is not available."""
        roster_players = [
            {"position": "RB", "points": 20.0},
            {"position": "WR", "points": 15.0},
        ]
        roster_positions = ["QB", "RB", "WR", "BN"]  # No QB available
        
        result = calculate_optimal_lineup(roster_players, roster_positions)
        
        # RB (20) + WR (15) = 35 (no QB to fill that slot)
        assert result == 35.0

    def test_empty_roster(self):
        """Test handling of empty roster."""
        roster_players = []
        roster_positions = ["QB", "RB", "WR"]
        
        result = calculate_optimal_lineup(roster_players, roster_positions)
        
        assert result == 0.0


class TestFlexPositions:
    """Test suite for FLEX position configuration."""

    def test_flex_allows_rb_wr_te(self):
        """Test FLEX position allows RB, WR, TE."""
        assert "RB" in FLEX_POSITIONS["FLEX"]
        assert "WR" in FLEX_POSITIONS["FLEX"]
        assert "TE" in FLEX_POSITIONS["FLEX"]
        assert "QB" not in FLEX_POSITIONS["FLEX"]

    def test_super_flex_allows_qb(self):
        """Test SUPER_FLEX position allows QB."""
        assert "QB" in FLEX_POSITIONS["SUPER_FLEX"]
        assert "RB" in FLEX_POSITIONS["SUPER_FLEX"]
        assert "WR" in FLEX_POSITIONS["SUPER_FLEX"]
        assert "TE" in FLEX_POSITIONS["SUPER_FLEX"]

    def test_rec_flex_only_receivers(self):
        """Test REC_FLEX only allows WR and TE."""
        assert "WR" in FLEX_POSITIONS["REC_FLEX"]
        assert "TE" in FLEX_POSITIONS["REC_FLEX"]
        assert "RB" not in FLEX_POSITIONS["REC_FLEX"]


class TestGeneratePowerRankingsSyncDynastyBlend:
    """Test suite for the dynasty value component of the Power Level score."""

    def _base_args(self):
        rosters = [{"roster_id": 1, "owner_id": "u1"}]
        matchups_by_week = {
            1: [
                {
                    "roster_id": 1,
                    "matchup_id": 100,
                    "points": 120.0,
                    "players_points": {"p1": 20.0, "p2": 15.0},
                },
                {
                    "roster_id": 2,
                    "matchup_id": 100,
                    "points": 100.0,
                    "players_points": {},
                },
            ]
        }
        users = {"u1": "Team One"}
        players = {"p1": {"position": "QB"}, "p2": {"position": "RB"}}
        roster_positions = ["QB", "RB", "BN"]
        return rosters, matchups_by_week, users, players, roster_positions

    def test_dynasty_value_increases_power_level_by_weighted_amount(self):
        args = self._base_args()

        without = generate_power_rankings_sync(*args, current_week=1, season=2026)
        with_dynasty = generate_power_rankings_sync(
            *args, current_week=1, season=2026, dynasty_values={1: 50000}
        )

        # Weight is 15%, scaled by /1000: (50000 / 1000) * 0.15 == 7.5
        delta = with_dynasty.loc[0, "Power Level"] - without.loc[0, "Power Level"]
        assert round(delta, 1) == 7.5
        assert with_dynasty.loc[0, "Dynasty Value"] == 50000

    def test_missing_dynasty_values_defaults_to_zero(self):
        args = self._base_args()

        df = generate_power_rankings_sync(*args, current_week=1, season=2026)

        assert df.loc[0, "Dynasty Value"] == 0

    def test_unmatched_roster_id_contributes_zero(self):
        args = self._base_args()

        df = generate_power_rankings_sync(
            *args, current_week=1, season=2026, dynasty_values={999: 50000}
        )

        assert df.loc[0, "Dynasty Value"] == 0
