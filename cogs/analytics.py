"""Analytics Cog for Dynasty Bot.

Provides power rankings and team analytics using NFLverse data
and optimal lineup calculations.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Optional

import discord
import pandas as pd
from discord import app_commands
from discord.ext import commands

from cogs.trade_values import get_team_dynasty_values
from config import SLEEPER_LEAGUE_ID
from database import db
from lib.plotting import render_power_rankings
from lib.results import (
    FLEX_POSITIONS,
    TeamRecord,
    build_records,
    build_week_results,
    calculate_optimal_lineup,
)
from lib.standings import compute_standings

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.analytics")

# Thread pool for CPU-intensive operations
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analytics")

# FLEX_POSITIONS and calculate_optimal_lineup now live in lib/results.py,
# which is the single place weekly results get reconstructed. They're
# re-exported here because cogs/draft.py, three scripts and two test
# modules already import them from this module.
__all__ = [
    "Analytics",
    "FLEX_POSITIONS",
    "calculate_optimal_lineup",
    "generate_power_rankings_sync",
]


def generate_power_rankings_sync(
    rosters: list[dict],
    matchups_by_week: dict[int, list[dict]],
    users: dict[str, str],
    players: dict[str, dict],
    roster_positions: list[str],
    current_week: int,
    season: int,
    dynasty_values: Optional[dict[int, int]] = None,
) -> pd.DataFrame:
    """Generate power rankings DataFrame (blocking, runs in executor).

    Calculates various metrics for each team:
    - Max Potential Points (optimal lineup each week)
    - Points For / Against
    - Record and Win %
    - Dynasty roster value (KeepTradeCut, Superflex)
    - Power Level score

    Args:
        rosters: List of roster dicts from Sleeper API.
        matchups_by_week: Dict mapping week number to list of matchups.
        users: Dict mapping owner_id to display_name.
        players: Dict mapping player_id to player data.
        roster_positions: League roster positions.
        current_week: Current NFL week.
        season: Current season year.
        dynasty_values: Dict mapping roster_id to total KTC superflex roster
            value. Teams missing from this dict (e.g. no sync has run yet)
            contribute 0 to their Power Level.

    Returns:
        DataFrame with power rankings data, sorted by Power Level.
    """
    dynasty_values = dynasty_values or {}
    rankings_data = []

    # Reconstruct every week through the shared derivation layer rather than
    # re-deriving matchup pairing and optimal lineups here. lib/results.py is
    # the one place that logic lives now, so awards, the shame wall and the
    # luck index all agree with these numbers by construction.
    all_results = []
    for week in range(1, current_week + 1):
        all_results.extend(
            build_week_results(
                matchups_by_week.get(week, []),
                players,
                roster_positions,
                season,
                week,
            )
        )
    records = build_records(all_results)

    for roster in rosters:
        roster_id = roster["roster_id"]
        owner_id = roster.get("owner_id", "")
        owner_name = users.get(owner_id, f"Team {roster_id}")

        record = records.get(roster_id) or TeamRecord(roster_id)
        total_potential = record.optimal_points
        total_points_for = record.points_for
        wins, losses = record.wins, record.losses
        win_pct = record.win_pct
        avg_points = total_points_for / current_week if current_week > 0 else 0

        dynasty_value = dynasty_values.get(roster_id, 0)

        # Calculate power level (weighted score)
        # Weights: Potential Points (35%), Win% (25%), Avg Points (25%),
        # Dynasty Value (15%) - roster's KTC superflex value, scaled down
        # to be comparable in magnitude to the performance-based terms.
        power_level = (
            (total_potential / 100) * 0.35
            + win_pct * 0.25
            + avg_points * 0.25
            + (dynasty_value / 1000) * 0.15
        )

        rankings_data.append({
            "Owner": owner_name,
            "Power Level": round(power_level, 1),
            "Potential Points": round(total_potential, 1),
            "Points For": round(total_points_for, 1),
            "Average Points": round(avg_points, 1),
            "Dynasty Value": dynasty_value,
            "Record": record.record_text,
            "Win %": f"{win_pct:.0f}%",
        })
    
    # Create DataFrame and sort by Power Level
    df = pd.DataFrame(rankings_data)
    df = df.sort_values("Power Level", ascending=False).reset_index(drop=True)
    
    return df


class Analytics(commands.Cog):
    """Provides power rankings and analytics commands.
    
    Uses NFLverse data and calculates optimal lineups to generate
    comprehensive team rankings and statistics.
    """
    
    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID
    
    async def cog_load(self) -> None:
        """Called when the cog is loaded."""
        logger.info("Analytics cog loaded")
    
    async def cog_unload(self) -> None:
        """Called when the cog is unloaded."""
        _executor.shutdown(wait=False)
    
    @app_commands.command(
        name="rankings",
        description="Generate Power Rankings based on Max Potential Points"
    )
    async def rankings(self, interaction: discord.Interaction) -> None:
        """Generate and display power rankings for the league."""
        await interaction.response.defer()
        
        try:
            # Fetch all required data from Sleeper API
            logger.info("Fetching league data for power rankings...")
            
            league = await self.bot.sleeper.get_league(self.league_id)
            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users_list = await self.bot.sleeper.get_users(self.league_id)
            players = await self.bot.sleeper.get_all_players()
            
            # Get league settings
            current_week = league.get("settings", {}).get("leg", 1)
            season = int(league.get("season", datetime.now().year))
            roster_positions = league.get("roster_positions", [])
            
            # Build user lookup
            users = {u["user_id"]: u.get("display_name", "Unknown") for u in users_list}
            
            # Fetch matchups for all weeks (this is the slow part)
            logger.info(f"Fetching matchups for weeks 1-{current_week}...")
            matchups_by_week = {}
            
            for week in range(1, current_week + 1):
                matchups_by_week[week] = await self.bot.sleeper.get_matchups(
                    self.league_id, week
                )

            # Dynasty roster value (KeepTradeCut) feeds into the Power Level score
            dynasty_values = await get_team_dynasty_values(rosters)

            # Run heavy calculation in thread pool to avoid blocking
            logger.info("Calculating power rankings (in executor)...")

            loop = asyncio.get_event_loop()
            rankings_df = await loop.run_in_executor(
                _executor,
                partial(
                    generate_power_rankings_sync,
                    rosters,
                    matchups_by_week,
                    users,
                    players,
                    roster_positions,
                    current_week,
                    season,
                    dynasty_values,
                ),
            )
            
            # Generate the image (also CPU-intensive)
            logger.info("Rendering rankings image...")
            
            image_buffer = await loop.run_in_executor(
                _executor,
                partial(render_power_rankings, rankings_df, current_week, season),
            )
            
            # Send the image
            file = discord.File(image_buffer, filename="power_rankings.png")
            
            embed = discord.Embed(
                title="⚡ Power Rankings",
                description=(
                    f"**Week {current_week}** • {season} Season\n\n"
                    "Rankings based on Max Potential Points (optimal lineup each week), "
                    "win percentage, average points scored, and dynasty roster value "
                    "(KeepTradeCut)."
                ),
                color=discord.Color.gold(),
            )
            embed.set_image(url="attachment://power_rankings.png")
            embed.set_footer(text="Updated just now")
            embed.timestamp = datetime.now()
            
            await interaction.followup.send(embed=embed, file=file)
            
            # Save to database for historical tracking
            await self._save_rankings(rankings_df, current_week, season, users_list)
            
            logger.info("Power rankings generated successfully")
            
        except Exception as e:
            logger.error(f"Rankings command failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error generating rankings: {e}")
    
    async def _save_rankings(
        self,
        rankings_df: pd.DataFrame,
        week: int,
        season: int,
        users_list: list[dict],
    ) -> None:
        """Save power rankings to the database for historical tracking."""
        # Build reverse lookup: display_name -> user_id
        name_to_id = {u.get("display_name", ""): u["user_id"] for u in users_list}
        
        for idx, row in rankings_df.iterrows():
            owner_name = row["Owner"]
            user_id = name_to_id.get(owner_name, owner_name)
            power_level = row["Power Level"]
            rank = idx + 1
            
            try:
                async with db.execute(
                    """
                    INSERT INTO power_rankings (user_id, team_name, rank, score, week, season)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, owner_name, rank, power_level, week, season),
                ):
                    pass
            except Exception as e:
                logger.warning(f"Failed to save ranking for {owner_name}: {e}")
    
    @app_commands.command(
        name="standings",
        description="View current league standings"
    )
    async def standings(self, interaction: discord.Interaction) -> None:
        """Display current league standings."""
        await interaction.response.defer()
        
        try:
            league = await self.bot.sleeper.get_league(self.league_id)
            standings = await compute_standings(self.bot.sleeper, self.league_id)

            # Build embed
            embed = discord.Embed(
                title="📊 League Standings",
                color=discord.Color.blue(),
            )

            standings_text = "```\n"
            standings_text += f"{'#':<3} {'Team':<18} {'Record':<10} {'PF':>8}\n"
            standings_text += "-" * 42 + "\n"

            for team in standings:
                record = f"{team.wins}-{team.losses}"
                if team.ties:
                    record += f"-{team.ties}"
                standings_text += f"{team.rank:<3} {team.owner[:17]:<18} {record:<10} {team.pf:>8.1f}\n"

            standings_text += "```"
            embed.description = standings_text
            
            season = league.get("season", datetime.now().year)
            current_week = league.get("settings", {}).get("leg", 1)
            embed.set_footer(text=f"Week {current_week} • {season} Season")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Standings command failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error fetching standings: {e}")
    
    @app_commands.command(
        name="matchups",
        description="View current week's matchups"
    )
    @app_commands.describe(week="Week number (defaults to current week)")
    async def matchups(
        self, interaction: discord.Interaction, week: Optional[int] = None
    ) -> None:
        """Display matchups for a given week."""
        await interaction.response.defer()
        
        try:
            league = await self.bot.sleeper.get_league(self.league_id)
            current_week = league.get("settings", {}).get("leg", 1)
            
            if week is None:
                week = current_week
            
            matchups = await self.bot.sleeper.get_matchups(self.league_id, week)
            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users_list = await self.bot.sleeper.get_users(self.league_id)
            
            # Build lookups
            users = {u["user_id"]: u.get("display_name", "Unknown") for u in users_list}
            roster_to_owner = {}
            for roster in rosters:
                roster_id = roster["roster_id"]
                owner_id = roster.get("owner_id", "")
                roster_to_owner[roster_id] = users.get(owner_id, f"Team {roster_id}")
            
            # Group matchups
            matchup_groups = {}
            for m in matchups:
                matchup_id = m.get("matchup_id")
                if matchup_id not in matchup_groups:
                    matchup_groups[matchup_id] = []
                matchup_groups[matchup_id].append(m)
            
            embed = discord.Embed(
                title=f"🏈 Week {week} Matchups",
                color=discord.Color.green(),
            )
            
            for matchup_id, teams in matchup_groups.items():
                if len(teams) != 2:
                    continue
                
                team1, team2 = teams
                name1 = roster_to_owner.get(team1["roster_id"], "???")
                name2 = roster_to_owner.get(team2["roster_id"], "???")
                pts1 = team1.get("points", 0) or 0
                pts2 = team2.get("points", 0) or 0
                
                # Determine leader
                if pts1 > pts2:
                    display = f"**{name1}** ({pts1:.1f}) vs {name2} ({pts2:.1f})"
                elif pts2 > pts1:
                    display = f"{name1} ({pts1:.1f}) vs **{name2}** ({pts2:.1f})"
                else:
                    display = f"{name1} ({pts1:.1f}) vs {name2} ({pts2:.1f})"
                
                embed.add_field(
                    name=f"Matchup {matchup_id}",
                    value=display,
                    inline=False,
                )
            
            season = league.get("season", datetime.now().year)
            embed.set_footer(text=f"{season} Season")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Matchups command failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error fetching matchups: {e}")


async def setup(bot: "DynastyBot") -> None:
    """Load the Analytics cog."""
    await bot.add_cog(Analytics(bot))
