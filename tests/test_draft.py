"""Tests for the Draft Order and Payout Calculator."""

import pytest

from cogs.draft import (
    TeamStats,
    calculate_payouts,
    calculate_draft_order,
    PLACEMENT_PAYOUTS,
    POINTS_PAYOUTS,
)


class TestTeamStats:
    """Test suite for TeamStats dataclass."""

    def test_total_payout(self):
        """Test total payout calculation."""
        team = TeamStats(
            roster_id=1,
            team_name="Test Team",
            owner_id="user_001",
            placement_payout=30,
            points_payout=20,
        )
        assert team.total_payout == 50

    def test_record_string(self):
        """Test record string formatting."""
        team = TeamStats(
            roster_id=1,
            team_name="Test Team",
            owner_id="user_001",
            wins=8,
            losses=6,
        )
        assert team.record == "8-6"


class TestCalculatePayouts:
    """Test suite for payout calculation."""

    def test_placement_payouts(self):
        """Test that placement payouts are assigned correctly."""
        teams = [
            TeamStats(roster_id=1, team_name="Champ", owner_id="u1", placement=1),
            TeamStats(roster_id=2, team_name="Runner", owner_id="u2", placement=2),
            TeamStats(roster_id=3, team_name="Third", owner_id="u3", placement=3),
            TeamStats(roster_id=4, team_name="Fourth", owner_id="u4", placement=4),
        ]
        
        result = calculate_payouts(teams)
        
        assert result[0].placement_payout == 30  # 1st place
        assert result[1].placement_payout == 20  # 2nd place
        assert result[2].placement_payout == 10  # 3rd place
        assert result[3].placement_payout == 0   # 4th place (no payout)

    def test_points_payouts(self):
        """Test that points payouts go to top 3 scorers."""
        teams = [
            TeamStats(roster_id=1, team_name="A", owner_id="u1", total_points=2000),
            TeamStats(roster_id=2, team_name="B", owner_id="u2", total_points=1900),
            TeamStats(roster_id=3, team_name="C", owner_id="u3", total_points=1800),
            TeamStats(roster_id=4, team_name="D", owner_id="u4", total_points=1700),
        ]
        
        result = calculate_payouts(teams)
        
        # Points payouts should be assigned to top 3
        assert result[0].points_payout == 30  # Most points
        assert result[1].points_payout == 20  # 2nd most
        assert result[2].points_payout == 10  # 3rd most
        assert result[3].points_payout == 0   # 4th (no payout)

    def test_combined_payouts(self):
        """Test team can win both placement and points payouts."""
        teams = [
            TeamStats(
                roster_id=1,
                team_name="Champ",
                owner_id="u1",
                placement=1,
                total_points=2500,  # Also highest scorer
            ),
            TeamStats(
                roster_id=2,
                team_name="Other",
                owner_id="u2",
                total_points=2000,
            ),
        ]
        
        result = calculate_payouts(teams)
        
        # Champion who also had most points
        assert result[0].placement_payout == 30
        assert result[0].points_payout == 30
        assert result[0].total_payout == 60


class TestCalculateDraftOrder:
    """Test suite for draft order calculation."""

    def test_non_winners_ordered_by_max_pf_ascending(self):
        """Test that non-winners are ordered by MaxPF (lowest first)."""
        teams = [
            TeamStats(roster_id=1, team_name="High", owner_id="u1", max_pf=2500),
            TeamStats(roster_id=2, team_name="Mid", owner_id="u2", max_pf=2000),
            TeamStats(roster_id=3, team_name="Low", owner_id="u3", max_pf=1500),
        ]
        
        result = calculate_draft_order(teams)
        
        assert result[0].team_name == "Low"   # 1st pick (lowest MaxPF)
        assert result[1].team_name == "Mid"   # 2nd pick
        assert result[2].team_name == "High"  # 3rd pick (highest MaxPF)

    def test_winners_pick_last(self):
        """Test that money winners pick after non-winners."""
        teams = [
            TeamStats(
                roster_id=1,
                team_name="Winner",
                owner_id="u1",
                max_pf=1500,  # Lowest MaxPF but won money
                placement_payout=30,
            ),
            TeamStats(
                roster_id=2,
                team_name="Loser",
                owner_id="u2",
                max_pf=2500,  # Highest MaxPF but no money
            ),
        ]
        
        result = calculate_draft_order(teams)
        
        # Non-winner picks first despite higher MaxPF
        assert result[0].team_name == "Loser"
        assert result[1].team_name == "Winner"

    def test_winners_ordered_by_payout_amount(self):
        """Test that multiple winners are ordered by payout (most $ = last pick)."""
        teams = [
            TeamStats(
                roster_id=1,
                team_name="Big Winner",
                owner_id="u1",
                max_pf=2000,
                placement_payout=30,
                points_payout=30,  # $60 total
            ),
            TeamStats(
                roster_id=2,
                team_name="Small Winner",
                owner_id="u2",
                max_pf=2000,
                placement_payout=10,  # $10 total
            ),
            TeamStats(
                roster_id=3,
                team_name="Non-Winner",
                owner_id="u3",
                max_pf=1800,
            ),
        ]
        
        result = calculate_draft_order(teams)
        
        # Order: Non-winner first, then small winner, then big winner (last)
        assert result[0].team_name == "Non-Winner"
        assert result[1].team_name == "Small Winner"
        assert result[2].team_name == "Big Winner"

    def test_payout_ties_broken_by_max_pf(self):
        """Test that equal payouts are ordered by MaxPF (higher MaxPF = later pick)."""
        teams = [
            TeamStats(
                roster_id=1,
                team_name="High MaxPF",
                owner_id="u1",
                max_pf=2500,
                placement_payout=20,  # Same payout
            ),
            TeamStats(
                roster_id=2,
                team_name="Low MaxPF",
                owner_id="u2",
                max_pf=2000,
                placement_payout=20,  # Same payout
            ),
        ]
        
        result = calculate_draft_order(teams)
        
        # Lower MaxPF picks first among winners with same payout
        assert result[0].team_name == "Low MaxPF"
        assert result[1].team_name == "High MaxPF"

    def test_full_league_scenario(self):
        """Test a realistic 12-team league scenario."""
        teams = [
            # Champion with most points
            TeamStats(roster_id=1, team_name="Champ", owner_id="u1",
                     max_pf=2400, placement=1, total_points=2100),
            # Runner-up with 3rd most points
            TeamStats(roster_id=2, team_name="Runner", owner_id="u2",
                     max_pf=2300, placement=2, total_points=1900),
            # 3rd place with 2nd most points  
            TeamStats(roster_id=3, team_name="Third", owner_id="u3",
                     max_pf=2200, placement=3, total_points=2000),
            # Non-playoff teams
            TeamStats(roster_id=4, team_name="Team4", owner_id="u4", max_pf=2100),
            TeamStats(roster_id=5, team_name="Team5", owner_id="u5", max_pf=2000),
            TeamStats(roster_id=6, team_name="Team6", owner_id="u6", max_pf=1900),
            TeamStats(roster_id=7, team_name="Team7", owner_id="u7", max_pf=1800),
            TeamStats(roster_id=8, team_name="Team8", owner_id="u8", max_pf=1700),
            TeamStats(roster_id=9, team_name="Team9", owner_id="u9", max_pf=1600),
            TeamStats(roster_id=10, team_name="Team10", owner_id="u10", max_pf=1500),
            TeamStats(roster_id=11, team_name="Team11", owner_id="u11", max_pf=1400),
            TeamStats(roster_id=12, team_name="Team12", owner_id="u12", max_pf=1300),
        ]
        
        # Calculate payouts first
        teams = calculate_payouts(teams)
        
        # Calculate draft order
        result = calculate_draft_order(teams)
        
        # First pick should be lowest MaxPF non-winner
        assert result[0].team_name == "Team12"
        assert result[0].max_pf == 1300
        
        # Last 3 picks should be the money winners
        # Ordered by payout then MaxPF
        winners = result[-3:]
        winner_names = [t.team_name for t in winners]
        
        # All 3 placement winners should be in last 3 picks
        assert "Champ" in winner_names
        assert "Runner" in winner_names
        assert "Third" in winner_names
        
        # Champion (most money: $60) should be last pick
        assert result[-1].team_name == "Champ"


class TestPayoutConstants:
    """Test payout structure constants."""

    def test_placement_payouts_sum(self):
        """Verify placement payouts sum to expected total."""
        total = sum(PLACEMENT_PAYOUTS.values())
        assert total == 60  # $30 + $20 + $10

    def test_points_payouts_sum(self):
        """Verify points payouts sum to expected total."""
        total = sum(POINTS_PAYOUTS.values())
        assert total == 60  # $30 + $20 + $10

    def test_total_pot(self):
        """Verify total pot is $120 ($60 placement + $60 points)."""
        total = sum(PLACEMENT_PAYOUTS.values()) + sum(POINTS_PAYOUTS.values())
        assert total == 120
