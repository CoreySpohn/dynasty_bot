#!/usr/bin/env python3
"""Test script for Draft Order and Payout Calculator.

This script fetches real data from the Sleeper API and calculates:
1. Season payouts based on placement and points
2. Rookie draft order based on payouts and MaxPF

Usage:
    uv run python scripts/test_draft_order.py
    uv run python scripts/test_draft_order.py --week 14  # Through specific week
"""

import asyncio
import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp

from clients.sleeper import SleeperClient
from cogs.analytics import calculate_optimal_lineup
from cogs.draft import (
    TeamStats,
    calculate_payouts,
    calculate_draft_order,
    PLACEMENT_PAYOUTS,
    POINTS_PAYOUTS,
)
from config import SLEEPER_LEAGUE_ID


async def fetch_team_stats(
    client: SleeperClient,
    league_id: str,
    through_week: int = 17,
) -> tuple[list[TeamStats], dict]:
    """Fetch all team statistics from Sleeper API.
    
    Returns:
        Tuple of (list of TeamStats, league info dict)
    """
    # Get league info
    league = await client.get_league(league_id)
    season = league.get("season", datetime.now().year)
    roster_positions = league.get("roster_positions", [])
    playoff_start = league.get("settings", {}).get("playoff_week_start", 15)
    
    print(f"\n📊 Fetching data for {season} season (through week {through_week})...")
    print(f"   League: {league.get('name', 'Unknown')}")
    print(f"   Playoff start: Week {playoff_start}")
    
    # Get rosters and users
    rosters = await client.get_rosters(league_id)
    users = await client.get_users(league_id)
    players = await client.get_all_players()
    
    # Build user lookup
    user_lookup = {}
    for user in users:
        user_id = user.get("user_id")
        team_name = user.get("metadata", {}).get("team_name") or user.get("display_name")
        user_lookup[user_id] = team_name
    
    # Initialize team stats
    teams: dict[int, TeamStats] = {}
    for roster in rosters:
        roster_id = roster.get("roster_id")
        owner_id = roster.get("owner_id", "")
        settings = roster.get("settings", {})
        
        teams[roster_id] = TeamStats(
            roster_id=roster_id,
            team_name=user_lookup.get(owner_id, f"Team {roster_id}"),
            owner_id=owner_id,
            wins=settings.get("wins", 0),
            losses=settings.get("losses", 0),
        )
    
    # Fetch matchups and calculate points/MaxPF
    print(f"   Fetching matchups for weeks 1-{through_week}...")
    
    for week in range(1, through_week + 1):
        try:
            matchups = await client.get_matchups(league_id, week)
        except Exception as e:
            print(f"   ⚠️  Week {week} not available: {e}")
            break
        
        for matchup in matchups:
            roster_id = matchup.get("roster_id")
            if roster_id not in teams:
                continue
            
            team = teams[roster_id]
            
            # Add points for this week
            points = matchup.get("points", 0) or 0
            team.total_points += points
            
            # Calculate MaxPF for this week
            players_points = matchup.get("players_points", {})
            roster_players = []
            for player_id, pts in players_points.items():
                player_data = players.get(player_id, {})
                roster_players.append({
                    "position": player_data.get("position", ""),
                    "points": pts or 0,
                })
            
            week_max = calculate_optimal_lineup(roster_players, roster_positions)
            team.max_pf += week_max
    
    # Determine playoff placements
    await determine_placements(client, league_id, teams, through_week)
    
    return list(teams.values()), league


async def determine_placements(
    client: SleeperClient,
    league_id: str,
    teams: dict[int, TeamStats],
    through_week: int,
) -> None:
    """Determine playoff placements from championship matchups."""
    championship_week = min(through_week, 17)
    
    try:
        final_matchups = await client.get_matchups(league_id, championship_week)
    except Exception:
        print("   ⚠️  Could not fetch championship week matchups")
        return
    
    if not final_matchups:
        return
    
    # Find championship matchup (matchup_id = 1)
    championship_teams = [m for m in final_matchups if m.get("matchup_id") == 1]
    
    if len(championship_teams) == 2:
        team1, team2 = championship_teams
        pts1 = team1.get("points", 0) or 0
        pts2 = team2.get("points", 0) or 0
        
        if pts1 > pts2:
            winner_id, runner_id = team1["roster_id"], team2["roster_id"]
        else:
            winner_id, runner_id = team2["roster_id"], team1["roster_id"]
        
        if winner_id in teams:
            teams[winner_id].placement = 1
        if runner_id in teams:
            teams[runner_id].placement = 2
    
    # Find 3rd place game (matchup_id = 2)
    third_place_teams = [m for m in final_matchups if m.get("matchup_id") == 2]
    
    if len(third_place_teams) == 2:
        team1, team2 = third_place_teams
        pts1 = team1.get("points", 0) or 0
        pts2 = team2.get("points", 0) or 0
        
        winner_id = team1["roster_id"] if pts1 > pts2 else team2["roster_id"]
        
        if winner_id in teams:
            teams[winner_id].placement = 3


def print_separator(char: str = "=", width: int = 80):
    """Print a separator line."""
    print(char * width)


def print_header(title: str, width: int = 80):
    """Print a centered header."""
    print_separator()
    print(f"{title:^{width}}")
    print_separator()


async def main(through_week: int = 17):
    """Main function to run the draft order calculation."""
    print("\n🏈 Dynasty Bot - Draft Order & Payout Calculator")
    print_separator()
    
    async with aiohttp.ClientSession() as session:
        client = SleeperClient(session)
        
        # Fetch team stats
        teams, league = await fetch_team_stats(
            client,
            SLEEPER_LEAGUE_ID,
            through_week,
        )
        
        season = league.get("season", datetime.now().year)
        next_season = int(season) + 1
        
        # Calculate payouts
        teams = calculate_payouts(teams)
        
        # Calculate draft order
        draft_order = calculate_draft_order(teams)
        
        # Print Standings by Points
        print_header("📊 STANDINGS BY TOTAL POINTS")
        by_points = sorted(teams, key=lambda t: t.total_points, reverse=True)
        
        print(f"{'Rank':<5} {'Team':<25} {'Record':<8} {'Points':>12} {'MaxPF':>12}")
        print("-" * 70)
        
        for rank, team in enumerate(by_points, start=1):
            medal = ""
            if rank == 1:
                medal = " 🥇"
            elif rank == 2:
                medal = " 🥈"
            elif rank == 3:
                medal = " 🥉"
            
            print(
                f"{rank:<5} {team.team_name[:24]:<25} {team.record:<8} "
                f"{team.total_points:>11.2f}{medal}"
                f"{team.max_pf:>12.2f}"
            )
        
        # Print Playoff Placements
        print()
        print_header("🏆 PLAYOFF PLACEMENTS")
        
        placement_teams = [t for t in teams if t.placement]
        placement_teams.sort(key=lambda t: t.placement or 99)
        
        for team in placement_teams:
            place_names = {1: "🏆 Champion", 2: "🥈 Runner-up", 3: "🥉 3rd Place"}
            place_name = place_names.get(team.placement, f"#{team.placement}")
            print(f"  {place_name}: {team.team_name}")
        
        if not placement_teams:
            print("  (Placements not yet determined)")
        
        # Print Payouts
        print()
        print_header("💰 SEASON PAYOUTS")
        
        print(f"\n  Payout Structure:")
        print(f"    Placement: 1st=${PLACEMENT_PAYOUTS[1]}, 2nd=${PLACEMENT_PAYOUTS[2]}, 3rd=${PLACEMENT_PAYOUTS[3]}")
        print(f"    Points:    1st=${POINTS_PAYOUTS[1]}, 2nd=${POINTS_PAYOUTS[2]}, 3rd=${POINTS_PAYOUTS[3]}")
        print(f"    Total Pot: ${sum(PLACEMENT_PAYOUTS.values()) + sum(POINTS_PAYOUTS.values())}")
        
        print(f"\n{'Team':<25} {'Placement':>10} {'Points':>10} {'Total':>10}")
        print("-" * 60)
        
        # Sort by total payout
        by_payout = sorted(teams, key=lambda t: t.total_payout, reverse=True)
        total_paid = 0
        
        for team in by_payout:
            if team.total_payout > 0:
                place_str = f"${team.placement_payout}" if team.placement_payout else "-"
                pts_str = f"${team.points_payout}" if team.points_payout else "-"
                total_str = f"${team.total_payout}"
                total_paid += team.total_payout
                
                print(f"{team.team_name[:24]:<25} {place_str:>10} {pts_str:>10} {total_str:>10}")
        
        print("-" * 60)
        print(f"{'Total Paid Out':<25} {'':<10} {'':<10} ${total_paid:>9}")
        
        # Print Draft Order
        print()
        print_header(f"🏈 {next_season} ROOKIE DRAFT ORDER")
        
        print(f"\n  Logic:")
        print(f"    • Money winners pick LAST (most $ = last pick)")
        print(f"    • Non-winners ordered by MaxPF (lowest = 1st pick)")
        print(f"    • Ties broken by MaxPF")
        
        print(f"\n{'Pick':<6} {'Team':<25} {'MaxPF':>12} {'Payout':>10} {'Reason':<20}")
        print("-" * 80)
        
        for pick, team in enumerate(draft_order, start=1):
            payout_str = f"${team.total_payout}" if team.total_payout else "-"
            
            if team.total_payout > 0:
                reason = f"Won ${team.total_payout}"
            else:
                reason = f"MaxPF rank"
            
            print(
                f"{pick:<6} {team.team_name[:24]:<25} "
                f"{team.max_pf:>12.2f} {payout_str:>10} {reason:<20}"
            )
        
        # Summary
        print()
        print_separator()
        print(f"\n✅ Draft order calculated successfully!")
        print(f"   Season: {season}")
        print(f"   Through week: {through_week}")
        print(f"   Teams: {len(teams)}")
        print(f"   Total payouts: ${total_paid}")
        print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Calculate draft order and payouts for your dynasty league"
    )
    parser.add_argument(
        "--week", "-w",
        type=int,
        default=17,
        help="Last week to include (default: 17 for full season)"
    )
    
    args = parser.parse_args()
    
    asyncio.run(main(through_week=args.week))
