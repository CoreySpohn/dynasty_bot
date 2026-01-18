"""Primetime Player Watch Cog for Dynasty Bot.

Posts alerts about players to watch in Sunday Night and Monday Night
Football games who could affect close fantasy matchups.

Runs at 8:00 PM EST on Sundays and Mondays during the season.
"""

import asyncio
import logging
import os
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import SLEEPER_LEAGUE_ID

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.primetime")

# Eastern time zone
EST = ZoneInfo("America/New_York")

# Alert time: 8:00 PM EST
ALERT_TIME = time(20, 0)

# Close matchup threshold (points)
CLOSE_MATCHUP_THRESHOLD = 30.0


class PrimetimeWatch(commands.Cog):
    """Alerts for primetime games affecting fantasy matchups."""
    
    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID
        
        # Channel for alerts
        self.alert_channel_id = int(os.getenv("PRIMETIME_CHANNEL_ID", 0)) or \
                                int(os.getenv("RUMORS_CHANNEL_ID", 0))
        
        # Cache
        self._players: Optional[dict] = None
        self._roster_to_team: Optional[dict[int, str]] = None
        self._roster_to_players: Optional[dict[int, list[str]]] = None
        
        # Start scheduled task
        self.primetime_alert_task.start()
    
    async def cog_load(self) -> None:
        """Called when the cog is loaded."""
        logger.info("Primetime Watch cog loaded")
        if not self.alert_channel_id:
            logger.warning("PRIMETIME_CHANNEL_ID not configured")
    
    async def cog_unload(self) -> None:
        """Called when the cog is unloaded."""
        self.primetime_alert_task.cancel()
        logger.info("Primetime Watch cog unloaded")
    
    async def _get_players(self) -> dict:
        """Get player data."""
        if self._players:
            return self._players
        self._players = await self.bot.sleeper.get_all_players()
        return self._players
    
    async def _get_roster_lookup(self) -> tuple[dict[int, str], dict[int, list[str]]]:
        """Get roster_id to team name and roster_id to players mappings."""
        if self._roster_to_team and self._roster_to_players:
            return self._roster_to_team, self._roster_to_players
        
        rosters = await self.bot.sleeper.get_rosters(self.league_id)
        users = await self.bot.sleeper.get_users(self.league_id)
        
        user_lookup = {}
        for user in users:
            user_id = user.get("user_id")
            team_name = user.get("metadata", {}).get("team_name") or user.get("display_name")
            user_lookup[user_id] = team_name
        
        self._roster_to_team = {}
        self._roster_to_players = {}
        
        for roster in rosters:
            roster_id = roster.get("roster_id")
            owner_id = roster.get("owner_id", "")
            self._roster_to_team[roster_id] = user_lookup.get(owner_id, f"Team {roster_id}")
            
            # Get players on this roster (starters only for primetime)
            starters = roster.get("starters", [])
            self._roster_to_players[roster_id] = starters
        
        return self._roster_to_team, self._roster_to_players
    
    async def _get_current_week(self) -> int:
        """Get the current NFL week from league settings."""
        try:
            league = await self.bot.sleeper.get_league(self.league_id)
            state = await self.bot.sleeper._get("/state/nfl")
            return state.get("week", league.get("settings", {}).get("leg", 1))
        except Exception as e:
            logger.error(f"Failed to get current week: {e}")
            return 1
    
    async def _get_close_matchups(
        self,
        week: int,
    ) -> list[dict]:
        """Find matchups within the close threshold.
        
        Returns:
            List of dicts with matchup details including relevant players.
        """
        matchups = await self.bot.sleeper.get_matchups(self.league_id, week)
        roster_to_team, roster_to_players = await self._get_roster_lookup()
        
        # Group by matchup_id
        matchup_groups: dict[int, list[dict]] = {}
        for m in matchups:
            matchup_id = m.get("matchup_id")
            if matchup_id is None:
                continue
            if matchup_id not in matchup_groups:
                matchup_groups[matchup_id] = []
            matchup_groups[matchup_id].append(m)
        
        close_matchups = []
        
        for matchup_id, teams in matchup_groups.items():
            if len(teams) != 2:
                continue
            
            team1, team2 = teams
            roster1 = team1.get("roster_id")
            roster2 = team2.get("roster_id")
            points1 = team1.get("points", 0) or 0
            points2 = team2.get("points", 0) or 0
            
            diff = abs(points1 - points2)
            
            if diff <= CLOSE_MATCHUP_THRESHOLD:
                close_matchups.append({
                    "matchup_id": matchup_id,
                    "team1_name": roster_to_team.get(roster1, f"Team {roster1}"),
                    "team1_points": points1,
                    "team1_roster_id": roster1,
                    "team1_starters": team1.get("starters", []),
                    "team1_starters_points": team1.get("starters_points", []),
                    "team2_name": roster_to_team.get(roster2, f"Team {roster2}"),
                    "team2_points": points2,
                    "team2_roster_id": roster2,
                    "team2_starters": team2.get("starters", []),
                    "team2_starters_points": team2.get("starters_points", []),
                    "diff": diff,
                })
        
        return close_matchups
    
    def _find_primetime_players(
        self,
        close_matchups: list[dict],
        players: dict,
        primetime_teams: list[str],
    ) -> list[dict]:
        """Find players in primetime games who could affect matchups.
        
        Args:
            close_matchups: List of close matchup dicts.
            players: Player data dictionary.
            primetime_teams: List of NFL team abbreviations in primetime.
            
        Returns:
            List of dicts with player and matchup info.
        """
        primetime_players = []
        
        for matchup in close_matchups:
            # Check both teams' starters
            for team_key in ["team1", "team2"]:
                starters = matchup.get(f"{team_key}_starters", [])
                starters_points = matchup.get(f"{team_key}_starters_points", [])
                team_name = matchup.get(f"{team_key}_name")
                team_points = matchup.get(f"{team_key}_points")
                
                other_key = "team2" if team_key == "team1" else "team1"
                opponent_name = matchup.get(f"{other_key}_name")
                opponent_points = matchup.get(f"{other_key}_points")
                
                for i, player_id in enumerate(starters):
                    if not player_id or player_id == "0":
                        continue
                    
                    player = players.get(player_id, {})
                    nfl_team = player.get("team", "")
                    
                    # Check if player's NFL team is in primetime
                    if nfl_team in primetime_teams:
                        # Check if player has scored yet (if 0, they likely haven't played)
                        points_scored = starters_points[i] if i < len(starters_points) else 0
                        
                        if points_scored == 0 or points_scored is None:
                            player_name = player.get("full_name", f"Player {player_id}")
                            position = player.get("position", "")
                            
                            # Calculate points needed
                            diff = matchup.get("diff")
                            
                            primetime_players.append({
                                "player_name": player_name,
                                "position": position,
                                "nfl_team": nfl_team,
                                "fantasy_team": team_name,
                                "fantasy_points": team_points,
                                "opponent": opponent_name,
                                "opponent_points": opponent_points,
                                "points_diff": diff,
                                "matchup_id": matchup.get("matchup_id"),
                            })
        
        return primetime_players
    
    async def _post_primetime_alert(
        self,
        players_to_watch: list[dict],
        is_monday: bool = False,
    ) -> bool:
        """Post the primetime player watch alert.
        
        Args:
            players_to_watch: List of primetime player dicts.
            is_monday: True for MNF, False for SNF.
            
        Returns:
            True if posted successfully.
        """
        if not self.alert_channel_id:
            logger.warning("Cannot post primetime alert - channel not configured")
            return False
        
        channel = self.bot.get_channel(self.alert_channel_id)
        if not channel:
            logger.error(f"Channel {self.alert_channel_id} not found")
            return False
        
        game_type = "MONDAY NIGHT" if is_monday else "SUNDAY NIGHT"
        emoji = "🌙" if is_monday else "🌆"
        
        embed = discord.Embed(
            title=f"{emoji} {game_type} SPOTLIGHT",
            description="Players to watch who could swing close matchups!",
            color=discord.Color.gold(),
            timestamp=datetime.now(),
        )
        
        # Group by matchup
        matchup_groups: dict[int, list[dict]] = {}
        for p in players_to_watch:
            mid = p.get("matchup_id")
            if mid not in matchup_groups:
                matchup_groups[mid] = []
            matchup_groups[mid].append(p)
        
        for matchup_id, players in matchup_groups.items():
            first_player = players[0]
            matchup_title = (
                f"{first_player['fantasy_team']} ({first_player['fantasy_points']:.1f}) vs "
                f"{first_player['opponent']} ({first_player['opponent_points']:.1f})"
            )
            
            player_lines = []
            for p in players:
                diff = first_player['points_diff']
                player_lines.append(
                    f"⚡ **{p['player_name']}** ({p['position']}, {p['nfl_team']}) - "
                    f"{p['fantasy_team']}"
                )
            
            embed.add_field(
                name=matchup_title,
                value="\n".join(player_lines),
                inline=False,
            )
        
        embed.set_footer(text="Good luck! 🏈")
        
        try:
            await channel.send(embed=embed)
            logger.info(f"Posted {game_type} spotlight with {len(players_to_watch)} players")
            return True
        except Exception as e:
            logger.error(f"Failed to post primetime alert: {e}")
            return False
    
    @tasks.loop(hours=1)
    async def primetime_alert_task(self) -> None:
        """Check if it's time to post a primetime alert."""
        await self.bot.wait_until_ready()
        
        now = datetime.now(EST)
        current_time = now.time()
        weekday = now.weekday()  # 0=Monday, 6=Sunday
        
        # Check if it's Sunday (6) or Monday (0) at 8 PM EST
        is_alert_time = (
            weekday in (0, 6) and
            current_time.hour == ALERT_TIME.hour and
            current_time.minute < 30  # Within first 30 minutes of the hour
        )
        
        if not is_alert_time:
            return
        
        is_monday = weekday == 0
        
        # Get SNF/MNF teams (would need to be updated weekly, or fetched from an API)
        # For now, we'll check for players who haven't scored yet
        primetime_teams = await self._get_primetime_teams()
        
        if not primetime_teams:
            logger.info("No primetime teams detected")
            return
        
        logger.info(f"Checking primetime players for teams: {primetime_teams}")
        
        # Get current week
        week = await self._get_current_week()
        
        # Find close matchups
        close_matchups = await self._get_close_matchups(week)
        
        if not close_matchups:
            logger.info("No close matchups this week")
            return
        
        # Get players data
        players = await self._get_players()
        
        # Find primetime players in close matchups
        players_to_watch = self._find_primetime_players(
            close_matchups,
            players,
            primetime_teams,
        )
        
        if not players_to_watch:
            logger.info("No primetime players to watch in close matchups")
            return
        
        # Post the alert
        await self._post_primetime_alert(players_to_watch, is_monday)
    
    async def _get_primetime_teams(self) -> list[str]:
        """Get teams playing in primetime games tonight.
        
        This uses a heuristic: check which players haven't scored yet
        and are on rosters as starters. In the future, this could 
        integrate with an NFL schedule API.
        
        Returns:
            List of NFL team abbreviations playing tonight.
        """
        # Since we don't have a live schedule API integrated,
        # we'll return an empty list and the command version
        # will require manual input of primetime teams
        return []
    
    @primetime_alert_task.before_loop
    async def before_primetime_alert(self) -> None:
        """Wait for bot to be ready."""
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)
    
    @app_commands.command(
        name="primetime",
        description="Show players to watch in tonight's primetime game"
    )
    @app_commands.describe(
        teams="NFL teams playing tonight (comma-separated, e.g., 'KC,BUF')"
    )
    async def primetime_check(
        self,
        interaction: discord.Interaction,
        teams: str,
    ) -> None:
        """Manually check for primetime players to watch."""
        await interaction.response.defer()
        
        # Parse teams
        primetime_teams = [t.strip().upper() for t in teams.split(",")]
        
        # Get current week
        week = await self._get_current_week()
        
        # Find close matchups
        close_matchups = await self._get_close_matchups(week)
        
        if not close_matchups:
            await interaction.followup.send(
                "No matchups are close enough to matter tonight! "
                f"(Threshold: within {CLOSE_MATCHUP_THRESHOLD} points)"
            )
            return
        
        # Get players data
        players = await self._get_players()
        
        # Find primetime players
        players_to_watch = self._find_primetime_players(
            close_matchups,
            players,
            primetime_teams,
        )
        
        if not players_to_watch:
            await interaction.followup.send(
                f"No starters from {', '.join(primetime_teams)} are in close matchups tonight!"
            )
            return
        
        # Build embed
        is_monday = datetime.now().weekday() == 0
        game_type = "MONDAY NIGHT" if is_monday else "PRIMETIME"
        emoji = "🌙" if is_monday else "🌆"
        
        embed = discord.Embed(
            title=f"{emoji} {game_type} SPOTLIGHT",
            description=f"Players from {', '.join(primetime_teams)} who could swing close matchups!",
            color=discord.Color.gold(),
            timestamp=datetime.now(),
        )
        
        # Group by matchup
        matchup_groups: dict[int, list[dict]] = {}
        for p in players_to_watch:
            mid = p.get("matchup_id")
            if mid not in matchup_groups:
                matchup_groups[mid] = []
            matchup_groups[mid].append(p)
        
        for matchup_id, plist in matchup_groups.items():
            first_player = plist[0]
            matchup_title = (
                f"{first_player['fantasy_team']} ({first_player['fantasy_points']:.1f}) vs "
                f"{first_player['opponent']} ({first_player['opponent_points']:.1f})"
            )
            
            player_lines = []
            for p in plist:
                player_lines.append(
                    f"⚡ **{p['player_name']}** ({p['position']}, {p['nfl_team']}) - "
                    f"{p['fantasy_team']}"
                )
            
            embed.add_field(
                name=matchup_title,
                value="\n".join(player_lines),
                inline=False,
            )
        
        embed.set_footer(text="Good luck! 🏈")
        
        await interaction.followup.send(embed=embed)


async def setup(bot: "DynastyBot") -> None:
    """Load the Primetime Watch cog."""
    await bot.add_cog(PrimetimeWatch(bot))
