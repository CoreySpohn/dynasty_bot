"""Trade Poll Cog for Dynasty Bot.

Automatically monitors for new trades in the league and posts
"Who won the trade?" polls to a designated channel.

Polls last for 48 hours and use Discord's reaction-based voting.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import SLEEPER_LEAGUE_ID

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.trades")

# Poll duration
POLL_DURATION_HOURS = 48

# Reaction emojis for voting
REACTIONS = {
    "team1": "1️⃣",
    "team2": "2️⃣",
    "fair": "🤝",
}


class TradePoll(commands.Cog):
    """Monitors trades and posts automatic polls."""
    
    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID
        
        # Channel for trade polls (uses same as rumors if not set)
        self.trades_channel_id = int(os.getenv("TRADES_CHANNEL_ID", 0)) or \
                                  int(os.getenv("RUMORS_CHANNEL_ID", 0))
        
        # Track processed trades to avoid duplicates
        self._processed_trades: set[str] = set()
        
        # Cache for user/roster lookups
        self._roster_to_team: Optional[dict[int, str]] = None
        self._players: Optional[dict] = None
        
        # Start monitoring task
        self.check_trades_task.start()
    
    async def cog_load(self) -> None:
        """Called when the cog is loaded."""
        logger.info("Trade Poll cog loaded")
        if not self.trades_channel_id:
            logger.warning("TRADES_CHANNEL_ID not configured")
        # Note: Database loading happens in before_check_trades when bot is ready
    
    async def cog_unload(self) -> None:
        """Called when the cog is unloaded."""
        self.check_trades_task.cancel()
        logger.info("Trade Poll cog unloaded")
    
    async def _load_processed_trades(self) -> None:
        """Load processed trade IDs from database."""
        if not hasattr(self.bot, 'db') or self.bot.db is None:
            logger.warning("Database not available, skipping trade history load")
            return
            
        try:
            async with self.bot.db.connection() as conn:
                # Check if table exists, create if not
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS processed_trades (
                        transaction_id TEXT PRIMARY KEY,
                        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                cursor = await conn.execute(
                    "SELECT transaction_id FROM processed_trades"
                )
                rows = await cursor.fetchall()
                self._processed_trades = {row[0] for row in rows}
                logger.info(f"Loaded {len(self._processed_trades)} processed trades")
        except Exception as e:
            logger.error(f"Failed to load processed trades: {e}")
    
    async def _mark_trade_processed(self, transaction_id: str) -> None:
        """Mark a trade as processed in the database."""
        self._processed_trades.add(transaction_id)
        
        if not hasattr(self.bot, 'db') or self.bot.db is None:
            return
            
        try:
            async with self.bot.db.connection() as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO processed_trades (transaction_id) VALUES (?)",
                    (transaction_id,)
                )
                await conn.commit()
        except Exception as e:
            logger.error(f"Failed to mark trade processed: {e}")
    
    async def _get_roster_lookup(self) -> dict[int, str]:
        """Get roster_id to team name mapping."""
        if self._roster_to_team:
            return self._roster_to_team
        
        try:
            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users = await self.bot.sleeper.get_users(self.league_id)
            
            user_lookup = {}
            for user in users:
                user_id = user.get("user_id")
                team_name = user.get("metadata", {}).get("team_name") or user.get("display_name")
                user_lookup[user_id] = team_name
            
            self._roster_to_team = {}
            for roster in rosters:
                roster_id = roster.get("roster_id")
                owner_id = roster.get("owner_id", "")
                self._roster_to_team[roster_id] = user_lookup.get(owner_id, f"Team {roster_id}")
            
            return self._roster_to_team
        except Exception as e:
            logger.error(f"Failed to get roster lookup: {e}")
            return {}
    
    async def _get_players(self) -> dict:
        """Get player data."""
        if self._players:
            return self._players
        
        try:
            self._players = await self.bot.sleeper.get_all_players()
            return self._players
        except Exception as e:
            logger.error(f"Failed to get players: {e}")
            return {}
    
    def _format_player(self, player_id: str, players: dict) -> str:
        """Format a player name with position."""
        player = players.get(player_id, {})
        name = player.get("full_name") or player.get("first_name", "") + " " + player.get("last_name", "")
        pos = player.get("position", "")
        team = player.get("team", "")
        
        if name.strip():
            return f"{name} ({pos}, {team})" if team else f"{name} ({pos})"
        return f"Player {player_id}"
    
    def _format_pick(self, pick: dict) -> str:
        """Format a draft pick description."""
        season = pick.get("season", "")
        round_num = pick.get("round", "")
        
        # Format round with ordinal
        ordinals = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}
        round_str = ordinals.get(round_num, f"{round_num}th")
        
        return f"{season} {round_str} Round Pick"
    
    async def _post_trade_poll(
        self,
        team1_name: str,
        team1_receives: list[str],
        team2_name: str,
        team2_receives: list[str],
    ) -> bool:
        """Post a trade poll to the channel.
        
        Args:
            team1_name: Name of the first team.
            team1_receives: List of formatted items team 1 receives.
            team2_name: Name of the second team.
            team2_receives: List of formatted items team 2 receives.
            
        Returns:
            True if posted successfully.
        """
        if not self.trades_channel_id:
            logger.warning("Cannot post trade poll - channel not configured")
            return False
        
        channel = self.bot.get_channel(self.trades_channel_id)
        if not channel:
            logger.error(f"Channel {self.trades_channel_id} not found")
            return False
        
        # Build embed
        embed = discord.Embed(
            title="🔄 TRADE ALERT!",
            description="A trade has been completed! Who won?",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )
        
        # Team 1 receives
        team1_items = "\n".join(f"• {item}" for item in team1_receives) or "• Nothing"
        embed.add_field(
            name=f"{REACTIONS['team1']} {team1_name} receives:",
            value=team1_items,
            inline=True,
        )
        
        # Team 2 receives  
        team2_items = "\n".join(f"• {item}" for item in team2_receives) or "• Nothing"
        embed.add_field(
            name=f"{REACTIONS['team2']} {team2_name} receives:",
            value=team2_items,
            inline=True,
        )
        
        # Voting instructions
        embed.add_field(
            name="Vote!",
            value=(
                f"{REACTIONS['team1']} = {team1_name} won\n"
                f"{REACTIONS['team2']} = {team2_name} won\n"
                f"{REACTIONS['fair']} = Fair trade"
            ),
            inline=False,
        )
        
        embed.set_footer(text=f"Poll closes in {POLL_DURATION_HOURS} hours")
        
        try:
            message = await channel.send(embed=embed)
            
            # Add voting reactions
            for emoji in REACTIONS.values():
                await message.add_reaction(emoji)
            
            logger.info(f"Posted trade poll: {team1_name} vs {team2_name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to post trade poll: {e}")
            return False
    
    @tasks.loop(minutes=5)
    async def check_trades_task(self) -> None:
        """Check for new trades every 5 minutes."""
        await self.bot.wait_until_ready()
        
        try:
            # Get recent transactions
            transactions = await self.bot.sleeper.get_transactions(
                self.league_id,
                week=1,  # Week doesn't matter for trades, they return all
            )
            
            # Filter to trades only
            trades = [t for t in transactions if t.get("type") == "trade"]
            
            for trade in trades:
                transaction_id = trade.get("transaction_id")
                
                # Skip if already processed
                if transaction_id in self._processed_trades:
                    continue
                
                # Skip if trade is too old (more than 7 days)
                created_at = trade.get("created", 0)
                trade_time = datetime.fromtimestamp(created_at / 1000)
                if datetime.now() - trade_time > timedelta(days=7):
                    await self._mark_trade_processed(transaction_id)
                    continue
                
                # Process the trade
                await self._process_trade(trade)
                await self._mark_trade_processed(transaction_id)
                
                # Small delay between posts
                await asyncio.sleep(2)
                
        except Exception as e:
            logger.error(f"Error checking trades: {e}")
    
    async def _process_trade(self, trade: dict) -> None:
        """Process a trade and post a poll."""
        roster_lookup = await self._get_roster_lookup()
        players = await self._get_players()
        
        # Get trade details
        roster_ids = trade.get("roster_ids", [])
        adds = trade.get("adds", {}) or {}
        draft_picks = trade.get("draft_picks", []) or []
        
        if len(roster_ids) < 2:
            logger.warning(f"Trade has fewer than 2 rosters: {trade}")
            return
        
        # Organize by team
        team1_id = roster_ids[0]
        team2_id = roster_ids[1]
        
        team1_name = roster_lookup.get(team1_id, f"Team {team1_id}")
        team2_name = roster_lookup.get(team2_id, f"Team {team2_id}")
        
        # Players received
        team1_receives = []
        team2_receives = []
        
        for player_id, roster_id in adds.items():
            player_str = self._format_player(player_id, players)
            if roster_id == team1_id:
                team1_receives.append(player_str)
            elif roster_id == team2_id:
                team2_receives.append(player_str)
        
        # Draft picks
        for pick in draft_picks:
            pick_str = self._format_pick(pick)
            owner_id = pick.get("owner_id")
            
            if owner_id == team1_id:
                team1_receives.append(pick_str)
            elif owner_id == team2_id:
                team2_receives.append(pick_str)
        
        # Post the poll
        await self._post_trade_poll(
            team1_name=team1_name,
            team1_receives=team1_receives,
            team2_name=team2_name,
            team2_receives=team2_receives,
        )
    
    @check_trades_task.before_loop
    async def before_check_trades(self) -> None:
        """Wait for bot to be ready before checking trades."""
        await self.bot.wait_until_ready()
        # Load processed trades from database
        await self._load_processed_trades()
        # Initial delay
        await asyncio.sleep(30)
    
    @app_commands.command(
        name="checktrades",
        description="[Admin] Force check for new trades now"
    )
    @app_commands.default_permissions(administrator=True)
    async def force_check_trades(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Admin command to force trade check."""
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Get recent transactions
            transactions = await self.bot.sleeper.get_transactions(
                self.league_id,
                week=1,
            )
            
            trades = [t for t in transactions if t.get("type") == "trade"]
            new_trades = [t for t in trades if t.get("transaction_id") not in self._processed_trades]
            
            if not new_trades:
                await interaction.followup.send(
                    f"No new trades found. ({len(trades)} total trades, {len(self._processed_trades)} already processed)",
                    ephemeral=True,
                )
                return
            
            for trade in new_trades[:3]:  # Limit to 3 at a time
                await self._process_trade(trade)
                await self._mark_trade_processed(trade.get("transaction_id"))
                await asyncio.sleep(2)
            
            await interaction.followup.send(
                f"✅ Posted {min(len(new_trades), 3)} trade poll(s)!",
                ephemeral=True,
            )
            
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: "DynastyBot") -> None:
    """Load the Trade Poll cog."""
    await bot.add_cog(TradePoll(bot))
