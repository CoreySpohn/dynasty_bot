"""Taxi Squad Raiding Cog for Dynasty Bot.

Manages the taxi squad raiding system where owners can poach players
from other teams' taxi squads at a draft pick cost based on
the player's original draft round.
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import ALERT_CHANNEL_ID, SLEEPER_LEAGUE_ID
from database import db
from lib.members import get_member_registry

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.taxi")


def ordinal(n: int) -> str:
    """Convert an integer to its ordinal string (1st, 2nd, 3rd, etc.)."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def calculate_raid_cost(draft_round: int | str) -> str:
    """Calculate the raid cost based on draft round.
    
    Cost formula: The round drafted + (round - 1)
    Example: Round 3 player costs a 2nd + 3rd round pick
    
    Args:
        draft_round: The round the player was drafted, or "UDFA"
        
    Returns:
        Human-readable cost string (e.g., "2nd & 3rd")
    """
    if draft_round == "UDFA" or not isinstance(draft_round, int):
        # UDFA players cost a 4th round pick (league rule)
        return "4th Round Pick"
    
    if draft_round == 1:
        # 1st round picks just cost a 1st
        return "1st Round Pick"
    
    cost_round = draft_round - 1
    return f"{ordinal(cost_round)} & {ordinal(draft_round)} Round Picks"


class TaxiRaiding(commands.Cog):
    """Manages taxi squad raiding and draft origin lookups.
    
    Provides commands to raid players from other teams' taxi squads
    and calculates the draft pick cost based on original draft position.
    """
    
    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID
        
        # Cache for player name -> player_id lookups
        self._player_name_cache: dict[str, str] = {}
    
    async def cog_load(self) -> None:
        """Called when the cog is loaded."""
        logger.info("Taxi Raiding cog loaded")
        # Start the reminder loop
        self.raid_reminder_loop.start()
    
    async def cog_unload(self) -> None:
        """Called when the cog is unloaded."""
        self.raid_reminder_loop.cancel()
    
    @tasks.loop(hours=24)
    async def raid_reminder_loop(self) -> None:
        """Check pending raids every 24 hours and send reminders."""
        try:
            # Get pending raids from database
            async with db.connection.execute("""
                SELECT id, raider_user_id, victim_user_id, player_id, player_name, raid_date
                FROM raids
                WHERE status = 'pending'
            """) as cursor:
                pending = await cursor.fetchall()
            
            if not pending:
                return
            
            registry = get_member_registry()
            
            for raid_id, raider_id, victim_sleeper_id, player_id, player_name, raid_date in pending:
                # Check if player is still on victim's taxi squad
                result = await self._find_taxi_player_owner(player_id)
                
                if not result:
                    # Player no longer on taxi - raid completed or player moved
                    await db.connection.execute(
                        "UPDATE raids SET status = 'completed' WHERE id = ?",
                        (raid_id,)
                    )
                    await db.connection.commit()
                    logger.info(f"Raid {raid_id} marked completed - player {player_name} no longer on taxi")
                    continue
                
                # Player still on taxi - send reminder
                if ALERT_CHANNEL_ID:
                    channel = self.bot.get_channel(ALERT_CHANNEL_ID)
                    if channel:
                        # Find victim's Discord ID
                        victim_member = registry.find_by_sleeper_id(victim_sleeper_id)
                        victim_mention = f"<@{victim_member.discord_id}>" if victim_member else "Owner"
                        
                        # Find raider mention
                        raider_mention = f"<@{raider_id}>"
                        
                        await channel.send(
                            f"⏰ **RAID REMINDER** ⏰\n"
                            f"{victim_mention} - **{player_name}** is still being raided by {raider_mention}!\n"
                            f"The player must be moved off your taxi squad."
                        )
                        logger.info(f"Sent raid reminder for {player_name}")
        except Exception as e:
            logger.error(f"Error in raid reminder loop: {e}")
    
    @raid_reminder_loop.before_loop
    async def before_raid_reminder(self) -> None:
        """Wait for bot to be ready before starting loop."""
        await self.bot.wait_until_ready()
    
    async def _build_player_name_cache(self) -> None:
        """Build a lowercase name -> player_id lookup cache."""
        if self._player_name_cache:
            return
        
        players = await self.bot.sleeper.get_all_players()
        for player_id, data in players.items():
            # Index by various name formats
            full_name = data.get("full_name", "").lower()
            if full_name:
                self._player_name_cache[full_name] = player_id
            
            # Also index by "first last"
            first = data.get("first_name", "").lower()
            last = data.get("last_name", "").lower()
            if first and last:
                self._player_name_cache[f"{first} {last}"] = player_id
            
            # And just last name for common lookups
            if last and last not in self._player_name_cache:
                self._player_name_cache[last] = player_id
    
    def _find_player_id(self, player_name: str) -> Optional[str]:
        """Find a player ID by name (case-insensitive)."""
        name_lower = player_name.lower().strip()
        
        # Exact match
        if name_lower in self._player_name_cache:
            return self._player_name_cache[name_lower]
        
        # Partial match (starts with)
        for cached_name, player_id in self._player_name_cache.items():
            if cached_name.startswith(name_lower) or name_lower in cached_name:
                return player_id
        
        return None
    
    async def find_draft_origin(
        self, player_id: str, league_id: str, depth: int = 0
    ) -> int | str:
        """Recursively find which round a player was drafted in.
        
        Walks back through previous league seasons to find the original
        draft where this player was selected.
        
        Args:
            player_id: The Sleeper player ID
            league_id: The league ID to check
            depth: Recursion depth (for safety limits)
            
        Returns:
            The draft round (int) or "UDFA" if undrafted
        """
        # Safety limit to prevent infinite recursion
        if depth > 10:
            logger.warning(f"Max recursion depth reached for player {player_id}")
            return "UDFA"
        
        try:
            # Get drafts for this league
            drafts = await self.bot.sleeper.get_drafts(league_id)
            
            for draft in drafts:
                draft_id = draft.get("draft_id")
                if not draft_id:
                    continue
                
                # Get all picks in this draft
                picks = await self.bot.sleeper.get_picks_in_draft(draft_id)
                
                for pick in picks:
                    if pick.get("player_id") == player_id:
                        draft_round = pick.get("round", 1)
                        logger.info(
                            f"Found player {player_id} drafted in round {draft_round} "
                            f"(league: {league_id})"
                        )
                        return draft_round
            
            # Player not found in this league's drafts, check previous league
            league = await self.bot.sleeper.get_league(league_id)
            previous_league_id = league.get("previous_league_id")
            
            if previous_league_id:
                logger.debug(
                    f"Player {player_id} not in {league_id}, "
                    f"checking previous league {previous_league_id}"
                )
                return await self.find_draft_origin(
                    player_id, previous_league_id, depth + 1
                )
            
            # No previous league and not found - must be UDFA
            logger.info(f"Player {player_id} not found in any draft - UDFA")
            return "UDFA"
            
        except Exception as e:
            logger.error(f"Error finding draft origin: {e}", exc_info=True)
            return "UDFA"
    
    async def _find_taxi_player_owner(
        self, player_id: str
    ) -> Optional[tuple[dict, dict]]:
        """Find if a player is on a taxi squad and return owner info.
        
        Returns:
            Tuple of (roster, user) dicts if found, None otherwise
        """
        rosters = await self.bot.sleeper.get_rosters(self.league_id)
        users = await self.bot.sleeper.get_users(self.league_id)
        
        # Build user lookup
        user_lookup = {u["user_id"]: u for u in users}
        
        for roster in rosters:
            taxi = roster.get("taxi") or []
            if player_id in taxi:
                owner_id = roster.get("owner_id")
                user = user_lookup.get(owner_id, {"display_name": f"Team {roster['roster_id']}"})
                return roster, user
        
        return None
    
    @app_commands.command(name="raid", description="Raid a player from another team's taxi squad")
    @app_commands.describe(player_name="Name of the player to raid from a taxi squad")
    async def raid(self, interaction: discord.Interaction, player_name: str) -> None:
        """Initiate a taxi squad raid for a player."""
        await interaction.response.defer()
        
        try:
            # Build name cache if needed
            await self._build_player_name_cache()
            
            # Find the player ID
            player_id = self._find_player_id(player_name)
            if not player_id:
                await interaction.followup.send(
                    f"❌ Could not find player: **{player_name}**\n"
                    "Try using the player's full name."
                )
                return
            
            # Get player data
            players = await self.bot.sleeper.get_all_players()
            player_data = players.get(player_id, {})
            full_name = player_data.get("full_name", player_name)
            position = player_data.get("position", "?")
            team = player_data.get("team", "FA")
            
            # Check if player is on a taxi squad
            result = await self._find_taxi_player_owner(player_id)
            if not result:
                await interaction.followup.send(
                    f"❌ **{full_name}** ({position} - {team}) is not on any taxi squad."
                )
                return
            
            roster, victim_user = result
            victim_name = victim_user.get("display_name", "Unknown")
            victim_user_id = victim_user.get("user_id", "")
            
            # Find the draft origin
            draft_round = await self.find_draft_origin(player_id, self.league_id)
            
            # Calculate cost
            cost_text = calculate_raid_cost(draft_round)
            
            # Get raider info
            raider_name = interaction.user.display_name
            raider_id = str(interaction.user.id)
            
            # Get current week/season
            league = await self.bot.sleeper.get_league(self.league_id)
            current_week = league.get("settings", {}).get("leg", 1)
            season = league.get("season", datetime.now().year)
            
            # Format draft round for display
            draft_display = (
                f"Round {draft_round}" if isinstance(draft_round, int) else draft_round
            )
            
            # Save to database
            await self._save_raid(
                raider_user_id=raider_id,
                raider_team_name=raider_name,
                victim_user_id=victim_user_id,
                victim_team_name=victim_name,
                player_id=player_id,
                player_name=full_name,
                draft_round=str(draft_round),
                cost_text=cost_text,
                week=current_week,
                season=int(season),
            )
            
            # Build response message
            embed = discord.Embed(
                title="🚨 TAXI SQUAD RAID 🚨",
                color=discord.Color.red(),
            )
            embed.add_field(
                name="Player",
                value=f"**{full_name}** ({position} - {team})",
                inline=False,
            )
            embed.add_field(name="Raided From", value=victim_name, inline=True)
            embed.add_field(name="Raided By", value=raider_name, inline=True)
            embed.add_field(name="Draft Origin", value=draft_display, inline=True)
            embed.add_field(
                name="💰 Cost",
                value=f"**{cost_text}**",
                inline=False,
            )
            embed.set_footer(text=f"Week {current_week} • {season} Season")
            embed.timestamp = datetime.now()
            
            await interaction.followup.send(embed=embed)
            
            # Also send to alert channel if configured - with Discord mentions!
            if ALERT_CHANNEL_ID:
                channel = self.bot.get_channel(ALERT_CHANNEL_ID)
                if channel:
                    # Look up Discord IDs from member registry
                    registry = get_member_registry()
                    
                    # Find victim's Discord ID by Sleeper user ID
                    victim_member = registry.find_by_sleeper_id(victim_user_id)
                    victim_mention = f"<@{victim_member.discord_id}>" if victim_member else victim_name
                    
                    # Raider is the Discord user who ran the command
                    raider_mention = f"<@{raider_id}>"
                    
                    await channel.send(
                        f"🚨 **TAXI RAID** 🚨\n"
                        f"{raider_mention} is raiding **{full_name}** from {victim_mention}'s taxi squad!\n"
                        f"📋 Draft Origin: {draft_display}\n"
                        f"💰 Cost: **{cost_text}**\n\n"
                        f"{victim_mention} - you must move this player off your taxi squad!"
                    )
            
        except Exception as e:
            logger.error(f"Raid command failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error processing raid: {e}")
    
    async def _save_raid(
        self,
        raider_user_id: str,
        raider_team_name: str,
        victim_user_id: str,
        victim_team_name: str,
        player_id: str,
        player_name: str,
        draft_round: str,
        cost_text: str,
        week: int,
        season: int,
    ) -> None:
        """Save a raid record to the database."""
        raid_date = datetime.now().isoformat()
        
        async with db.execute(
            """
            INSERT INTO raids (
                raider_user_id, raider_team_name, victim_user_id, victim_team_name,
                player_id, player_name, draft_round, cost_text, raid_date, week, season
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raider_user_id, raider_team_name, victim_user_id, victim_team_name,
                player_id, player_name, draft_round, cost_text, raid_date, week, season,
            ),
        ):
            pass
        
        logger.info(f"Saved raid: {raider_team_name} raided {player_name} from {victim_team_name}")
    
    @app_commands.command(name="raidhistory", description="View taxi squad raid history")
    @app_commands.describe(season="Season year to view (defaults to current)")
    async def raid_history(
        self, interaction: discord.Interaction, season: Optional[int] = None
    ) -> None:
        """Display the raid history for a season."""
        await interaction.response.defer()
        
        try:
            # Get current season if not specified
            if season is None:
                league = await self.bot.sleeper.get_league(self.league_id)
                season = int(league.get("season", datetime.now().year))
            
            # Fetch raids from database
            async with db.execute(
                """
                SELECT player_name, raider_team_name, victim_team_name, cost_text, raid_date
                FROM raids
                WHERE season = ?
                ORDER BY raid_date DESC
                LIMIT 20
                """,
                (season,),
            ) as cursor:
                raids = await cursor.fetchall()
            
            if not raids:
                await interaction.followup.send(f"No raids recorded for the {season} season.")
                return
            
            embed = discord.Embed(
                title=f"🏴‍☠️ Taxi Squad Raids - {season}",
                color=discord.Color.dark_orange(),
            )
            
            for player_name, raider, victim, cost, raid_date in raids:
                # Parse and format date
                try:
                    dt = datetime.fromisoformat(raid_date)
                    date_str = dt.strftime("%b %d")
                except (ValueError, TypeError):
                    date_str = "Unknown"
                
                embed.add_field(
                    name=f"{player_name}",
                    value=f"**{raider}** ← {victim}\nCost: {cost} ({date_str})",
                    inline=False,
                )
            
            embed.set_footer(text=f"Showing up to 20 most recent raids")
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Raid history failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error fetching raid history: {e}")
    
    @app_commands.command(name="taxisquad", description="View a team's current taxi squad")
    @app_commands.describe(team_name="Name of the team owner (optional, shows all if omitted)")
    async def taxi_squad(
        self, interaction: discord.Interaction, team_name: Optional[str] = None
    ) -> None:
        """Display current taxi squad players."""
        await interaction.response.defer()
        
        try:
            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users = await self.bot.sleeper.get_users(self.league_id)
            players = await self.bot.sleeper.get_all_players()
            
            # Build user lookup
            user_lookup = {u["user_id"]: u.get("display_name", "Unknown") for u in users}
            
            embed = discord.Embed(
                title="🚕 Taxi Squads",
                color=discord.Color.blue(),
            )
            
            found_any = False
            
            for roster in rosters:
                owner_id = roster.get("owner_id")
                owner_name = user_lookup.get(owner_id, f"Team {roster['roster_id']}")
                
                # Filter by team name if specified
                if team_name and team_name.lower() not in owner_name.lower():
                    continue
                
                taxi = roster.get("taxi") or []
                if not taxi:
                    continue
                
                found_any = True
                
                # Format taxi players
                taxi_players = []
                for pid in taxi:
                    p = players.get(pid, {})
                    name = p.get("full_name", pid)
                    pos = p.get("position", "?")
                    team = p.get("team", "FA")
                    taxi_players.append(f"• {name} ({pos} - {team})")
                
                embed.add_field(
                    name=owner_name,
                    value="\n".join(taxi_players) if taxi_players else "Empty",
                    inline=False,
                )
            
            if not found_any:
                await interaction.followup.send("No taxi squad players found.")
                return
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Taxi squad command failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error fetching taxi squads: {e}")


async def setup(bot: "DynastyBot") -> None:
    """Load the Taxi Raiding cog."""
    await bot.add_cog(TaxiRaiding(bot))
