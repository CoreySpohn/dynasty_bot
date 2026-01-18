"""Ice Chug Monitor Cog for Dynasty Bot.

Enforces the league rule: "If you start an inactive player while having
an active replacement on your bench, you must chug an Ice."

Runs a background task every 15 minutes to detect violations.
"""

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from config import ALERT_CHANNEL_ID, SLEEPER_LEAGUE_ID

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.monitor")

# Player statuses considered "inactive" for ice chug purposes
INACTIVE_STATUSES = {"Out", "Inactive", "IR", "Doubtful", "Sus"}

# Positions that can substitute for each other
# Maps position -> list of positions that can replace it
POSITION_EQUIVALENTS: dict[str, list[str]] = {
    "QB": ["QB"],
    "RB": ["RB"],
    "WR": ["WR"],
    "TE": ["TE"],
    "K": ["K"],
    "DEF": ["DEF"],
    # Flex positions can be filled by multiple positions
    "FLEX": ["RB", "WR", "TE"],
    "SUPER_FLEX": ["QB", "RB", "WR", "TE"],
    "REC_FLEX": ["WR", "TE"],
    "WRRB_FLEX": ["WR", "RB"],
}


class IceChugMonitor(commands.Cog):
    """Monitors lineups for inactive starters with active bench replacements.
    
    When a violation is detected, sends an alert to the configured channel
    and tracks the alert to prevent duplicate notifications.
    """
    
    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID
        
        # Track alerts already sent this week: set of (week, roster_id, player_id)
        self._alerts_sent: set[tuple[int, int, str]] = set()
        self._current_week: int | None = None
        
        # User mapping cache: roster_id -> discord user mention or display name
        self._user_map: dict[int, str] = {}
    
    async def cog_load(self) -> None:
        """Called when the cog is loaded. Start the background task."""
        logger.info("Ice Chug Monitor cog loaded")
        self.monitor_lineups.start()
    
    async def cog_unload(self) -> None:
        """Called when the cog is unloaded. Stop the background task."""
        self.monitor_lineups.cancel()
        logger.info("Ice Chug Monitor cog unloaded")
    
    @tasks.loop(minutes=15)
    async def monitor_lineups(self) -> None:
        """Background task that checks for ice chug violations every 15 minutes."""
        try:
            await self._check_for_violations()
        except Exception as e:
            logger.error(f"Error in ice chug monitor: {e}", exc_info=True)
    
    @monitor_lineups.before_loop
    async def before_monitor(self) -> None:
        """Wait for the bot to be ready before starting the monitor."""
        await self.bot.wait_until_ready()
        logger.info("Ice Chug Monitor starting background checks")
    
    async def _check_for_violations(self) -> None:
        """Main logic to detect inactive starters with active replacements."""
        # Get current league info for the week
        league = await self.bot.sleeper.get_league(self.league_id)
        current_week = league.get("settings", {}).get("leg", 1)
        
        # Reset alerts if week changed
        if self._current_week != current_week:
            logger.info(f"Week changed to {current_week}, clearing alert cache")
            self._alerts_sent.clear()
            self._current_week = current_week
        
        # Fetch all required data
        rosters = await self.bot.sleeper.get_rosters(self.league_id)
        matchups = await self.bot.sleeper.get_matchups(self.league_id, current_week)
        players = await self.bot.sleeper.get_all_players()
        users = await self.bot.sleeper.get_users(self.league_id)
        
        # Build user lookup: owner_id -> display_name
        user_lookup = {u["user_id"]: u.get("display_name", "Unknown") for u in users}
        
        # Build roster lookup: roster_id -> roster data
        roster_lookup = {r["roster_id"]: r for r in rosters}
        
        # Get roster positions from league settings
        roster_positions = league.get("roster_positions", [])
        starter_count = sum(1 for p in roster_positions if p not in ("BN", "IR"))
        
        violations = []
        
        for matchup in matchups:
            roster_id = matchup.get("roster_id")
            starters = matchup.get("starters", [])
            
            if not roster_id or not starters:
                continue
            
            roster = roster_lookup.get(roster_id, {})
            owner_id = roster.get("owner_id")
            owner_name = user_lookup.get(owner_id, f"Team {roster_id}")
            
            # Get bench players (all players minus starters, taxi, and IR)
            all_player_ids = set(roster.get("players", []))
            taxi_ids = set(roster.get("taxi", []) or [])
            reserve_ids = set(roster.get("reserve", []) or [])
            starter_ids = set(starters)
            bench_ids = all_player_ids - starter_ids - taxi_ids - reserve_ids
            
            # Check each starter
            for idx, starter_id in enumerate(starters):
                if not starter_id or starter_id == "0":
                    continue
                
                # Skip if already alerted
                alert_key = (current_week, roster_id, starter_id)
                if alert_key in self._alerts_sent:
                    continue
                
                starter_data = players.get(starter_id, {})
                starter_status = starter_data.get("injury_status") or "Active"
                starter_name = self._get_player_name(starter_data)
                starter_position = starter_data.get("position", "")
                
                # Check if starter is inactive
                if starter_status not in INACTIVE_STATUSES:
                    continue
                
                # Determine which roster slot this is
                if idx < len(roster_positions):
                    slot_position = roster_positions[idx]
                else:
                    slot_position = starter_position
                
                # Find eligible positions for this slot
                eligible_positions = POSITION_EQUIVALENTS.get(
                    slot_position, [slot_position]
                )
                
                # Check bench for active replacement
                for bench_id in bench_ids:
                    bench_data = players.get(bench_id, {})
                    bench_status = bench_data.get("injury_status") or "Active"
                    bench_position = bench_data.get("position", "")
                    bench_name = self._get_player_name(bench_data)
                    
                    # Check if bench player is active and position-eligible
                    if (
                        bench_status not in INACTIVE_STATUSES
                        and bench_position in eligible_positions
                    ):
                        violations.append({
                            "roster_id": roster_id,
                            "owner_name": owner_name,
                            "starter_id": starter_id,
                            "starter_name": starter_name,
                            "starter_status": starter_status,
                            "bench_name": bench_name,
                            "bench_position": bench_position,
                        })
                        # Mark as alerted
                        self._alerts_sent.add(alert_key)
                        # Only need one replacement to trigger
                        break
        
        # Send alerts for violations
        if violations:
            await self._send_alerts(violations)
    
    def _get_player_name(self, player_data: dict) -> str:
        """Get formatted player name from player data."""
        first = player_data.get("first_name", "")
        last = player_data.get("last_name", "")
        if first and last:
            return f"{first} {last}"
        return player_data.get("full_name", "Unknown Player")
    
    async def _send_alerts(self, violations: list[dict]) -> None:
        """Send ice chug alert messages to the configured channel."""
        if not ALERT_CHANNEL_ID:
            logger.warning("ALERT_CHANNEL_ID not configured, skipping alerts")
            for v in violations:
                logger.info(
                    f"ICE CHUG: {v['owner_name']} - {v['starter_name']} "
                    f"({v['starter_status']}) with {v['bench_name']} available"
                )
            return
        
        channel = self.bot.get_channel(ALERT_CHANNEL_ID)
        if not channel:
            logger.error(f"Could not find alert channel {ALERT_CHANNEL_ID}")
            return
        
        for v in violations:
            message = (
                f"🍺 **ICE CHUG ALERT** {v['owner_name']} is starting "
                f"**{v['starter_name']}** (Status: {v['starter_status']}) "
                f"with **{v['bench_name']}** ({v['bench_position']}) available!"
            )
            
            try:
                await channel.send(message)
                logger.info(f"Sent ice chug alert: {v['owner_name']} - {v['starter_name']}")
            except discord.HTTPException as e:
                logger.error(f"Failed to send alert: {e}")
    
    @commands.hybrid_command(name="checklineups", description="Manually check for ice chug violations")
    @commands.has_permissions(manage_guild=True)
    async def check_lineups(self, ctx: commands.Context) -> None:
        """Manually trigger a lineup check for ice chug violations."""
        await ctx.defer()
        
        try:
            await self._check_for_violations()
            await ctx.send("✅ Lineup check complete! Any violations have been posted.")
        except Exception as e:
            logger.error(f"Manual lineup check failed: {e}", exc_info=True)
            await ctx.send(f"❌ Error checking lineups: {e}")
    
    @commands.hybrid_command(name="clearalerts", description="Clear ice chug alert cache for this week")
    @commands.has_permissions(manage_guild=True)
    async def clear_alerts(self, ctx: commands.Context) -> None:
        """Clear the ice chug alert cache to allow re-checking."""
        count = len(self._alerts_sent)
        self._alerts_sent.clear()
        await ctx.send(f"🧹 Cleared {count} cached alerts. Next check will re-scan all lineups.")


async def setup(bot: "DynastyBot") -> None:
    """Load the Ice Chug Monitor cog."""
    await bot.add_cog(IceChugMonitor(bot))
