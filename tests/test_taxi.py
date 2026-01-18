"""Tests for the taxi raiding cog utilities."""

import pytest

from cogs.taxi import ordinal, calculate_raid_cost


class TestOrdinal:
    """Test suite for ordinal number formatting."""

    @pytest.mark.parametrize(
        "number,expected",
        [
            (1, "1st"),
            (2, "2nd"),
            (3, "3rd"),
            (4, "4th"),
            (5, "5th"),
            (10, "10th"),
            (11, "11th"),
            (12, "12th"),
            (13, "13th"),
            (21, "21st"),
            (22, "22nd"),
            (23, "23rd"),
            (100, "100th"),
            (101, "101st"),
            (111, "111th"),
            (112, "112th"),
        ],
    )
    def test_ordinal_numbers(self, number, expected):
        """Test ordinal formatting for various numbers."""
        assert ordinal(number) == expected


class TestCalculateRaidCost:
    """Test suite for raid cost calculation."""

    def test_round_1_cost(self):
        """Round 1 picks cost just a 1st."""
        assert calculate_raid_cost(1) == "1st Round Pick"

    def test_round_2_cost(self):
        """Round 2 picks cost a 1st and 2nd."""
        assert calculate_raid_cost(2) == "1st & 2nd Round Picks"

    def test_round_3_cost(self):
        """Round 3 picks cost a 2nd and 3rd."""
        assert calculate_raid_cost(3) == "2nd & 3rd Round Picks"

    def test_round_4_cost(self):
        """Round 4 picks cost a 3rd and 4th."""
        assert calculate_raid_cost(4) == "3rd & 4th Round Picks"

    def test_round_5_cost(self):
        """Round 5 picks cost a 4th and 5th."""
        assert calculate_raid_cost(5) == "4th & 5th Round Picks"

    def test_udfa_cost(self):
        """UDFA players cost a 4th round pick."""
        assert calculate_raid_cost("UDFA") == "4th Round Pick"

    def test_non_integer_returns_udfa_cost(self):
        """Non-integer values should be treated as UDFA."""
        assert calculate_raid_cost("unknown") == "4th Round Pick"
