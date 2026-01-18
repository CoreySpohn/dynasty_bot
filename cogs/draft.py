"""Draft Order and Payout Calculator Cog for Dynasty Bot.

Calculates season payouts based on placement and total points,
then determines the rookie draft order using payouts and MaxPF.

Payout Structure:
- 1st place: $30, 2nd place: $20, 3rd place: $10
- Most points: $30, 2nd most points: $20, 3rd most points: $10

Draft Order Logic:
- Money winners get LAST picks (ordered by winnings, MaxPF tiebreaker)
- Non-winners ordered by MaxPF ascending (lowest MaxPF = 1st pick)
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from clients.sleeper import SleeperClient
from cogs.analytics import calculate_optimal_lineup
from config import SLEEPER_LEAGUE_ID

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.draft")

# Payout structure
PLACEMENT_PAYOUTS = {
    1: 30,  # 1st place
    2: 20,  # 2nd place
    3: 10,  # 3rd place
}

POINTS_PAYOUTS = {
    1: 30,  # Most points
    2: 20,  # 2nd most points
    3: 10,  # 3rd most points
}


@dataclass
class TeamStats:
    """Statistics for a team used in payout and draft order calculations."""
    
    roster_id: int
    team_name: str
    owner_id: str
    
    # Season stats
    wins: int = 0
    losses: int = 0
    total_points: float = 0.0  # Including playoffs
    max_pf: float = 0.0
    
    # Playoff placement (1 = champion, 2 = runner-up, etc.)
    placement: Optional[int] = None
    
    # Calculated payouts
    placement_payout: int = 0
    points_payout: int = 0
    
    @property
    def total_payout(self) -> int:
        """Total money won."""
        return self.placement_payout + self.points_payout
    
    @property
    def record(self) -> str:
        """Win-loss record string."""
        return f"{self.wins}-{self.losses}"


def calculate_payouts(teams: list[TeamStats]) -> list[TeamStats]:
    """Calculate payouts for all teams based on placement and points.
    
    Args:
        teams: List of TeamStats with placement and total_points set.
        
    Returns:
        Same list with payout fields populated.
    """
    # Placement payouts (for teams with placement set)
    for team in teams:
        if team.placement and team.placement in PLACEMENT_PAYOUTS:
            team.placement_payout = PLACEMENT_PAYOUTS[team.placement]
    
    # Points payouts (top 3 by total points)
    sorted_by_points = sorted(teams, key=lambda t: t.total_points, reverse=True)
    for rank, team in enumerate(sorted_by_points[:3], start=1):
        team.points_payout = POINTS_PAYOUTS[rank]
    
    return teams


def calculate_draft_order(teams: list[TeamStats]) -> list[TeamStats]:
    """Determine rookie draft order based on payouts and MaxPF.
    
    Logic:
    1. Teams that won money pick LAST, ordered by total payout DESC
       (most money = last pick), with MaxPF DESC as tiebreaker
    2. Teams with no winnings pick first, ordered by MaxPF ASC
       (lowest MaxPF = first pick)
    
    Args:
        teams: List of TeamStats with payouts and max_pf calculated.
        
    Returns:
        List of teams in draft order (index 0 = 1st pick).
    """
    # Separate into money winners and non-winners
    winners = [t for t in teams if t.total_payout > 0]
    non_winners = [t for t in teams if t.total_payout == 0]
    
    # Non-winners: sort by MaxPF ascending (lowest first = earliest pick)
    non_winners.sort(key=lambda t: t.max_pf)
    
    # Winners: sort by total payout DESC, then MaxPF DESC (pick last)
    # We reverse so highest payout is at the end of the final list
    winners.sort(key=lambda t: (t.total_payout, t.max_pf))
    
    # Draft order: non-winners first (by MaxPF), then winners last (by payout)
    draft_order = non_winners + winners
    
    return draft_order


class DraftCalculator(commands.Cog):
    """Calculates payouts and rookie draft order for the league."""
    
    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID
    
    async def cog_load(self) -> None:
        """Called when the cog is loaded."""
        logger.info("Draft Calculator cog loaded")
    
    async def _fetch_team_stats(
        self,
        season: Optional[int] = None,
        through_week: int = 17,  # Include playoffs
    ) -> list[TeamStats]:
        """Fetch and calculate all team statistics for the season.
        
        Args:
            season: Season year (defaults to current).
            through_week: Last week to include (17 for full season + playoffs).
            
        Returns:
            List of TeamStats with all fields populated.
        """
        # Get league info
        league = await self.bot.sleeper.get_league(self.league_id)
        if season is None:
            season = int(league.get("season", datetime.now().year))
        
        roster_positions = league.get("roster_positions", [])
        playoff_start = league.get("settings", {}).get("playoff_week_start", 15)
        
        # Get rosters and users
        rosters = await self.bot.sleeper.get_rosters(self.league_id)
        users = await self.bot.sleeper.get_users(self.league_id)
        players = await self.bot.sleeper.get_all_players()
        
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
        for week in range(1, through_week + 1):
            try:
                matchups = await self.bot.sleeper.get_matchups(self.league_id, week)
            except Exception:
                # Week may not exist yet
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
        
        # Determine playoff placements from bracket/winners bracket
        # For now, we'll infer from playoff matchup results
        await self._determine_placements(teams, playoff_start, through_week)
        
        return list(teams.values())
    
    async def _determine_placements(
        self,
        teams: dict[int, TeamStats],
        playoff_start: int,
        through_week: int,
    ) -> None:
        """Determine playoff placements from matchup results.
        
        This uses a simple heuristic based on the final playoff week matchups.
        For more accuracy, the Sleeper API's bracket endpoint should be used.
        """
        # Try to get the championship week matchups (typically week 16 or 17)
        championship_week = min(through_week, 17)
        
        try:
            final_matchups = await self.bot.sleeper.get_matchups(
                self.league_id, championship_week
            )
        except Exception:
            return
        
        if not final_matchups:
            return
        
        # Find the championship matchup (matchup_id = 1 typically)
        championship_teams = [m for m in final_matchups if m.get("matchup_id") == 1]
        
        if len(championship_teams) == 2:
            # Determine winner and runner-up
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
        
        # Find 3rd place game (matchup_id = 2 typically)
        third_place_teams = [m for m in final_matchups if m.get("matchup_id") == 2]
        
        if len(third_place_teams) == 2:
            team1, team2 = third_place_teams
            pts1 = team1.get("points", 0) or 0
            pts2 = team2.get("points", 0) or 0
            
            winner_id = team1["roster_id"] if pts1 > pts2 else team2["roster_id"]
            
            if winner_id in teams:
                teams[winner_id].placement = 3
    
    @app_commands.command(
        name="payouts",
        description="Calculate season payouts based on placement and points"
    )
    @app_commands.describe(
        through_week="Last week to include (default: 17 for full season)"
    )
    async def payouts(
        self,
        interaction: discord.Interaction,
        through_week: int = 17,
    ) -> None:
        """Display season payouts for all teams."""
        await interaction.response.defer()
        
        try:
            teams = await self._fetch_team_stats(through_week=through_week)
            teams = calculate_payouts(teams)
            
            # Sort by total payout, then points
            teams.sort(key=lambda t: (t.total_payout, t.total_points), reverse=True)
            
            # Get league info for season
            league = await self.bot.sleeper.get_league(self.league_id)
            season = league.get("season", datetime.now().year)
            
            embed = discord.Embed(
                title="💰 Season Payouts",
                description=f"**{season} Season** (through week {through_week})",
                color=discord.Color.gold(),
            )
            
            # Build payout table
            payout_text = "```\n"
            payout_text += f"{'Team':<22} {'Place':>6} {'Pts':>5} {'Total':>6}\n"
            payout_text += "-" * 42 + "\n"
            
            total_pot = 0
            for team in teams:
                place_str = f"${team.placement_payout}" if team.placement_payout else "-"
                pts_str = f"${team.points_payout}" if team.points_payout else "-"
                total_str = f"${team.total_payout}" if team.total_payout else "-"
                
                payout_text += (
                    f"{team.team_name[:21]:<22} "
                    f"{place_str:>6} {pts_str:>5} {total_str:>6}\n"
                )
                total_pot += team.total_payout
            
            payout_text += "-" * 42 + "\n"
            payout_text += f"{'Total Paid Out':<22} {'':<6} {'':<5} ${total_pot:>5}\n"
            payout_text += "```"
            
            embed.add_field(name="Payouts", value=payout_text, inline=False)
            
            # Add payout legend
            embed.add_field(
                name="Payout Structure",
                value=(
                    "**Placement:** 1st $30, 2nd $20, 3rd $10\n"
                    "**Points:** 1st $30, 2nd $20, 3rd $10"
                ),
                inline=False,
            )
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Payouts command failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error calculating payouts: {e}")
    
    @app_commands.command(
        name="draftorder",
        description="Calculate rookie draft order based on payouts and MaxPF"
    )
    @app_commands.describe(
        through_week="Last week to include (default: 17 for full season)"
    )
    async def draft_order(
        self,
        interaction: discord.Interaction,
        through_week: int = 17,
    ) -> None:
        """Display the calculated rookie draft order."""
        await interaction.response.defer()
        
        try:
            teams = await self._fetch_team_stats(through_week=through_week)
            teams = calculate_payouts(teams)
            draft_order = calculate_draft_order(teams)
            
            # Get league info
            league = await self.bot.sleeper.get_league(self.league_id)
            season = league.get("season", datetime.now().year)
            next_season = int(season) + 1
            
            embed = discord.Embed(
                title="🏈 Rookie Draft Order",
                description=(
                    f"**{next_season} Rookie Draft**\n"
                    f"Based on {season} season results (through week {through_week})"
                ),
                color=discord.Color.blue(),
            )
            
            # Build draft order table
            order_text = "```\n"
            order_text += f"{'Pick':<5} {'Team':<20} {'MaxPF':>10} {'Won':>7}\n"
            order_text += "-" * 45 + "\n"
            
            for pick, team in enumerate(draft_order, start=1):
                payout_str = f"${team.total_payout}" if team.total_payout else "-"
                order_text += (
                    f"{pick:<5} {team.team_name[:19]:<20} "
                    f"{team.max_pf:>10.2f} {payout_str:>7}\n"
                )
            
            order_text += "```"
            
            embed.add_field(name="Draft Order", value=order_text, inline=False)
            
            # Add explanation
            embed.add_field(
                name="How It's Calculated",
                value=(
                    "1️⃣ Money winners pick **last** (most $ = last pick)\n"
                    "2️⃣ Non-winners ordered by **MaxPF** (lowest = first pick)\n"
                    "3️⃣ Ties broken by MaxPF"
                ),
                inline=False,
            )
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Draft order command failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error calculating draft order: {e}")
    
    @app_commands.command(
        name="seasonreport",
        description="Full season report with payouts, MaxPF, and draft order"
    )
    @app_commands.describe(
        through_week="Last week to include (default: 17 for full season)"
    )
    async def season_report(
        self,
        interaction: discord.Interaction,
        through_week: int = 17,
    ) -> None:
        """Display a comprehensive season report."""
        await interaction.response.defer()
        
        try:
            teams = await self._fetch_team_stats(through_week=through_week)
            teams = calculate_payouts(teams)
            draft_order = calculate_draft_order(teams)
            
            # Get league info
            league = await self.bot.sleeper.get_league(self.league_id)
            season = league.get("season", datetime.now().year)
            
            embed = discord.Embed(
                title=f"📊 {season} Season Report",
                description=f"Complete season summary through week {through_week}",
                color=discord.Color.purple(),
            )
            
            # Points leaders
            by_points = sorted(teams, key=lambda t: t.total_points, reverse=True)
            points_text = ""
            for i, team in enumerate(by_points[:3], start=1):
                medal = ["🥇", "🥈", "🥉"][i-1]
                points_text += f"{medal} **{team.team_name}** - {team.total_points:.2f} pts\n"
            embed.add_field(name="📈 Points Leaders", value=points_text, inline=True)
            
            # Placement winners
            placement_teams = [t for t in teams if t.placement]
            placement_teams.sort(key=lambda t: t.placement or 99)
            place_text = ""
            for team in placement_teams[:3]:
                medal = ["🏆", "🥈", "🥉"][team.placement - 1]
                place_text += f"{medal} **{team.team_name}**\n"
            if place_text:
                embed.add_field(name="🏆 Final Standings", value=place_text, inline=True)
            
            # Total payouts
            total_paid = sum(t.total_payout for t in teams)
            winners = [t for t in teams if t.total_payout > 0]
            winners.sort(key=lambda t: t.total_payout, reverse=True)
            
            payout_text = ""
            for team in winners:
                payout_text += f"**{team.team_name}** - ${team.total_payout}\n"
            payout_text += f"\n*Total: ${total_paid}*"
            embed.add_field(name="💰 Payouts", value=payout_text, inline=False)
            
            # Draft order preview (first 3 and last 3)
            draft_text = "**First 3 Picks:**\n"
            for i, team in enumerate(draft_order[:3], start=1):
                draft_text += f"{i}. {team.team_name}\n"
            
            draft_text += "\n**Last 3 Picks:**\n"
            for i, team in enumerate(draft_order[-3:], start=len(draft_order)-2):
                draft_text += f"{i}. {team.team_name}\n"
            
            embed.add_field(name="🏈 Draft Order Preview", value=draft_text, inline=False)
            
            embed.set_footer(text="Use /draftorder for full draft order")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Season report failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error generating report: {e}")


async def setup(bot: "DynastyBot") -> None:
    """Load the Draft Calculator cog."""
    await bot.add_cog(DraftCalculator(bot))
