#!/usr/bin/env python3
"""Debug script to investigate playoff points calculation.

This script compares the points calculated from matchups vs 
the roster endpoint to identify any discrepancies during playoffs.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp
from clients.sleeper import SleeperClient
from cogs.analytics import calculate_optimal_lineup
from config import SLEEPER_LEAGUE_ID


async def investigate_points():
    """Compare matchup-based points with roster-based points."""
    
    async with aiohttp.ClientSession() as session:
        client = SleeperClient(session)
        
        # Get league info
        league = await client.get_league(SLEEPER_LEAGUE_ID)
        season = league.get("season")
        playoff_start = league.get("settings", {}).get("playoff_week_start", 15)
        roster_positions = league.get("roster_positions", [])
        
        print(f"\n📊 Investigating Points Calculation")
        print(f"   Season: {season}")
        print(f"   Playoff start: Week {playoff_start}")
        print("=" * 90)
        
        # Get rosters (has total season points)
        rosters = await client.get_rosters(SLEEPER_LEAGUE_ID)
        users = await client.get_users(SLEEPER_LEAGUE_ID)
        players = await client.get_all_players()
        
        # Build user lookup
        user_lookup = {}
        for user in users:
            user_id = user.get("user_id")
            team_name = user.get("metadata", {}).get("team_name") or user.get("display_name")
            user_lookup[user_id] = team_name
        
        # Build roster data with API-reported totals
        roster_data = {}
        for roster in rosters:
            roster_id = roster.get("roster_id")
            owner_id = roster.get("owner_id", "")
            settings = roster.get("settings", {})
            
            # API-reported season totals
            fpts = settings.get("fpts", 0) + settings.get("fpts_decimal", 0) / 100
            fpts_against = settings.get("fpts_against", 0) + settings.get("fpts_against_decimal", 0) / 100
            
            roster_data[roster_id] = {
                "team_name": user_lookup.get(owner_id, f"Team {roster_id}"),
                "api_fpts": fpts,
                "api_fpts_against": fpts_against,
                "matchup_pts": 0.0,
                "matchup_max_pf": 0.0,
                "weeks_played": 0,
                "week_details": {},
            }
        
        # Fetch matchups for all weeks
        print(f"\nFetching matchups for weeks 1-17...")
        
        for week in range(1, 18):
            try:
                matchups = await client.get_matchups(SLEEPER_LEAGUE_ID, week)
            except Exception as e:
                print(f"  Week {week}: Error - {e}")
                continue
            
            if not matchups:
                print(f"  Week {week}: No matchups")
                continue
            
            teams_with_matchups = 0
            
            for matchup in matchups:
                roster_id = matchup.get("roster_id")
                if roster_id not in roster_data:
                    continue
                
                points = matchup.get("points", 0) or 0
                players_points = matchup.get("players_points", {})
                matchup_id = matchup.get("matchup_id")
                
                # Only count if there's actual data
                if points > 0 or players_points:
                    teams_with_matchups += 1
                    roster_data[roster_id]["matchup_pts"] += points
                    roster_data[roster_id]["weeks_played"] += 1
                    roster_data[roster_id]["week_details"][week] = {
                        "points": points,
                        "matchup_id": matchup_id,
                        "has_players_points": bool(players_points),
                    }
                    
                    # Calculate MaxPF
                    roster_players = []
                    for player_id, pts in players_points.items():
                        player_data = players.get(player_id, {})
                        roster_players.append({
                            "position": player_data.get("position", ""),
                            "points": pts or 0,
                        })
                    
                    week_max = calculate_optimal_lineup(roster_players, roster_positions)
                    roster_data[roster_id]["matchup_max_pf"] += week_max
            
            week_type = "Regular" if week < playoff_start else "Playoff"
            print(f"  Week {week} ({week_type}): {teams_with_matchups} teams with matchups")
        
        # Compare results
        print("\n" + "=" * 90)
        print("COMPARISON: API fpts vs Calculated from Matchups")
        print("=" * 90)
        print(f"{'Team':<28} {'API PF':>10} {'Calc PF':>10} {'Diff':>8} {'Weeks':>6} {'Status':<10}")
        print("-" * 90)
        
        discrepancies = []
        
        for roster_id, data in sorted(roster_data.items(), key=lambda x: x[1]["api_fpts"], reverse=True):
            api_pts = data["api_fpts"]
            calc_pts = data["matchup_pts"]
            diff = api_pts - calc_pts
            weeks = data["weeks_played"]
            
            if abs(diff) < 0.1:
                status = "✓ Match"
            else:
                status = "⚠ DIFF"
                discrepancies.append((data["team_name"], api_pts, calc_pts, diff, weeks))
            
            print(f"{data['team_name'][:27]:<28} {api_pts:>10.2f} {calc_pts:>10.2f} {diff:>+8.2f} {weeks:>6} {status:<10}")
        
        # Detail any discrepancies
        if discrepancies:
            print("\n" + "=" * 90)
            print("DISCREPANCY DETAILS")
            print("=" * 90)
            
            for team_name, api_pts, calc_pts, diff, weeks in discrepancies:
                print(f"\n{team_name}:")
                print(f"  API Points: {api_pts:.2f}")
                print(f"  Calculated: {calc_pts:.2f}")
                print(f"  Difference: {diff:+.2f}")
                print(f"  Weeks played: {weeks}")
                
                # Find which weeks might be missing
                roster_id = None
                for rid, data in roster_data.items():
                    if data["team_name"] == team_name:
                        roster_id = rid
                        break
                
                if roster_id:
                    weeks_with_data = set(roster_data[roster_id]["week_details"].keys())
                    all_weeks = set(range(1, 18))
                    missing_weeks = all_weeks - weeks_with_data
                    
                    if missing_weeks:
                        print(f"  Missing weeks: {sorted(missing_weeks)}")
        else:
            print("\n✅ All teams match! API points = Calculated points")
        
        # Show playoff week details
        print("\n" + "=" * 90)
        print("PLAYOFF WEEK DETAILS")
        print("=" * 90)
        
        for week in range(playoff_start, 18):
            print(f"\nWeek {week}:")
            for roster_id, data in roster_data.items():
                if week in data["week_details"]:
                    details = data["week_details"][week]
                    print(f"  {data['team_name'][:25]:<26} - {details['points']:>8.2f} pts (matchup {details['matchup_id']})")
                else:
                    print(f"  {data['team_name'][:25]:<26} - NO MATCHUP")


if __name__ == "__main__":
    asyncio.run(investigate_points())
