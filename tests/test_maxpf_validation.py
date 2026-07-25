"""Integration test to validate MaxPF calculation against Sleeper's values.

This test fetches real data from the Sleeper API for the 2025 season
and compares our calculated Max Potential Points against Sleeper's
official values.

The expected values are taken from the Sleeper app standings page
at the end of the 2025 regular season (weeks 1-14, before week 15 playoffs).
"""

import pytest
import aiohttp

from clients.sleeper import SleeperClient
from cogs.analytics import calculate_optimal_lineup

# The 2025 league, pinned deliberately: the expected values below come from a
# 2025 end-of-regular-season standings screenshot, so this must keep reading the
# 2025 league even after SLEEPER_LEAGUE_ID moves on to a renewed season. Do not
# replace it with the config value.
LEAGUE_ID = "1231652068087844864"

# Expected MaxPF values from Sleeper app (2025 season, weeks 1-14)
# Extracted from standings screenshot
EXPECTED_MAX_PF = {
    "Freaky 4 Zekey": 2340.12,
    "Wolfe Packe": 2396.32,
    "Levis will not be good 😭": 2107.14,
    "I<3Jacksonville": 2097.36,
    "Revived Grizzled Vets!": 2105.34,
    "Touchdown Snipers": 2083.40,
    "First round wash-ups": 1916.78,
    "Order of the Penix": 2206.34,
    "Meet the Robinsons": 2006.62,
    "Team TheGRex": 1784.42,
    "Tight End Jail": 1681.02,
    "ParkedOutByThe Lake": 1659.46,
}

# Expected Points For values for additional validation
EXPECTED_POINTS_FOR = {
    "Freaky 4 Zekey": 2054.84,
    "Wolfe Packe": 1946.56,
    "Levis will not be good 😭": 1831.02,
    "I<3Jacksonville": 1836.22,
    "Revived Grizzled Vets!": 1748.56,
    "Touchdown Snipers": 1753.66,
    "First round wash-ups": 1597.54,
    "Order of the Penix": 1901.12,
    "Meet the Robinsons": 1590.32,
    "Team TheGRex": 1560.28,
    "Tight End Jail": 1487.94,
    "ParkedOutByThe Lake": 1334.82,
}

# Tolerance for MaxPF comparison (percentage)
# Our calculation may differ slightly due to:
# - Different scoring precision
# - Player data timing differences
# - Rounding in aggregations
MAX_PF_TOLERANCE_PERCENT = 5.0


class TestMaxPFValidation:
    """Integration tests validating MaxPF calculation against Sleeper data."""

    @pytest.fixture
    async def sleeper_client(self):
        """Create a real Sleeper client for integration testing."""
        async with aiohttp.ClientSession() as session:
            yield SleeperClient(session)

    @pytest.fixture
    async def league_data(self, sleeper_client):
        """Fetch all required league data from Sleeper API."""
        # Get league info
        league = await sleeper_client.get_league(LEAGUE_ID)
        
        # Get roster positions from league settings
        roster_positions = league.get("roster_positions", [])
        
        # Get users for team name mapping
        users = await sleeper_client.get_users(LEAGUE_ID)
        user_lookup = {}
        for user in users:
            user_id = user.get("user_id")
            # Try team_name from metadata first, then display_name
            team_name = user.get("metadata", {}).get("team_name") or user.get("display_name")
            user_lookup[user_id] = team_name
        
        # Get rosters to map roster_id to owner
        rosters = await sleeper_client.get_rosters(LEAGUE_ID)
        roster_to_team = {}
        for roster in rosters:
            roster_id = roster.get("roster_id")
            owner_id = roster.get("owner_id")
            team_name = user_lookup.get(owner_id, f"Team {roster_id}")
            roster_to_team[roster_id] = team_name
        
        # Get players data for position info
        players = await sleeper_client.get_all_players()
        
        # Fetch matchups for weeks 1-14 (regular season)
        matchups_by_week = {}
        for week in range(1, 15):
            matchups_by_week[week] = await sleeper_client.get_matchups(LEAGUE_ID, week)
        
        return {
            "roster_positions": roster_positions,
            "roster_to_team": roster_to_team,
            "players": players,
            "matchups_by_week": matchups_by_week,
        }

    def calculate_team_max_pf(
        self,
        roster_id: int,
        matchups_by_week: dict,
        players: dict,
        roster_positions: list,
    ) -> float:
        """Calculate MaxPF for a team across all weeks."""
        total_max_pf = 0.0
        
        for week in range(1, 15):
            week_matchups = matchups_by_week.get(week, [])
            
            # Find this team's matchup
            for matchup in week_matchups:
                if matchup.get("roster_id") == roster_id:
                    players_points = matchup.get("players_points", {})
                    
                    # Build roster players list with position and points
                    roster_players = []
                    for player_id, points in players_points.items():
                        player_data = players.get(player_id, {})
                        position = player_data.get("position", "")
                        roster_players.append({
                            "position": position,
                            "points": points or 0,
                        })
                    
                    # Calculate optimal lineup for this week
                    week_max = calculate_optimal_lineup(roster_players, roster_positions)
                    total_max_pf += week_max
                    break
        
        return total_max_pf

    @pytest.mark.asyncio
    async def test_max_pf_matches_sleeper_within_tolerance(self, league_data):
        """Test that our MaxPF calculation matches Sleeper's within tolerance."""
        roster_positions = league_data["roster_positions"]
        roster_to_team = league_data["roster_to_team"]
        players = league_data["players"]
        matchups_by_week = league_data["matchups_by_week"]
        
        results = []
        
        for roster_id, team_name in roster_to_team.items():
            # Calculate our MaxPF
            calculated_max_pf = self.calculate_team_max_pf(
                roster_id,
                matchups_by_week,
                players,
                roster_positions,
            )
            
            # Get expected MaxPF from Sleeper
            expected_max_pf = EXPECTED_MAX_PF.get(team_name)
            
            if expected_max_pf is None:
                # Try to find by partial match (team names may differ slightly)
                for expected_team, expected_value in EXPECTED_MAX_PF.items():
                    if expected_team.lower() in team_name.lower() or team_name.lower() in expected_team.lower():
                        expected_max_pf = expected_value
                        break
            
            if expected_max_pf is not None:
                # Calculate percentage difference
                diff_percent = abs(calculated_max_pf - expected_max_pf) / expected_max_pf * 100
                
                results.append({
                    "team": team_name,
                    "calculated": round(calculated_max_pf, 2),
                    "expected": expected_max_pf,
                    "diff_percent": round(diff_percent, 2),
                    "within_tolerance": diff_percent <= MAX_PF_TOLERANCE_PERCENT,
                })
        
        # Print results for debugging
        print("\n" + "=" * 80)
        print("MaxPF Validation Results")
        print("=" * 80)
        print(f"{'Team':<30} {'Calculated':>12} {'Expected':>12} {'Diff %':>10} {'Status':>10}")
        print("-" * 80)
        
        for r in sorted(results, key=lambda x: x["expected"], reverse=True):
            status = "✓ PASS" if r["within_tolerance"] else "✗ FAIL"
            print(f"{r['team'][:29]:<30} {r['calculated']:>12.2f} {r['expected']:>12.2f} {r['diff_percent']:>9.2f}% {status:>10}")
        
        print("-" * 80)
        
        # Calculate overall stats
        passed = sum(1 for r in results if r["within_tolerance"])
        total = len(results)
        avg_diff = sum(r["diff_percent"] for r in results) / total if total > 0 else 0
        
        print(f"Passed: {passed}/{total} ({passed/total*100:.1f}%)")
        print(f"Average difference: {avg_diff:.2f}%")
        print(f"Tolerance: {MAX_PF_TOLERANCE_PERCENT}%")
        print("=" * 80 + "\n")
        
        # Assert all teams are within tolerance
        failures = [r for r in results if not r["within_tolerance"]]
        if failures:
            failure_msg = "\n".join(
                f"  {r['team']}: calculated={r['calculated']}, expected={r['expected']}, diff={r['diff_percent']}%"
                for r in failures
            )
            pytest.fail(f"MaxPF validation failed for {len(failures)} team(s):\n{failure_msg}")

    @pytest.mark.asyncio
    async def test_points_for_accuracy(self, league_data):
        """Test that Points For from matchups sums correctly as sanity check."""
        roster_to_team = league_data["roster_to_team"]
        matchups_by_week = league_data["matchups_by_week"]
        
        for roster_id, team_name in roster_to_team.items():
            # Calculate total points for
            total_pf = 0.0
            
            for week in range(1, 15):
                week_matchups = matchups_by_week.get(week, [])
                for matchup in week_matchups:
                    if matchup.get("roster_id") == roster_id:
                        total_pf += matchup.get("points", 0) or 0
                        break
            
            # Check if this team is in our expected data
            expected_pf = EXPECTED_POINTS_FOR.get(team_name)
            
            if expected_pf is not None:
                # Points For should match exactly (same source)
                diff = abs(total_pf - expected_pf)
                assert diff < 1.0, (
                    f"Points For mismatch for {team_name}: "
                    f"calculated={total_pf:.2f}, expected={expected_pf:.2f}"
                )

    @pytest.mark.asyncio  
    async def test_roster_positions_are_valid(self, league_data):
        """Verify the league roster positions are as expected for a superflex dynasty."""
        roster_positions = league_data["roster_positions"]
        
        # Should have standard dynasty superflex positions
        assert "QB" in roster_positions
        assert "SUPER_FLEX" in roster_positions or "FLEX" in roster_positions
        assert "BN" in roster_positions
        
        # Count starters (non-BN, non-IR)
        starters = [p for p in roster_positions if p not in ("BN", "IR")]
        assert len(starters) >= 9, f"Expected at least 9 starters, got {len(starters)}"
        
        print(f"\nRoster positions: {roster_positions}")
        print(f"Starter count: {len(starters)}")
