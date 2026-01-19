"""Kohl's Cash Betting System for Dynasty Bot.

Allows owners to bet their leftover FAAB on NFL playoff games
and spend winnings in a store for Discord perks.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import discord
import yaml
from discord import app_commands
from discord.ext import commands, tasks

from database import db

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.kohls")

# Config path
STATE_PATH = Path(__file__).parent.parent / "config" / "league_state.yaml"

# Store items configuration
STORE_ITEMS = {
    "color": {
        "name": "Custom Discord Color",
        "cost": 25,
        "description": "Change your role color for 1 week",
        "emoji": "🎨",
    },
    "nickname": {
        "name": "Custom Nickname",
        "cost": 25,
        "description": "Bot sets your nickname for 1 week",
        "emoji": "📛",
    },
    "response": {
        "name": "Add Random Response",
        "cost": 50,
        "description": "Add a phrase to the bot's auto-replies",
        "emoji": "🎲",
    },
    "punishment": {
        "name": "Targeted Response (5 pack)",
        "cost": 75,
        "description": "1/10 chance bot replies to target with YOUR message (5 uses)",
        "emoji": "💀",
    },
    "punishment_extra": {
        "name": "Extra Targeted Response",
        "cost": 5,
        "description": "Additional activation for existing punishment",
        "emoji": "➕",
    },
}


class KohlsCash(commands.Cog):
    """Kohl's Cash betting and store system."""
    
    kohls = app_commands.Group(name="kohls", description="Kohl's Cash betting and store")
    
    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.config = self._load_config()
        self.channel_id = self.config.get("kohls", {}).get("channel_id")
        logger.info("Kohl's Cash cog loaded")
    
    def _load_config(self) -> dict:
        """Load league state config."""
        try:
            with open(STATE_PATH) as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            return {}
    
    async def _get_balance(self, owner_id: str) -> int:
        """Get a user's current Kohl's Cash balance from transaction ledger."""
        # First check transaction ledger
        async with db.connection.execute(
            "SELECT SUM(amount) FROM kohls_transactions WHERE owner_id = ?",
            (owner_id,)
        ) as cursor:
            row = await cursor.fetchone()
            tx_balance = row[0] if row and row[0] else 0
        
        # Also check legacy balance table (for migration)
        async with db.connection.execute(
            "SELECT balance FROM kohls_balances WHERE owner_id = ?",
            (owner_id,)
        ) as cursor:
            row = await cursor.fetchone()
            legacy_balance = row[0] if row else 0
        
        # If we have legacy balance but no transactions, migrate it
        if legacy_balance > 0 and tx_balance == 0:
            await self._record_transaction(
                owner_id, legacy_balance, "load", 
                description="Migrated from legacy balance"
            )
            return legacy_balance
        
        return tx_balance
    
    async def _record_transaction(
        self, 
        owner_id: str, 
        amount: int, 
        tx_type: str,
        reference_id: str = None,
        description: str = None
    ) -> int:
        """Record a transaction and return new balance.
        
        tx_type: 'load', 'bet', 'win', 'loss', 'refund', 'purchase', 'prop_bet', 'prop_win'
        """
        await db.connection.execute("""
            INSERT INTO kohls_transactions (owner_id, amount, tx_type, reference_id, description)
            VALUES (?, ?, ?, ?, ?)
        """, (owner_id, amount, tx_type, reference_id, description))
        await db.connection.commit()
        
        return await self._get_balance(owner_id)
    
    async def _update_balance(self, owner_id: str, amount: int, season: int) -> int:
        """Update a user's balance via transaction. Returns new balance."""
        tx_type = "adjustment" if amount >= 0 else "deduction"
        return await self._record_transaction(owner_id, amount, tx_type)
    
    @kohls.command(name="balance")
    async def balance(self, interaction: discord.Interaction):
        """Check your Kohl's Cash balance."""
        owner_id = str(interaction.user.id)
        balance = await self._get_balance(owner_id)
        
        embed = discord.Embed(
            title="💰 Kohl's Cash Balance",
            description=f"**{balance:,}** KC",
            color=discord.Color.green()
        )
        embed.set_footer(text="Use /kohls store to spend your cash!")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @kohls.command(name="store")
    async def store(self, interaction: discord.Interaction):
        """View available store items."""
        embed = discord.Embed(
            title="🏪 Kohl's Store",
            description="Spend your Kohl's Cash on perks!",
            color=discord.Color.purple()
        )
        
        for item_id, item in STORE_ITEMS.items():
            embed.add_field(
                name=f"{item['emoji']} {item['name']} — {item['cost']} KC",
                value=f"{item['description']}\n`/kohls buy {item_id}`",
                inline=False
            )
        
        # Show user's balance
        balance = await self._get_balance(str(interaction.user.id))
        embed.set_footer(text=f"Your balance: {balance:,} KC")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @kohls.command(name="buy")
    @app_commands.describe(
        item="The store item to purchase",
        target="Target user (for punishments)",
        text="Custom text (for responses/punishments)"
    )
    @app_commands.choices(item=[
        app_commands.Choice(name="🎨 Custom Discord Color (25 KC)", value="color"),
        app_commands.Choice(name="📛 Custom Nickname (25 KC)", value="nickname"),
        app_commands.Choice(name="🎲 Add Random Response (50 KC)", value="response"),
        app_commands.Choice(name="💀 Targeted Response 5-pack (75 KC)", value="punishment"),
        app_commands.Choice(name="➕ Extra Targeted Response (+5 KC)", value="punishment_extra"),
    ])
    async def buy(
        self,
        interaction: discord.Interaction,
        item: str,
        target: Optional[discord.Member] = None,
        text: Optional[str] = None
    ):
        """Purchase a store item."""
        if item not in STORE_ITEMS:
            await interaction.response.send_message("❌ Invalid item.", ephemeral=True)
            return
        
        store_item = STORE_ITEMS[item]
        cost = store_item["cost"]
        owner_id = str(interaction.user.id)
        
        # Check balance
        balance = await self._get_balance(owner_id)
        if balance < cost:
            await interaction.response.send_message(
                f"❌ Not enough Kohl's Cash! You have **{balance:,}** KC, need **{cost}** KC.",
                ephemeral=True
            )
            return
        
        # Validate item-specific requirements
        if item in ("punishment", "punishment_extra") and not target:
            await interaction.response.send_message(
                "❌ You must specify a target for punishments!",
                ephemeral=True
            )
            return
        
        if item in ("punishment", "response") and not text:
            await interaction.response.send_message(
                "❌ You must provide custom text for this item!",
                ephemeral=True
            )
            return
        
        # Get season
        season = self.config.get("season", {}).get("year", datetime.now().year)
        
        # Deduct cost
        await self._update_balance(owner_id, -cost, season)
        
        # Process purchase based on item type
        if item == "punishment":
            # Add targeted response
            await db.connection.execute("""
                INSERT INTO kohls_targeted_responses 
                (target_discord_id, response_text, chance, remaining_activations, buyer_id)
                VALUES (?, ?, 10, 5, ?)
            """, (str(target.id), text, owner_id))
            await db.connection.commit()
            
            message = (
                f"✅ **Secret purchase complete!**\n"
                f"**Target:** {target.display_name} (they won't know!)\n"
                f"**Response:** {text}\n"
                f"**Activations:** 5 remaining\n"
                f"**Cost:** {cost} KC"
            )
        
        elif item == "punishment_extra":
            # Add more activations to existing punishment
            async with db.connection.execute("""
                SELECT id, remaining_activations FROM kohls_targeted_responses
                WHERE buyer_id = ? AND target_discord_id = ?
                ORDER BY created_at DESC LIMIT 1
            """, (owner_id, str(target.id))) as cursor:
                row = await cursor.fetchone()
            
            if not row:
                # Refund
                await self._update_balance(owner_id, cost, season)
                await interaction.response.send_message(
                    "❌ You don't have an existing punishment for this target!",
                    ephemeral=True
                )
                return
            
            await db.connection.execute("""
                UPDATE kohls_targeted_responses
                SET remaining_activations = remaining_activations + 1
                WHERE id = ?
            """, (row[0],))
            await db.connection.commit()
            
            message = (
                f"✅ **Added 1 activation!**\n"
                f"**Target:** {target.display_name}\n"
                f"**New total:** {row[1] + 1} remaining"
            )
        
        elif item == "response":
            # Add to random responses
            responses_cog = self.bot.get_cog("RandomResponses")
            if responses_cog:
                responses_cog.responses.append({
                    "text": text,
                    "chance": 1000,
                    "added_by": interaction.user.display_name
                })
                responses_cog._save_responses(responses_cog.responses)
                responses_cog.responses = responses_cog._load_responses()
            
            message = (
                f"✅ **Random response added!**\n"
                f"**Text:** {text}\n"
                f"**Chance:** 1/1000"
            )
        
        elif item == "color":
            # Record purchase - needs manual application
            await db.connection.execute("""
                INSERT INTO kohls_purchases (owner_id, item_type, custom_text, cost)
                VALUES (?, 'color', ?, ?)
            """, (owner_id, text, cost))
            await db.connection.commit()
            
            message = (
                f"✅ **Custom color purchased!**\n"
                f"Use `/kohls setcolor #HEXCODE` to apply your color."
            )
        
        elif item == "nickname":
            await db.connection.execute("""
                INSERT INTO kohls_purchases (owner_id, item_type, custom_text, cost)
                VALUES (?, 'nickname', ?, ?)
            """, (owner_id, text, cost))
            await db.connection.commit()
            
            if text:
                try:
                    await interaction.user.edit(nick=text)
                    message = f"✅ **Nickname set to:** {text}"
                except discord.Forbidden:
                    message = f"✅ **Purchase recorded!** Bot lacks permission to change your nickname."
            else:
                message = "✅ **Nickname purchase recorded!** Use `/kohls setnick` to apply."
        
        else:
            message = f"✅ **Purchased {store_item['name']}!**"
        
        await interaction.response.send_message(message, ephemeral=True)
        logger.info(f"Kohl's purchase: {interaction.user} bought {item} for {cost} KC")
    
    @kohls.command(name="leaderboard")
    async def leaderboard(self, interaction: discord.Interaction):
        """View the Kohl's Cash leaderboard."""
        async with db.connection.execute("""
            SELECT owner_id, balance FROM kohls_balances
            ORDER BY balance DESC
            LIMIT 12
        """) as cursor:
            rows = await cursor.fetchall()
        
        if not rows:
            await interaction.response.send_message(
                "No balances recorded yet!",
                ephemeral=True
            )
            return
        
        # Get member registry for real names
        from lib.members import get_member_registry
        registry = get_member_registry()
        
        embed = discord.Embed(
            title="💰 Kohl's Cash Leaderboard",
            color=discord.Color.gold()
        )
        
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        
        for idx, (owner_id, balance) in enumerate(rows):
            # Try to get real name from member registry
            member = registry.find_by_discord_id(owner_id)
            if member:
                name = member.name
            else:
                # Fallback to Discord username
                try:
                    user = await self.bot.fetch_user(int(owner_id))
                    name = user.display_name
                except:
                    name = f"Unknown ({owner_id[:8]}...)"
            
            prefix = medals[idx] if idx < 3 else f"`{idx + 1}.`"
            lines.append(f"{prefix} **{name}** — {balance:,} KC")
        
        embed.description = "\n".join(lines)
        
        await interaction.response.send_message(embed=embed)
    
    @kohls.command(name="mybets")
    async def mybets(self, interaction: discord.Interaction):
        """View your pending bets."""
        owner_id = str(interaction.user.id)
        
        async with db.connection.execute("""
            SELECT b.pick, b.amount, b.result, g.home_team, g.away_team, g.spread
            FROM kohls_bets b
            JOIN kohls_games g ON b.game_id = g.game_id
            WHERE b.owner_id = ?
            ORDER BY g.kickoff DESC
        """, (owner_id,)) as cursor:
            rows = await cursor.fetchall()
        
        if not rows:
            await interaction.response.send_message(
                "You haven't placed any bets yet!",
                ephemeral=True
            )
            return
        
        embed = discord.Embed(
            title="🎰 Your Bets",
            color=discord.Color.blue()
        )
        
        for pick, amount, result, home, away, spread in rows:
            spread_str = f"+{spread}" if spread > 0 else str(spread)
            status = {"pending": "⏳", "won": "✅", "lost": "❌", "push": "↩️"}.get(result, "?")
            
            embed.add_field(
                name=f"{status} {away} @ {home} ({spread_str})",
                value=f"**Pick:** {pick} • **Wager:** {amount} KC",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    async def team_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for team selection in bet command."""
        choices = []
        
        # Check if we're in a game thread
        if isinstance(interaction.channel, discord.Thread):
            async with db.connection.execute("""
                SELECT home_team, away_team, spread
                FROM kohls_games
                WHERE thread_id = ? AND status = 'open'
            """, (interaction.channel.id,)) as cursor:
                game = await cursor.fetchone()
            
            if game:
                home, away, spread = game
                spread_str = f"+{spread}" if spread > 0 else str(spread)
                away_spread = f"+{-spread}" if -spread > 0 else str(-spread)
                
                choices = [
                    app_commands.Choice(name=f"{home} ({spread_str})", value=home),
                    app_commands.Choice(name=f"{away} ({away_spread})", value=away),
                ]
        else:
            # Show all open games
            async with db.connection.execute("""
                SELECT home_team, away_team, spread
                FROM kohls_games
                WHERE status = 'open'
                ORDER BY kickoff
                LIMIT 10
            """) as cursor:
                games = await cursor.fetchall()
            
            for home, away, spread in games:
                spread_str = f"+{spread}" if spread > 0 else str(spread)
                away_spread = f"+{-spread}" if -spread > 0 else str(-spread)
                
                if current.lower() in home.lower():
                    choices.append(app_commands.Choice(name=f"{home} ({spread_str})", value=home))
                if current.lower() in away.lower():
                    choices.append(app_commands.Choice(name=f"{away} ({away_spread})", value=away))
                
                if not current:
                    choices.append(app_commands.Choice(name=f"{home} ({spread_str})", value=home))
                    choices.append(app_commands.Choice(name=f"{away} ({away_spread})", value=away))
        
        return choices[:25]  # Discord limit
    
    @kohls.command(name="bet")
    @app_commands.describe(
        team="Team to bet on (covers the spread)",
        amount="Amount to wager"
    )
    @app_commands.autocomplete(team=team_autocomplete)
    async def bet(
        self,
        interaction: discord.Interaction,
        team: str,
        amount: int
    ):
        """Place a bet on a playoff game."""
        owner_id = str(interaction.user.id)
        
        # Validate amount
        if amount < 5:
            await interaction.response.send_message(
                "❌ Minimum bet is 5 KC.",
                ephemeral=True
            )
            return
        
        # Check balance
        balance = await self._get_balance(owner_id)
        if balance < amount:
            await interaction.response.send_message(
                f"❌ Insufficient funds! You have **{balance:,}** KC.",
                ephemeral=True
            )
            return
        
        # Check if we're in a game thread
        thread_game = None
        if isinstance(interaction.channel, discord.Thread):
            async with db.connection.execute("""
                SELECT game_id, home_team, away_team, spread, kickoff, status
                FROM kohls_games
                WHERE thread_id = ? AND status = 'open'
            """, (interaction.channel.id,)) as cursor:
                thread_game = await cursor.fetchone()
        
        # If in a thread, use that game; otherwise search by team name
        if thread_game:
            game_id, home_team, away_team, spread, kickoff, status = thread_game
            games = [thread_game]
        else:
            # Find matching game by team name
            team_lower = team.lower()
            async with db.connection.execute("""
                SELECT game_id, home_team, away_team, spread, kickoff, status
                FROM kohls_games
                WHERE status = 'open'
                AND (LOWER(home_team) LIKE ? OR LOWER(away_team) LIKE ?)
            """, (f"%{team_lower}%", f"%{team_lower}%")) as cursor:
                games = await cursor.fetchall()
        
        if not games:
            await interaction.response.send_message(
                f"❌ No open games found matching '{team}'.",
                ephemeral=True
            )
            return
        
        if len(games) > 1:
            await interaction.response.send_message(
                f"❌ Multiple games match '{team}'. Be more specific.",
                ephemeral=True
            )
            return
        
        game_id, home_team, away_team, spread, kickoff, status = games[0]
        
        # Check if already locked
        kickoff_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
        if datetime.now(kickoff_dt.tzinfo) > kickoff_dt:
            await interaction.response.send_message(
                "❌ This game has already started!",
                ephemeral=True
            )
            return
        
        # Determine pick
        team_lower = team.lower()
        if team_lower in home_team.lower():
            pick = "home"
            picked_team = home_team
        else:
            pick = "away"
            picked_team = away_team
        
        # Get season
        season = self.config.get("season", {}).get("year", datetime.now().year)
        
        # Deduct bet amount
        await self._update_balance(owner_id, -amount, season)
        
        # Record bet
        await db.connection.execute("""
            INSERT INTO kohls_bets (owner_id, game_id, pick, amount)
            VALUES (?, ?, ?, ?)
        """, (owner_id, game_id, pick, amount))
        await db.connection.commit()
        
        spread_str = f"+{spread}" if spread > 0 else str(spread)
        
        # Get member name
        from lib.members import get_member_registry
        registry = get_member_registry()
        member = registry.find_by_discord_id(owner_id)
        display_name = member.name if member else interaction.user.display_name
        
        # If in a game thread, post publicly; otherwise ephemeral
        if thread_game:
            await interaction.response.send_message(
                f"🎰 **{display_name}** bet **{amount} KC** on **{picked_team}** ({spread_str})"
            )
        else:
            await interaction.response.send_message(
                f"✅ **Bet placed!**\n"
                f"**{picked_team}** ({spread_str}) — **{amount}** KC\n"
                f"`{away_team} @ {home_team}`",
                ephemeral=True
            )
        logger.info(f"Bet placed: {interaction.user} bet {amount} KC on {picked_team}")
    
    @kohls.command(name="sync")
    @app_commands.checks.has_permissions(administrator=True)
    async def sync_balances(self, interaction: discord.Interaction):
        """(Admin) Sync Kohl's Cash balances from Sleeper FAAB."""
        await interaction.response.defer(ephemeral=True)
        
        from config import SLEEPER_LEAGUE_ID
        
        try:
            rosters = await self.bot.sleeper.get_rosters(SLEEPER_LEAGUE_ID)
            users = await self.bot.sleeper.get_users(SLEEPER_LEAGUE_ID)
            
            # Build owner lookup
            user_lookup = {u.get("user_id"): u for u in users}
            
            # Get member registry for Discord ID mapping
            from lib.members import get_member_registry
            registry = get_member_registry()
            sleeper_to_discord = {}
            for member in registry.members:
                for username in member.sleeper_usernames:
                    sleeper_to_discord[username.lower()] = member.discord_id

            
            season = self.config.get("season", {}).get("year", datetime.now().year)
            synced = 0
            
            for roster in rosters:
                owner_id = roster.get("owner_id")
                if not owner_id:
                    continue
                
                settings = roster.get("settings", {})
                # FAAB is stored as waiver_budget - waiver_budget_used
                budget = settings.get("waiver_budget", 100)
                used = settings.get("waiver_budget_used", 0)
                remaining = budget - used
                
                # Get Discord ID from registry
                user = user_lookup.get(owner_id, {})
                username = user.get("display_name", "").lower()
                discord_id = sleeper_to_discord.get(username)
                
                if discord_id and remaining > 0:
                    await db.connection.execute("""
                        INSERT INTO kohls_balances (owner_id, balance, season, last_updated)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(owner_id) DO UPDATE SET
                            balance = ?,
                            last_updated = ?
                    """, (str(discord_id), remaining, season, datetime.now().isoformat(),
                          remaining, datetime.now().isoformat()))
                    synced += 1
            
            await db.connection.commit()
            
            await interaction.followup.send(
                f"✅ Synced Kohl's Cash for **{synced}** owners from Sleeper FAAB.",
                ephemeral=True
            )
            logger.info(f"Synced {synced} Kohl's Cash balances")
            
        except Exception as e:
            logger.error(f"Failed to sync balances: {e}")
            await interaction.followup.send(
                f"❌ Failed to sync: {e}",
                ephemeral=True
            )
    
    @kohls.command(name="loadbalances")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(clear_first="Delete all existing balances before loading (recommended)")
    async def load_balances(self, interaction: discord.Interaction, clear_first: bool = True):
        """(Admin) Load Kohl's Cash balances from config/kohls_balances.yaml."""
        await interaction.response.defer(ephemeral=True)
        
        config_path = Path(__file__).parent.parent / "config" / "kohls_balances.yaml"
        
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f)
            
            balances = config.get("balances", {})
            season = self.config.get("season", {}).get("year", datetime.now().year)
            
            # Get member registry to look up Discord IDs by name
            from lib.members import get_member_registry
            registry = get_member_registry()
            
            # Clear existing entries if requested
            if clear_first:
                await db.connection.execute("DELETE FROM kohls_balances")
                logger.info("Cleared all existing Kohl's Cash balances")
            
            loaded = 0
            not_found = []
            
            for name, balance in balances.items():
                # Look up member by name
                member = registry.find(name)
                
                if not member or not member.discord_id:
                    not_found.append(name)
                    continue
                
                discord_id = member.discord_id
                
                if balance > 0:
                    await db.connection.execute("""
                        INSERT INTO kohls_balances (owner_id, balance, season, last_updated)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(owner_id) DO UPDATE SET
                            balance = ?,
                            last_updated = ?
                    """, (str(discord_id), balance, season, datetime.now().isoformat(),
                          balance, datetime.now().isoformat()))
                    loaded += 1
            
            await db.connection.commit()
            
            msg = f"✅ Loaded Kohl's Cash for **{loaded}** owners from config."
            if clear_first:
                msg = f"🧹 Cleared old entries.\n" + msg
            if not_found:
                msg += f"\n⚠️ Could not find: {', '.join(not_found)}"
            
            await interaction.followup.send(msg, ephemeral=True)
            logger.info(f"Loaded {loaded} Kohl's Cash balances from YAML")
            
        except FileNotFoundError:
            await interaction.followup.send(
                "❌ config/kohls_balances.yaml not found!",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Failed to load balances: {e}")
            await interaction.followup.send(
                f"❌ Failed to load: {e}",
                ephemeral=True
            )
    
    @kohls.command(name="fetchgames")
    @app_commands.checks.has_permissions(administrator=True)
    async def fetch_games(self, interaction: discord.Interaction):
        """(Admin) Fetch NFL games and spreads from The Odds API and create forum threads."""
        await interaction.response.defer(ephemeral=True)
        
        import os
        import aiohttp
        
        api_key = os.getenv("THE_ODDS_API_KEY")
        if not api_key:
            await interaction.followup.send(
                "❌ THE_ODDS_API_KEY not set in .env!",
                ephemeral=True
            )
            return
        
        # Get forum channel
        forum_channel = None
        if self.channel_id:
            forum_channel = self.bot.get_channel(self.channel_id)
            if not forum_channel:
                try:
                    forum_channel = await self.bot.fetch_channel(self.channel_id)
                except:
                    pass
        
        try:
            from clients.odds import OddsClient
            
            async with aiohttp.ClientSession() as session:
                client = OddsClient(api_key, session)
                
                # Fetch spreads AND totals (2 API calls total)
                games = await client.get_nfl_spreads()
                totals = await client.get_totals()
                
                # Build lookup for totals by game_id
                totals_lookup = {t["game_id"]: t["total"] for t in totals}
            
            if not games:
                await interaction.followup.send(
                    "❌ No NFL games found (offseason?).",
                    ephemeral=True
                )
                return
            
            season = self.config.get("season", {}).get("year", datetime.now().year)
            added = 0
            threads_created = 0
            props_added = 0
            
            for game in games:
                game_id = game["game_id"]
                home_team = game["home_team"]
                away_team = game["away_team"]
                spread = game["spread"]
                kickoff = game["kickoff"]
                total = totals_lookup.get(game_id)
                
                # Check if game already exists
                async with db.connection.execute(
                    "SELECT thread_id FROM kohls_games WHERE game_id = ?", (game_id,)
                ) as cursor:
                    existing = await cursor.fetchone()
                
                thread_id = existing[0] if existing else None
                
                # Create forum thread if we have a forum channel and no thread yet
                if forum_channel and not thread_id and isinstance(forum_channel, discord.ForumChannel):
                    try:
                        # Parse kickoff for display (convert UTC to EST)
                        kickoff_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
                        eastern = ZoneInfo("America/New_York")
                        kickoff_est = kickoff_dt.astimezone(eastern)
                        time_str = kickoff_est.strftime("%a %m/%d %I:%M %p EST")
                        spread_str = f"+{spread}" if spread > 0 else str(spread)
                        
                        thread_name = f"🏈 {away_team} @ {home_team}"
                        
                        content = (
                            f"# {away_team} @ {home_team}\n\n"
                            f"**Spread:** {home_team} {spread_str}\n"
                        )
                        if total:
                            content += f"**Total:** {total}\n"
                        content += (
                            f"**Kickoff:** {time_str}\n\n"
                            f"---\n"
                            f"💰 **Place your bets!**\n"
                            f"`/kohls bet <team> <amount>` - Spread bet\n"
                            f"`/kohls props` - View prop bets\n"
                            f"`/kohls propbet <id> <amount>` - Prop bet\n\n"
                            f"Bets lock at kickoff. Winners get 2x their wager!"
                        )
                        
                        thread, message = await forum_channel.create_thread(
                            name=thread_name,
                            content=content
                        )
                        thread_id = thread.id
                        threads_created += 1
                        logger.info(f"Created forum thread for {away_team} @ {home_team}")
                    except Exception as e:
                        logger.error(f"Failed to create thread for {game_id}: {e}")
                
                # Insert or update game
                await db.connection.execute("""
                    INSERT INTO kohls_games (game_id, home_team, away_team, spread, kickoff, status, thread_id, season)
                    VALUES (?, ?, ?, ?, ?, 'open', ?, ?)
                    ON CONFLICT(game_id) DO UPDATE SET
                        spread = ?,
                        kickoff = ?,
                        thread_id = COALESCE(kohls_games.thread_id, ?),
                        status = CASE WHEN status = 'final' THEN status ELSE 'open' END
                """, (
                    game_id,
                    home_team,
                    away_team,
                    spread,
                    kickoff,
                    thread_id,
                    season,
                    spread,
                    kickoff,
                    thread_id,
                ))
                added += 1
                
                # Add total as prop bet if available
                if total:
                    for outcome in ["over", "under"]:
                        await db.connection.execute("""
                            INSERT OR IGNORE INTO kohls_props (game_id, market_key, description, line, outcome, odds)
                            VALUES (?, 'totals', ?, ?, ?, -110)
                        """, (
                            game_id,
                            f"{away_team} @ {home_team} {outcome.title()} {total}",
                            total,
                            outcome,
                        ))
                        props_added += 1
            
            await db.connection.commit()
            
            msg = f"✅ Fetched **{added}** NFL games from The Odds API."
            if threads_created > 0:
                msg += f"\n🧵 Created **{threads_created}** forum threads in #kohls-cashino."
            if props_added > 0:
                msg += f"\n🎲 Added **{props_added}** total props."
            
            await interaction.followup.send(msg, ephemeral=True)
            logger.info(f"Fetched {added} games, created {threads_created} threads, {props_added} props")
            
        except Exception as e:
            logger.error(f"Failed to fetch games: {e}")
            await interaction.followup.send(
                f"❌ Failed to fetch games: {e}",
                ephemeral=True
            )
    
    @kohls.command(name="games")
    async def list_games(self, interaction: discord.Interaction):
        """View available games to bet on."""
        async with db.connection.execute("""
            SELECT game_id, home_team, away_team, spread, kickoff, status
            FROM kohls_games
            WHERE status = 'open'
            ORDER BY kickoff ASC
        """) as cursor:
            games = await cursor.fetchall()
        
        if not games:
            await interaction.response.send_message(
                "No games available to bet on right now!\n"
                "Games will be added before each playoff round.",
                ephemeral=True
            )
            return
        
        embed = discord.Embed(
            title="🏈 Available Games",
            description="Place bets with `/kohls bet <team> <amount>`",
            color=discord.Color.blue()
        )
        
        for game_id, home, away, spread, kickoff, status in games:
            # Parse kickoff time (convert to EST)
            try:
                kickoff_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
                eastern = ZoneInfo("America/New_York")
                kickoff_est = kickoff_dt.astimezone(eastern)
                time_str = kickoff_est.strftime("%a %m/%d %I:%M %p EST")
            except:
                time_str = kickoff
            
            spread_str = f"+{spread}" if spread > 0 else str(spread)
            
            embed.add_field(
                name=f"{away} @ {home}",
                value=f"**Spread:** {home} {spread_str}\n**Kickoff:** {time_str}",
                inline=True
            )
        
        balance = await self._get_balance(str(interaction.user.id))
        embed.set_footer(text=f"Your balance: {balance:,} KC • Min bet: 5 KC")
        
        await interaction.response.send_message(embed=embed)
    
    @kohls.command(name="props")
    @app_commands.describe(game="Team name to filter by (optional)")
    async def list_props(self, interaction: discord.Interaction, game: str = None):
        """View available prop bets."""
        if game:
            async with db.connection.execute("""
                SELECT p.prop_id, p.description, p.line, p.odds, g.home_team, g.away_team
                FROM kohls_props p
                JOIN kohls_games g ON p.game_id = g.game_id
                WHERE p.status = 'open'
                AND (LOWER(g.home_team) LIKE ? OR LOWER(g.away_team) LIKE ?)
                ORDER BY p.prop_id
            """, (f"%{game.lower()}%", f"%{game.lower()}%")) as cursor:
                props = await cursor.fetchall()
        else:
            async with db.connection.execute("""
                SELECT p.prop_id, p.description, p.line, p.odds, g.home_team, g.away_team
                FROM kohls_props p
                JOIN kohls_games g ON p.game_id = g.game_id
                WHERE p.status = 'open'
                ORDER BY g.kickoff, p.prop_id
                LIMIT 20
            """) as cursor:
                props = await cursor.fetchall()
        
        if not props:
            await interaction.response.send_message(
                "No prop bets available. Run `/kohls fetchprops` to load props.",
                ephemeral=True
            )
            return
        
        embed = discord.Embed(
            title="🎲 Prop Bets",
            description="Use `/kohls propbet <id> <amount>` to place a bet",
            color=discord.Color.purple()
        )
        
        lines = []
        for prop_id, desc, line, odds, home, away in props:
            odds_str = f"+{odds}" if odds > 0 else str(odds)
            lines.append(f"`{prop_id}` {desc} ({odds_str})")
        
        embed.description = "\n".join(lines[:20])
        if len(props) > 20:
            embed.set_footer(text=f"Showing 20 of {len(props)} props. Use 'game' filter to narrow down.")
        else:
            balance = await self._get_balance(str(interaction.user.id))
            embed.set_footer(text=f"Your balance: {balance:,} KC")
        
        await interaction.response.send_message(embed=embed)
    
    async def prop_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        """Autocomplete for prop bet selection."""
        choices = []
        
        # Check if we're in a game thread
        if isinstance(interaction.channel, discord.Thread):
            async with db.connection.execute("""
                SELECT p.prop_id, p.description, p.odds
                FROM kohls_props p
                JOIN kohls_games g ON p.game_id = g.game_id
                WHERE g.thread_id = ? AND p.status = 'open'
                ORDER BY p.market_key, p.prop_id
                LIMIT 25
            """, (interaction.channel.id,)) as cursor:
                props = await cursor.fetchall()
        else:
            # Show all open props
            async with db.connection.execute("""
                SELECT prop_id, description, odds
                FROM kohls_props
                WHERE status = 'open'
                ORDER BY prop_id
                LIMIT 25
            """) as cursor:
                props = await cursor.fetchall()
        
        for prop_id, desc, odds in props:
            # Truncate description if needed (Discord limit is 100 chars)
            short_desc = desc[:90] if len(desc) > 90 else desc
            odds_str = f"+{odds}" if odds > 0 else str(odds)
            
            if not current or current.lower() in desc.lower() or str(prop_id) == current:
                choices.append(app_commands.Choice(
                    name=f"{short_desc} ({odds_str})",
                    value=prop_id
                ))
        
        return choices[:25]
    
    @kohls.command(name="propbet")
    @app_commands.describe(
        prop_id="Prop bet ID from /kohls props",
        amount="Amount to wager"
    )
    @app_commands.autocomplete(prop_id=prop_autocomplete)
    async def prop_bet(
        self,
        interaction: discord.Interaction,
        prop_id: int,
        amount: int
    ):
        """Place a prop bet."""
        owner_id = str(interaction.user.id)
        
        # Validate amount
        if amount < 5:
            await interaction.response.send_message(
                "❌ Minimum bet is 5 KC.",
                ephemeral=True
            )
            return
        
        # Check balance
        balance = await self._get_balance(owner_id)
        if balance < amount:
            await interaction.response.send_message(
                f"❌ Insufficient funds! You have **{balance:,}** KC.",
                ephemeral=True
            )
            return
        
        # Get the prop
        async with db.connection.execute("""
            SELECT p.prop_id, p.description, p.line, p.odds, p.status, g.kickoff, g.home_team, g.away_team
            FROM kohls_props p
            JOIN kohls_games g ON p.game_id = g.game_id
            WHERE p.prop_id = ?
        """, (prop_id,)) as cursor:
            prop = await cursor.fetchone()
        
        if not prop:
            await interaction.response.send_message(
                f"❌ Prop bet #{prop_id} not found.",
                ephemeral=True
            )
            return
        
        prop_id, description, line, odds, status, kickoff, home, away = prop
        
        if status != "open":
            await interaction.response.send_message(
                "❌ This prop bet is no longer available.",
                ephemeral=True
            )
            return
        
        # Check if game started
        try:
            kickoff_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            if datetime.now(kickoff_dt.tzinfo) > kickoff_dt:
                await interaction.response.send_message(
                    "❌ This game has already started!",
                    ephemeral=True
                )
                return
        except:
            pass
        
        # Record transaction and bet
        await self._record_transaction(
            owner_id, -amount, "prop_bet",
            reference_id=str(prop_id),
            description=f"Prop bet: {description}"
        )
        
        await db.connection.execute("""
            INSERT INTO kohls_prop_bets (owner_id, prop_id, amount)
            VALUES (?, ?, ?)
        """, (owner_id, prop_id, amount))
        await db.connection.commit()
        
        odds_str = f"+{odds}" if odds > 0 else str(odds)
        
        # Get member name
        from lib.members import get_member_registry
        registry = get_member_registry()
        member = registry.find_by_discord_id(owner_id)
        display_name = member.name if member else interaction.user.display_name
        
        # Check if in thread - post publicly if so
        if isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                f"🎲 **{display_name}** bet **{amount} KC** on: {description} ({odds_str})"
            )
        else:
            await interaction.response.send_message(
                f"✅ **Prop bet placed!**\n"
                f"**{description}** ({odds_str}) — **{amount}** KC",
                ephemeral=True
            )
        logger.info(f"Prop bet placed: {interaction.user} bet {amount} KC on prop {prop_id}")
    
    @kohls.command(name="fetchprops")
    @app_commands.checks.has_permissions(administrator=True)
    async def fetch_props(self, interaction: discord.Interaction):
        """(Admin) Fetch player props from The Odds API and post to threads."""
        await interaction.response.defer(ephemeral=True)
        
        import os
        import aiohttp
        
        api_key = os.getenv("THE_ODDS_API_KEY")
        if not api_key:
            await interaction.followup.send(
                "❌ THE_ODDS_API_KEY not set in .env!",
                ephemeral=True
            )
            return
        
        try:
            from clients.odds import OddsClient
            
            # Get our games with thread IDs
            async with db.connection.execute("""
                SELECT game_id, home_team, away_team, thread_id
                FROM kohls_games
                WHERE status = 'open' AND thread_id IS NOT NULL
            """) as cursor:
                our_games = await cursor.fetchall()
            
            if not our_games:
                await interaction.followup.send(
                    "❌ No open games with threads. Run `/kohls fetchgames` first.",
                    ephemeral=True
                )
                return
            
            async with aiohttp.ClientSession() as session:
                client = OddsClient(api_key, session)
                
                props_added = 0
                threads_updated = 0
                
                # Fetch player props for each game (1 API call per game)
                for game_id, home, away, thread_id in our_games:
                    player_props = await client.get_player_props(game_id)
                    
                    if not player_props:
                        continue
                    
                    game_props = []
                    
                    for prop in player_props:
                        result = await db.connection.execute("""
                            INSERT OR IGNORE INTO kohls_props (game_id, market_key, description, line, outcome, odds)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (
                            prop["game_id"],
                            prop["market_key"],
                            prop["description"],
                            prop["line"],
                            prop["outcome"],
                            prop["odds"],
                        ))
                        if result.rowcount > 0:
                            props_added += 1
                        game_props.append(prop)
                    
                    # Post to thread if we have new props
                    if game_props and thread_id:
                        try:
                            thread = self.bot.get_channel(int(thread_id))
                            if not thread:
                                thread = await self.bot.fetch_channel(int(thread_id))
                            if thread:
                                # Get prop IDs for this game
                                async with db.connection.execute("""
                                    SELECT prop_id, description, odds
                                    FROM kohls_props
                                    WHERE game_id = ? AND market_key != 'totals' AND status = 'open'
                                    ORDER BY market_key, prop_id
                                """, (game_id,)) as cursor:
                                    db_props = await cursor.fetchall()
                                
                                if db_props:
                                    lines = [f"🎲 **Player Props Available!**\n"]
                                    for prop_id, desc, odds in db_props:
                                        odds_str = f"+{odds}" if odds > 0 else str(odds)
                                        lines.append(f"`{prop_id}` {desc} ({odds_str})")
                                    lines.append(f"\nUse `/kohls propbet <id> <amount>` to bet!")
                                    
                                    await thread.send("\n".join(lines))
                                    threads_updated += 1
                        except Exception as e:
                            logger.error(f"Failed to post props to thread {thread_id}: {e}")
                
                await db.connection.commit()
            
            msg = f"✅ Added **{props_added}** player props from The Odds API."
            if threads_updated > 0:
                msg += f"\n📝 Posted to **{threads_updated}** game threads."
            
            await interaction.followup.send(msg, ephemeral=True)
            logger.info(f"Fetched {props_added} props, updated {threads_updated} threads")
            
        except Exception as e:
            logger.error(f"Failed to fetch props: {e}")
            await interaction.followup.send(
                f"❌ Failed to fetch props: {e}",
                ephemeral=True
            )

    
    @kohls.command(name="resolvegame")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        home_team="Home team name (partial match)",
        home_score="Home team final score",
        away_score="Away team final score"
    )
    async def resolve_game(
        self,
        interaction: discord.Interaction,
        home_team: str,
        home_score: int,
        away_score: int
    ):
        """(Admin) Enter final score and resolve bets for a game."""
        await interaction.response.defer()
        
        # Find the game
        async with db.connection.execute("""
            SELECT game_id, home_team, away_team, spread, status, thread_id
            FROM kohls_games
            WHERE LOWER(home_team) LIKE ?
            AND status != 'final'
        """, (f"%{home_team.lower()}%",)) as cursor:
            game = await cursor.fetchone()
        
        if not game:
            await interaction.followup.send(
                f"❌ No pending game found for '{home_team}'.",
                ephemeral=True
            )
            return
        
        game_id, home, away, spread, status, thread_id = game
        
        # Calculate result (spread is from home perspective)
        # Negative spread = home favored, must win by more than that
        score_diff = home_score - away_score  # Positive = home won
        adjusted_diff = score_diff + spread   # Add spread (negative if favorite)
        
        # Determine winners
        if adjusted_diff > 0:
            winner = "home"
        elif adjusted_diff < 0:
            winner = "away"
        else:
            winner = "push"
        
        # Update game as final
        await db.connection.execute("""
            UPDATE kohls_games
            SET status = 'final', home_score = ?, away_score = ?
            WHERE game_id = ?
        """, (home_score, away_score, game_id))
        
        # Get all bets for this game
        async with db.connection.execute("""
            SELECT bet_id, owner_id, pick, amount
            FROM kohls_bets
            WHERE game_id = ? AND result = 'pending'
        """, (game_id,)) as cursor:
            bets = await cursor.fetchall()
        
        season = self.config.get("season", {}).get("year", datetime.now().year)
        winners_list = []
        losers_list = []
        
        for bet_id, owner_id, pick, amount in bets:
            if winner == "push":
                # Refund
                await self._update_balance(owner_id, amount, season)
                await db.connection.execute(
                    "UPDATE kohls_bets SET result = 'push', payout = ? WHERE bet_id = ?",
                    (amount, bet_id)
                )
            elif pick == winner:
                # Winner - pays even money
                payout = amount * 2
                await self._update_balance(owner_id, payout, season)
                await db.connection.execute(
                    "UPDATE kohls_bets SET result = 'won', payout = ? WHERE bet_id = ?",
                    (payout, bet_id)
                )
                # Get name
                from lib.members import get_member_registry
                registry = get_member_registry()
                member = registry.find_by_discord_id(owner_id)
                name = member.name if member else "Unknown"
                winners_list.append(f"**{name}** won **{amount}** KC!")
            else:
                # Loser
                await db.connection.execute(
                    "UPDATE kohls_bets SET result = 'lost', payout = 0 WHERE bet_id = ?",
                    (bet_id,)
                )
                from lib.members import get_member_registry
                registry = get_member_registry()
                member = registry.find_by_discord_id(owner_id)
                name = member.name if member else "Unknown"
                losers_list.append(f"**{name}** lost **{amount}** KC")
        
        await db.connection.commit()
        
        # Build result message
        spread_str = f"+{spread}" if spread > 0 else str(spread)
        result_msg = (
            f"🏈 **{away} {away_score}** @ **{home} {home_score}**\n"
            f"Spread: {home} {spread_str}\n\n"
        )
        
        if winner == "push":
            result_msg += "**Result: PUSH** - All bets refunded!\n"
        elif winner == "home":
            result_msg += f"**{home} covered!**\n\n"
        else:
            result_msg += f"**{away} covered!**\n\n"
        
        if winners_list:
            result_msg += "💰 **Winners:**\n" + "\n".join(winners_list) + "\n\n"
        if losers_list:
            result_msg += "💸 **Losers:**\n" + "\n".join(losers_list)
        
        if not winners_list and not losers_list:
            result_msg += "_No bets were placed on this game._"
        
        # Post to the game thread if it exists
        if thread_id:
            try:
                thread = self.bot.get_channel(thread_id)
                if not thread:
                    thread = await self.bot.fetch_channel(thread_id)
                if thread:
                    await thread.send(result_msg)
                    logger.info(f"Posted results to thread {thread_id}")
            except Exception as e:
                logger.error(f"Failed to post to thread {thread_id}: {e}")
        
        await interaction.followup.send(result_msg)
        logger.info(f"Game resolved: {away} {away_score} @ {home} {home_score}")
    
    @kohls.command(name="autoresolve")
    @app_commands.checks.has_permissions(administrator=True)
    async def auto_resolve(self, interaction: discord.Interaction):
        """(Admin) Auto-resolve games using NFL schedule data from nflreadpy."""
        await interaction.response.defer()
        
        from clients.nfl_schedule import NFLScheduleClient
        from lib.members import get_member_registry
        
        # Get current NFL season (playoffs are in January/February after the season year)
        current_month = datetime.now().month
        if current_month <= 2:
            # If Jan/Feb, playoffs are for previous year's season
            nfl_season = datetime.now().year - 1
        else:
            nfl_season = datetime.now().year
        
        try:
            client = NFLScheduleClient()
            results = client.get_playoff_results(nfl_season)
            
            if not results:
                await interaction.followup.send(
                    "❌ No completed playoff games found in nflreadpy data.",
                    ephemeral=True
                )
                return
            
            # Get our pending games
            async with db.connection.execute("""
                SELECT game_id, home_team, away_team, spread, status, thread_id
                FROM kohls_games
                WHERE status != 'final'
            """) as cursor:
                pending_games = await cursor.fetchall()
            
            if not pending_games:
                await interaction.followup.send(
                    "No pending games to resolve.",
                    ephemeral=True
                )
                return
            
            season = self.config.get("season", {}).get("year", datetime.now().year)
            registry = get_member_registry()
            resolved_count = 0
            all_results = []
            
            for game_id, home, away, spread, status, thread_id in pending_games:
                # Try to match with nflreadpy results
                matched_result = None
                for result in results:
                    # Match by team abbreviations (nflreadpy uses abbreviations)
                    if (result['home_team'].upper() in home.upper() or 
                        home.upper() in result['home_team'].upper()):
                        matched_result = result
                        break
                
                if not matched_result:
                    continue
                
                home_score = matched_result['home_score']
                away_score = matched_result['away_score']
                
                # Calculate result
                score_diff = home_score - away_score
                adjusted_diff = score_diff + spread
                
                if adjusted_diff > 0:
                    winner = "home"
                elif adjusted_diff < 0:
                    winner = "away"
                else:
                    winner = "push"
                
                # Update game as final
                await db.connection.execute("""
                    UPDATE kohls_games
                    SET status = 'final', home_score = ?, away_score = ?
                    WHERE game_id = ?
                """, (home_score, away_score, game_id))
                
                # Get bets for this game
                async with db.connection.execute("""
                    SELECT bet_id, owner_id, pick, amount
                    FROM kohls_bets
                    WHERE game_id = ? AND result = 'pending'
                """, (game_id,)) as cursor:
                    bets = await cursor.fetchall()
                
                winners_list = []
                losers_list = []
                
                for bet_id, owner_id, pick, amount in bets:
                    if winner == "push":
                        await self._update_balance(owner_id, amount, season)
                        await db.connection.execute(
                            "UPDATE kohls_bets SET result = 'push', payout = ? WHERE bet_id = ?",
                            (amount, bet_id)
                        )
                    elif pick == winner:
                        payout = amount * 2
                        await self._update_balance(owner_id, payout, season)
                        await db.connection.execute(
                            "UPDATE kohls_bets SET result = 'won', payout = ? WHERE bet_id = ?",
                            (payout, bet_id)
                        )
                        member = registry.find_by_discord_id(owner_id)
                        name = member.name if member else "Unknown"
                        winners_list.append(f"**{name}** +{amount} KC")
                    else:
                        await db.connection.execute(
                            "UPDATE kohls_bets SET result = 'lost', payout = 0 WHERE bet_id = ?",
                            (bet_id,)
                        )
                        member = registry.find_by_discord_id(owner_id)
                        name = member.name if member else "Unknown"
                        losers_list.append(f"**{name}** -{amount} KC")
                
                resolved_count += 1
                spread_str = f"+{spread}" if spread > 0 else str(spread)
                cover_result = f"{home} covered" if winner == "home" else (f"{away} covered" if winner == "away" else "PUSH")
                
                game_summary = f"**{away} {away_score} @ {home} {home_score}** ({spread_str}) → {cover_result}"
                if winners_list:
                    game_summary += f"\n  💰 {', '.join(winners_list)}"
                if losers_list:
                    game_summary += f"\n  💸 {', '.join(losers_list)}"
                all_results.append(game_summary)
                
                # Post to the game thread if it exists
                if thread_id:
                    try:
                        thread = self.bot.get_channel(thread_id)
                        if not thread:
                            thread = await self.bot.fetch_channel(thread_id)
                        if thread:
                            thread_msg = (
                                f"🏈 **FINAL: {away} {away_score} @ {home} {home_score}**\n\n"
                                f"**{cover_result}!**\n\n"
                            )
                            if winners_list:
                                thread_msg += "💰 **Winners:** " + ", ".join(winners_list) + "\n"
                            if losers_list:
                                thread_msg += "💸 **Losers:** " + ", ".join(losers_list)
                            await thread.send(thread_msg)
                    except Exception as e:
                        logger.error(f"Failed to post to thread {thread_id}: {e}")
            
            await db.connection.commit()
            
            if resolved_count == 0:
                await interaction.followup.send(
                    "No matching games found to auto-resolve.\n"
                    "Make sure game names match NFL team names.",
                    ephemeral=True
                )
                return
            
            result_msg = f"✅ **Auto-resolved {resolved_count} game(s):**\n\n"
            result_msg += "\n\n".join(all_results)
            
            await interaction.followup.send(result_msg)
            logger.info(f"Auto-resolved {resolved_count} games from nflreadpy")
            
        except Exception as e:
            logger.error(f"Failed to auto-resolve: {e}")
            await interaction.followup.send(
                f"❌ Failed to auto-resolve: {e}",
                ephemeral=True
            )


async def setup(bot: "DynastyBot"):
    """Load the Kohl's Cash cog."""
    await bot.add_cog(KohlsCash(bot))
