"""
Scheduled alerts for inactive or injured starters.

Checks lineup starters every 4 hours and alerts if any player
is inactive or out due to injury.
"""

import logging
from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import ALERT_CHANNEL_ID, SLEEPER_LEAGUE_ID
from utils.data_manager import load_user_map

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.alerts")


class Alerts(commands.Cog):
    """Scheduled tasks for lineup alerts."""

    def __init__(self, bot: "DynastyBot") -> None:
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID
        self.sent_alerts: Set[Tuple[int, str, str]] = set()  # (week, player_id, roster_id)
        self.current_week: int = 0

    def cog_load(self) -> None:
        """Start the alert task when the cog is loaded."""
        self.check_starters.start()

    def cog_unload(self) -> None:
        """Stop the alert task when the cog is unloaded."""
        self.check_starters.cancel()

    @tasks.loop(hours=4)
    async def check_starters(self) -> None:
        """Runs every 4 hours and sends alerts to the configured channel."""
        try:
            await self._check_starters()
        except Exception as e:
            # Log error but don't crash the task
            logger.error(f"Error in check_starters: {e}", exc_info=True)

    async def _check_starters(self) -> Optional[int]:
        """
        Check all starters for inactive or injured players.

        Returns:
            The number of alert messages sent, or None if the check was
            skipped entirely (e.g. NFL is in the offseason).
        """
        # Get current NFL state
        nfl_state = await self.bot.sleeper.get_nfl_state()
        week = nfl_state.get("week")
        season_type = nfl_state.get("season_type")

        # Only check during regular season or playoffs
        if season_type not in ("regular", "post"):
            logger.info(f"Skipping starter check - season_type is '{season_type}'")
            return None

        # Ensure week is an integer
        if week is None:
            return None
        try:
            week = int(week)
        except (ValueError, TypeError):
            return None

        # Clear alerts if week changed
        if week != self.current_week:
            self.sent_alerts.clear()
            self.current_week = week

        # Get all players (cached)
        all_players = await self.bot.sleeper.get_all_players()

        # Get matchups for current week
        matchups = await self.bot.sleeper.get_matchups(self.league_id, week)

        # Get all rosters to map roster_id to team name
        rosters = await self.bot.sleeper.get_rosters(self.league_id)
        roster_map: Dict[str, Dict[str, any]] = {str(r["roster_id"]): r for r in rosters}

        # Get user mappings
        user_map = load_user_map()

        # Check each matchup
        alerts_to_send: List[str] = []
        for matchup in matchups:
            roster_id = str(matchup.get("roster_id", ""))
            starters = matchup.get("starters", [])

            # Get team name from roster
            roster_info = roster_map.get(roster_id, {})
            team_name = roster_info.get("metadata", {}).get("team_name", f"Team {roster_id}")

            # Check each starter
            for player_id in starters:
                if not player_id or player_id == "0":
                    continue

                # Check if we've already sent this alert
                alert_key = (week, player_id, roster_id)
                if alert_key in self.sent_alerts:
                    continue

                # Get player info
                player = all_players.get(player_id)
                if not player:
                    continue

                status = (player.get("status") or "").lower()
                injury_status = (player.get("injury_status") or "").lower()

                # Check if player is inactive or out
                if status == "inactive" or injury_status == "out":
                    # Find Discord user if mapped
                    discord_user_id = None
                    for disc_id, sleeper_roster_id in user_map.items():
                        if sleeper_roster_id == roster_id:
                            discord_user_id = disc_id
                            break

                    # Build alert message
                    player_name = player.get("full_name", "Unknown Player")
                    position = player.get("position", "N/A")

                    if discord_user_id:
                        mention = f"<@{discord_user_id}>"
                        alert_msg = (
                            f"⚠️ {mention} - **{player_name}** ({position}) "
                            f"on {team_name} is "
                            f"{'INACTIVE' if status == 'inactive' else 'OUT'} "
                            f"this week!"
                        )
                    else:
                        alert_msg = (
                            f"⚠️ **{player_name}** ({position}) "
                            f"on {team_name} is "
                            f"{'INACTIVE' if status == 'inactive' else 'OUT'} "
                            f"this week!"
                        )

                    alerts_to_send.append(alert_msg)
                    self.sent_alerts.add(alert_key)

        # Send all alerts
        if not ALERT_CHANNEL_ID:
            logger.warning("ALERT_CHANNEL_ID not configured, skipping alerts")
            return 0

        channel = self.bot.get_channel(ALERT_CHANNEL_ID)
        if not channel:
            logger.error(f"Could not find alert channel {ALERT_CHANNEL_ID}")
            return 0

        for alert in alerts_to_send:
            await channel.send(alert)

        return len(alerts_to_send)

    @check_starters.before_loop
    async def before_check_starters(self) -> None:
        """Wait until the bot is ready before starting the task."""
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="checkalerts",
        description="[Admin] Manually check for inactive/injured starters right now",
    )
    @app_commands.default_permissions(administrator=True)
    async def check_alerts_now(self, interaction: discord.Interaction) -> None:
        """Admin command to force a lineup alert check immediately."""
        await interaction.response.defer(ephemeral=True)

        try:
            sent = await self._check_starters()
        except Exception as e:
            logger.error(f"Manual alert check failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error checking starters: {e}", ephemeral=True)
            return

        if sent is None:
            await interaction.followup.send(
                "ℹ️ Skipped — NFL is not in the regular season or playoffs right now.",
                ephemeral=True,
            )
        elif sent == 0:
            await interaction.followup.send(
                "✅ Checked all starters. No inactive/out players found (or alert channel isn't reachable).",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ Checked all starters. Sent {sent} alert(s).", ephemeral=True
            )


async def setup(bot: "DynastyBot") -> None:
    """Load the Alerts cog."""
    await bot.add_cog(Alerts(bot))
