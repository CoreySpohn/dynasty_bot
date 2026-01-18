#!/usr/bin/env python3
"""Debug script to verify regular season (weeks 1-14) points match API fpts."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp
from clients.sleeper import SleeperClient
from cogs.analytics import calculate_optimal_lineup
from config import SLEEPER_LEAGUE_ID


async def verify_regular_season():
    """Compare regular season matchup points with API fpts."""
    
    async with aiohttp.ClientSession() as session:
        client = SleeperClient(session)
        
        # Get league info
        league = await client.get_league(SLEEPER_LEAGUE_ID)
        season = league.get("season")
        playoff_start = league.get("settings", {}).get("playoff_week_start", 15)
        regular_season_end = playoff_start - 1  # Week 14
        roster_positions = league.get("roster_positions", [])
        
        print(f"\n📊 Verifying Regular Season Points (Weeks 1-{regular_season_end})")
        print(f"   Season: {season}")
        print(f"   Playoff start: Week {playoff_start}")
        print("=" * 80)
        
        # Get rosters
        rosters = await client.get_rosters(SLEEPER_LEAGUE_ID)
        users = await client.get_users(SLEEPER_LEAGUE_ID)
        players = await client.get_all_players()
        
        # Build user lookup
        user_lookup = {}
        for user in users:
            user_id = user.get("user_id")
            team_name = user.get("metadata", {}).get("team_name") or user.get("display_name")
            user_lookup[user_id] = team_name
        
        # Build roster data
        roster_data = {}
        for roster in rosters:
            roster_id = roster.get("roster_id")
            owner_id = roster.get("owner_id", "")
            settings = roster.get("settings", {})
            
            api_fpts = settings.get("fpts", 0) + settings.get("fpts_decimal", 0) / 100
            
            roster_data[roster_id] = {
                "team_name": user_lookup.get(owner_id, f"Team {roster_id}"),
                "api_fpts": api_fpts,
                "regular_season_pts": 0.0,
                "regular_season_max_pf": 0.0,
                "playoff_pts": 0.0,
                "playoff_max_pf": 0.0,
            }
        
        # Fetch matchups - separate regular season from playoffs
        print(f"\nFetching matchups...")
        
        for week in range(1, 18):
            try:
                matchups = await client.get_matchups(SLEEPER_LEAGUE_ID, week)
            except Exception:
                continue
            
            for matchup in matchups:
                roster_id = matchup.get("roster_id")
                if roster_id not in roster_data:
                    continue
                
                points = matchup.get("points", 0) or 0
                players_points = matchup.get("players_points", {})
                
                # Calculate MaxPF
                roster_players = []
                for player_id, pts in players_points.items():
                    player_data = players.get(player_id, {})
                    roster_players.append({
                        "position": player_data.get("position", ""),
                        "points": pts or 0,
                    })
                
                week_max = calculate_optimal_lineup(roster_players, roster_positions)
                
                if week <= regular_season_end:
                    roster_data[roster_id]["regular_season_pts"] += points
                    roster_data[roster_id]["regular_season_max_pf"] += week_max
                else:
                    roster_data[roster_id]["playoff_pts"] += points
                    roster_data[roster_id]["playoff_max_pf"] += week_max
        
        # Compare regular season
        print("\n" + "=" * 80)
        print(f"REGULAR SEASON (Weeks 1-{regular_season_end}) - Compare with API fpts")
        print("=" * 80)
        print(f"{'Team':<28} {'API PF':>10} {'Calc PF':>10} {'Diff':>8} {'MaxPF':>10}")
        print("-" * 80)
        
        all_match = True
        for roster_id, data in sorted(roster_data.items(), key=lambda x: x[1]["api_fpts"], reverse=True):
            api_pts = data["api_fpts"]
            calc_pts = data["regular_season_pts"]
            max_pf = data["regular_season_max_pf"]
            diff = api_pts - calc_pts
            
            status = "✓" if abs(diff) < 0.1 else "✗"
            if abs(diff) >= 0.1:
                all_match = False
            
            print(f"{data['team_name'][:27]:<28} {api_pts:>10.2f} {calc_pts:>10.2f} {diff:>+8.2f} {max_pf:>10.2f} {status}")
        
        if all_match:
            print("\n✅ Regular season points MATCH the API fpts values!")
        else:
            print("\n⚠️ Some discrepancies found")
        
        # Show full season totals
        print("\n" + "=" * 80)
        print("FULL SEASON TOTALS (Including Playoffs)")
        print("=" * 80)
        print(f"{'Team':<28} {'Reg Szn':>10} {'Playoff':>10} {'Total':>10} {'MaxPF':>10}")
        print("-" * 80)
        
        for roster_id, data in sorted(
            roster_data.items(), 
            key=lambda x: x[1]["regular_season_pts"] + x[1]["playoff_pts"], 
            reverse=True
        ):
            reg = data["regular_season_pts"]
            playoff = data["playoff_pts"]
            total = reg + playoff
            max_pf = data["regular_season_max_pf"] + data["playoff_max_pf"]
            
            print(f"{data['team_name'][:27]:<28} {reg:>10.2f} {playoff:>10.2f} {total:>10.2f} {max_pf:>10.2f}")
        
        # Recommendation
        print("\n" + "=" * 80)
        print("RECOMMENDATION")
        print("=" * 80)
        print("""
For PAYOUTS based on "Most Points":
  - Use API's roster.settings.fpts (regular season only)
  - This matches Sleeper's standings "PF" column

For MAXPF/Draft Order:
  - Use calculated MaxPF from regular season only (weeks 1-14)
  - This ensures fair comparison since everyone plays 14 games

For "Total Points Including Playoffs" if needed:
  - Sum matchup points from all weeks
  - Note: Not all teams play same number of playoff games
""")


if __name__ == "__main__":
    asyncio.run(verify_regular_season())
