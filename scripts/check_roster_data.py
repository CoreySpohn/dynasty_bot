#!/usr/bin/env python3
"""Investigate how to get points for teams without matchups.

Check if Sleeper provides roster/starter data for weeks without matchups,
and explore getting player scores from NFL stats.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp
from clients.sleeper import SleeperClient
from config import SLEEPER_LEAGUE_ID


async def investigate_roster_data():
    """Check what roster data is available per week."""
    
    async with aiohttp.ClientSession() as session:
        client = SleeperClient(session)
        
        # Get league info
        league = await client.get_league(SLEEPER_LEAGUE_ID)
        season = league.get("season")
        playoff_start = league.get("settings", {}).get("playoff_week_start", 15)
        
        print(f"\n📊 Investigating Roster Data Availability")
        print(f"   Season: {season}")
        print(f"   Playoff start: Week {playoff_start}")
        print("=" * 80)
        
        # Get current rosters
        rosters = await client.get_rosters(SLEEPER_LEAGUE_ID)
        users = await client.get_users(SLEEPER_LEAGUE_ID)
        
        # Build user lookup
        user_lookup = {}
        for user in users:
            user_id = user.get("user_id")
            team_name = user.get("metadata", {}).get("team_name") or user.get("display_name")
            user_lookup[user_id] = team_name
        
        # Check what the roster endpoint provides
        print("\n📋 Current Roster Data (sample):")
        sample_roster = rosters[0]
        owner_id = sample_roster.get("owner_id", "")
        team_name = user_lookup.get(owner_id, "Unknown")
        
        print(f"  Team: {team_name}")
        print(f"  Keys: {list(sample_roster.keys())}")
        print(f"  Starters: {sample_roster.get('starters', [])[:5]}...")
        print(f"  Players: {len(sample_roster.get('players', []))} total")
        
        # Check matchups for playoff weeks
        print("\n📅 Matchup Data per Week (Playoff Weeks):")
        
        for week in range(playoff_start, 18):
            try:
                matchups = await client.get_matchups(SLEEPER_LEAGUE_ID, week)
            except Exception as e:
                print(f"  Week {week}: Error - {e}")
                continue
            
            # Check which teams have data
            teams_with_points = []
            teams_without_points = []
            
            for m in matchups:
                roster_id = m.get("roster_id")
                points = m.get("points", 0) or 0
                players_points = m.get("players_points", {})
                matchup_id = m.get("matchup_id")
                starters = m.get("starters", [])
                
                roster = next((r for r in rosters if r["roster_id"] == roster_id), None)
                owner_id = roster.get("owner_id", "") if roster else ""
                team_name = user_lookup.get(owner_id, f"Team {roster_id}")
                
                has_data = points > 0 or len(players_points) > 0
                
                info = {
                    "team": team_name,
                    "matchup_id": matchup_id,
                    "points": points,
                    "has_players_points": len(players_points) > 0,
                    "starters": len(starters),
                }
                
                if has_data:
                    teams_with_points.append(info)
                else:
                    teams_without_points.append(info)
            
            print(f"\n  Week {week}:")
            print(f"    Teams with data: {len(teams_with_points)}")
            print(f"    Teams without data: {len(teams_without_points)}")
            
            if teams_without_points:
                print(f"    Teams missing data:")
                for t in teams_without_points:
                    print(f"      - {t['team']}: matchup_id={t['matchup_id']}, starters={t['starters']}")
        
        # Check if we can get weekly stats from nfl-data-py
        print("\n" + "=" * 80)
        print("ALTERNATIVE: Using nfl-data-py for player stats")
        print("=" * 80)
        
        try:
            import nfl_data_py as nfl
            print("\n  Checking nfl_data_py availability...")
            
            # Try to load player stats for the season
            print(f"  Loading player stats for {season}...")
            # This would give us weekly player scores
            # stats = nfl.import_weekly_data([int(season)])
            # print(f"  Columns: {list(stats.columns)[:10]}...")
            print("  ✓ nfl_data_py is available for fetching weekly player stats")
            print("  This could be used as a fallback for teams without matchup data")
        except ImportError:
            print("  ✗ nfl_data_py not available")
        except Exception as e:
            print(f"  ✗ Error: {e}")
        
        print("\n" + "=" * 80)
        print("CONCLUSION")
        print("=" * 80)
        print("""
Based on the investigation:

1. If ALL 12 teams have matchup data every week (including consolation):
   → Current approach works, just use all weeks 1-17

2. If some teams have no matchup in certain weeks:
   → Need to use roster starters + nfl_data_py weekly stats
   → Or accept that some teams won't have data for those weeks
""")


if __name__ == "__main__":
    asyncio.run(investigate_roster_data())
