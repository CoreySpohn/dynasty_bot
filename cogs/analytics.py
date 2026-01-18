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

from config import SLEEPER_LEAGUE_ID
from database import db
from lib.plotting import render_power_rankings

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.analytics")

# Thread pool for CPU-intensive operations
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analytics")

# Position eligibility for flex slots
FLEX_POSITIONS = {
    "FLEX": ["RB", "WR", "TE"],
    "SUPER_FLEX": ["QB", "RB", "WR", "TE"],
    "REC_FLEX": ["WR", "TE"],
    "WRRB_FLEX": ["WR", "RB"],
}


def calculate_optimal_lineup(
    roster_players: list[dict],
    roster_positions: list[str],
) -> float:
    """Calculate maximum potential points for an optimal lineup.
    
    Greedily assigns the highest-scoring players to each roster slot,
    respecting position eligibility rules.
    
    This function runs in a thread pool to avoid blocking the event loop.
    
    Args:
        roster_players: List of dicts with 'position' and 'points' keys.
        roster_positions: List of roster slot positions from league settings.
        
    Returns:
        Maximum potential points for the optimal lineup.
    """
    # Sort players by points descending
    available = sorted(roster_players, key=lambda x: x.get("points", 0), reverse=True)
    used_indices = set()
    total_points = 0.0
    
    # Only count starter positions (not BN, IR)
    starter_positions = [p for p in roster_positions if p not in ("BN", "IR")]
    
    for slot in starter_positions:
        # Determine eligible positions for this slot
        if slot in FLEX_POSITIONS:
            eligible = FLEX_POSITIONS[slot]
        else:
            eligible = [slot]
        
        # Find best available player for this slot
        for idx, player in enumerate(available):
            if idx in used_indices:
                continue
            if player.get("position") in eligible:
                total_points += player.get("points", 0)
                used_indices.add(idx)
                break
    
    return total_points


def generate_power_rankings_sync(
    rosters: list[dict],
    matchups_by_week: dict[int, list[dict]],
    users: dict[str, str],
    players: dict[str, dict],
    roster_positions: list[str],
    current_week: int,
    season: int,
) -> pd.DataFrame:
    """Generate power rankings DataFrame (blocking, runs in executor).
    
    Calculates various metrics for each team:
    - Max Potential Points (optimal lineup each week)
    - Points For / Against
    - Record and Win %
    - Power Level score
    
    Args:
        rosters: List of roster dicts from Sleeper API.
        matchups_by_week: Dict mapping week number to list of matchups.
        users: Dict mapping owner_id to display_name.
        players: Dict mapping player_id to player data.
        roster_positions: League roster positions.
        current_week: Current NFL week.
        season: Current season year.
        
    Returns:
        DataFrame with power rankings data, sorted by Power Level.
    """
    rankings_data = []
    
    # Build roster lookup
    roster_lookup = {r["roster_id"]: r for r in rosters}
    
    for roster in rosters:
        roster_id = roster["roster_id"]
        owner_id = roster.get("owner_id", "")
        owner_name = users.get(owner_id, f"Team {roster_id}")
        
        # Initialize accumulators
        total_potential = 0.0
        total_points_for = 0.0
        total_points_against = 0.0
        wins = 0
        losses = 0
        
        # Calculate stats for each week
        for week in range(1, current_week + 1):
            week_matchups = matchups_by_week.get(week, [])
            
            # Find this team's matchup
            team_matchup = None
            opponent_matchup = None
            
            for m in week_matchups:
                if m.get("roster_id") == roster_id:
                    team_matchup = m
                    matchup_id = m.get("matchup_id")
                    # Find opponent
                    for o in week_matchups:
                        if (
                            o.get("matchup_id") == matchup_id
                            and o.get("roster_id") != roster_id
                        ):
                            opponent_matchup = o
                            break
                    break
            
            if not team_matchup:
                continue
            
            # Get points scored
            points_for = team_matchup.get("points", 0) or 0
            total_points_for += points_for
            
            if opponent_matchup:
                points_against = opponent_matchup.get("points", 0) or 0
                total_points_against += points_against
                
                if points_for > points_against:
                    wins += 1
                elif points_for < points_against:
                    losses += 1
            
            # Calculate potential points for this week
            players_points = team_matchup.get("players_points", {})
            roster_players = []
            
            for player_id, pts in players_points.items():
                player_data = players.get(player_id, {})
                roster_players.append({
                    "position": player_data.get("position", ""),
                    "points": pts or 0,
                })
            
            week_potential = calculate_optimal_lineup(roster_players, roster_positions)
            total_potential += week_potential
        
        # Calculate derived stats
        games_played = wins + losses
        win_pct = (wins / games_played * 100) if games_played > 0 else 0
        avg_points = total_points_for / current_week if current_week > 0 else 0
        
        # Calculate power level (weighted score)
        # Weights: Potential Points (40%), Win% (30%), Avg Points (30%)
        power_level = (
            (total_potential / 100) * 0.4
            + win_pct * 0.3
            + avg_points * 0.3
        )
        
        rankings_data.append({
            "Owner": owner_name,
            "Power Level": round(power_level, 1),
            "Potential Points": round(total_potential, 1),
            "Points For": round(total_points_for, 1),
            "Average Points": round(avg_points, 1),
            "Record": f"{wins}-{losses}",
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
                    "win percentage, and average points scored."
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
            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users_list = await self.bot.sleeper.get_users(self.league_id)
            league = await self.bot.sleeper.get_league(self.league_id)
            
            # Build user lookup
            users = {u["user_id"]: u.get("display_name", "Unknown") for u in users_list}
            
            # Build standings data
            standings = []
            for roster in rosters:
                owner_id = roster.get("owner_id", "")
                owner_name = users.get(owner_id, f"Team {roster['roster_id']}")
                
                settings = roster.get("settings", {})
                wins = settings.get("wins", 0)
                losses = settings.get("losses", 0)
                ties = settings.get("ties", 0)
                points_for = settings.get("fpts", 0) + settings.get("fpts_decimal", 0) / 100
                points_against = settings.get("fpts_against", 0) + settings.get("fpts_against_decimal", 0) / 100
                
                standings.append({
                    "owner": owner_name,
                    "wins": wins,
                    "losses": losses,
                    "ties": ties,
                    "pf": points_for,
                    "pa": points_against,
                })
            
            # Sort by wins, then points for
            standings.sort(key=lambda x: (x["wins"], x["pf"]), reverse=True)
            
            # Build embed
            embed = discord.Embed(
                title="📊 League Standings",
                color=discord.Color.blue(),
            )
            
            standings_text = "```\n"
            standings_text += f"{'#':<3} {'Team':<18} {'Record':<10} {'PF':>8}\n"
            standings_text += "-" * 42 + "\n"
            
            for idx, team in enumerate(standings, 1):
                record = f"{team['wins']}-{team['losses']}"
                if team["ties"]:
                    record += f"-{team['ties']}"
                standings_text += f"{idx:<3} {team['owner'][:17]:<18} {record:<10} {team['pf']:>8.1f}\n"
            
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
