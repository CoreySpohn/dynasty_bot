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
from lib.results import get_league_chain
from lib.taxi_rules import (
    TAXI_MAX_SEASONS,
    Acquisition,
    audit,
    build_draft_index,
    build_records,
    eligible_additions,
    over_slot_limit,
)

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
            await self._check_pending_raids()
        except Exception as e:
            logger.error(f"Error in raid reminder loop: {e}")

    async def _check_pending_raids(self) -> int:
        """Check pending raids and send a reminder for any still active.

        Returns:
            The number of reminder messages sent.
        """
        # Get pending raids from database
        async with db.connection.execute("""
            SELECT id, raider_user_id, victim_user_id, player_id, player_name, raid_date
            FROM raids
            WHERE status = 'pending'
        """) as cursor:
            pending = await cursor.fetchall()

        if not pending:
            return 0

        registry = get_member_registry()
        reminders_sent = 0

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
            if not ALERT_CHANNEL_ID:
                logger.warning("ALERT_CHANNEL_ID not configured, skipping raid reminder")
                continue

            channel = self.bot.get_channel(ALERT_CHANNEL_ID)
            if not channel:
                logger.error(f"Could not find alert channel {ALERT_CHANNEL_ID}")
                continue

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
            reminders_sent += 1

        return reminders_sent

    @raid_reminder_loop.before_loop
    async def before_raid_reminder(self) -> None:
        """Wait for bot to be ready before starting loop."""
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="checkraids",
        description="[Admin] Manually check pending raids and send reminders now",
    )
    @app_commands.default_permissions(administrator=True)
    async def check_raids_now(self, interaction: discord.Interaction) -> None:
        """Admin command to force a pending-raid reminder check immediately."""
        await interaction.response.defer(ephemeral=True)

        try:
            sent = await self._check_pending_raids()
        except Exception as e:
            logger.error(f"Manual raid check failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error checking raids: {e}", ephemeral=True)
            return

        if sent == 0:
            await interaction.followup.send(
                "✅ Checked pending raids. None pending, or nothing to remind.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ Checked pending raids. Sent {sent} reminder(s).", ephemeral=True
            )
    
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
    
    # =====================================================================
    # League taxi rules (Sleeper does not enforce these)
    # =====================================================================

    async def _draft_index(self) -> dict:
        """Every drafted player, indexed to their earliest draft.

        Walks the whole `previous_league_id` chain. Sleeper retains drafts
        permanently, so this is derived rather than stored.
        """
        chain = await get_league_chain(self.bot.sleeper, self.league_id)
        picks_by_league = []
        for league in chain:
            season = int(league.get("season") or 0)
            for draft in await self.bot.sleeper.get_drafts(league["league_id"]):
                picks = await self.bot.sleeper.get_picks_in_draft(draft["draft_id"])
                picks_by_league.append((season, picks))
        return build_draft_index(picks_by_league)

    async def _ledger(self) -> tuple[set, dict]:
        """Recorded acquisitions and activations from taxi_ledger.

        Returns:
            (traded_for pairs, {(owner_id, player_id): activated_season}).
        """
        traded_for: set[tuple[Optional[str], str]] = set()
        activated: dict[tuple[Optional[str], str], int] = {}

        async with db.execute(
            "SELECT owner_id, player_id, acquisition, activated_season "
            "FROM taxi_ledger"
        ) as cursor:
            rows = await cursor.fetchall()

        for owner_id, player_id, acquisition, activated_season in rows:
            if acquisition == Acquisition.TRADE.value:
                traded_for.add((owner_id, player_id))
            if activated_season is not None:
                activated[(owner_id, player_id)] = activated_season
        return traded_for, activated

    async def _ledger_size(self) -> int:
        async with db.execute("SELECT COUNT(*) FROM taxi_ledger") as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    # Shown whenever the ledger hasn't been seeded. Without recorded
    # activations, every own-draftee sitting on the active roster looks
    # taxi-eligible - which reads as 70 eligible players when the real answer
    # is zero. Better to say so than to quietly present the wrong number.
    UNSEEDED_WARNING = (
        "⚠️ The taxi ledger is empty, so no activations are on record. "
        "Anyone an owner drafted looks eligible even if they've been starting "
        "for two years. Run `/taxibackfill` first."
    )

    async def _taxi_context(self) -> dict:
        """Assemble everything the rule engine needs."""
        league = await self.bot.sleeper.get_league(self.league_id)
        rosters = await self.bot.sleeper.get_rosters(self.league_id)
        users = await self.bot.sleeper.get_users(self.league_id)
        players = await self.bot.sleeper.get_all_players()

        draft_index = await self._draft_index()
        traded_for, activated = await self._ledger()

        return {
            "league": league,
            "rosters": rosters,
            "players": players,
            "owners": {
                r["roster_id"]: {
                    u["user_id"]: u.get("display_name", "Unknown") for u in users
                }.get(r.get("owner_id", ""), f"Team {r['roster_id']}")
                for r in rosters
            },
            "season": int(league.get("season") or 0),
            "taxi_slots": league.get("settings", {}).get("taxi_slots", 5),
            "records": build_records(rosters, draft_index, traded_for, activated),
        }

    def _player_name(self, players: dict, player_id: str) -> str:
        return (players.get(player_id) or {}).get("full_name") or player_id

    @app_commands.command(
        name="taxiaudit",
        description="Check every taxi squad against league rules, not Sleeper's",
    )
    @app_commands.describe(
        season="Season to judge against (defaults to the upcoming one)"
    )
    async def taxi_audit(
        self, interaction: discord.Interaction, season: Optional[int] = None
    ) -> None:
        await interaction.response.defer()
        try:
            ctx = await self._taxi_context()
            target = season or ctx["season"]
            violations = audit(ctx["records"], target)
            over = over_slot_limit(ctx["rosters"], ctx["taxi_slots"])

            embed = discord.Embed(
                title=f"🚕 Taxi Audit — {target} season",
                description=(
                    "Sleeper allows anyone inside their first three years on a "
                    "taxi slot. League rules are narrower: **only rookies you "
                    "drafted yourself**, never after being activated, never "
                    f"acquired by trade, and no more than **{TAXI_MAX_SEASONS} "
                    "seasons** from their draft."
                ),
                color=(
                    discord.Color.red() if violations or over
                    else discord.Color.green()
                ),
            )

            if violations:
                by_owner: dict[str, list[str]] = {}
                for violation in violations:
                    owner = ctx["owners"].get(
                        violation.roster_id, f"Team {violation.roster_id}"
                    )
                    detail = self._player_name(ctx["players"], violation.player_id)
                    if violation.draft_season:
                        detail += (
                            f" (drafted {violation.draft_season}"
                            + (
                                f" rd{violation.draft_round}"
                                if violation.draft_round
                                else ""
                            )
                            + f", {violation.seasons_used} seasons)"
                        )
                    by_owner.setdefault(owner, []).append(
                        f"• {detail} — {violation.reason_text}"
                    )
                for owner, lines in sorted(by_owner.items()):
                    embed.add_field(
                        name=f"❌ {owner}", value="\n".join(lines), inline=False
                    )
            else:
                embed.add_field(
                    name="✅ All clear",
                    value="Every taxi squad is legal under league rules.",
                    inline=False,
                )

            if over:
                embed.add_field(
                    name="🚨 Over the slot limit",
                    value="\n".join(
                        f"• {ctx['owners'].get(roster_id, roster_id)} — "
                        f"{count}/{ctx['taxi_slots']}"
                        for roster_id, count in over
                    ),
                    inline=False,
                )

            embed.set_footer(
                text=(
                    f"{len(violations)} violation(s) • judged against the "
                    f"{target} season"
                )
            )
            if not await self._ledger_size():
                # Age and origin violations are unaffected by the ledger, but
                # "already activated" ones can't be detected without it.
                embed.add_field(
                    name="Incomplete data",
                    value=(
                        "No activations on record, so this can only catch age "
                        "and origin violations. Run `/taxibackfill`."
                    ),
                    inline=False,
                )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"taxiaudit failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error auditing taxi squads: {e}")

    @app_commands.command(
        name="taxieligible",
        description="Who an owner could legally still put on their taxi squad",
    )
    @app_commands.describe(team_name="Owner to check (defaults to everyone)")
    async def taxi_eligible(
        self, interaction: discord.Interaction, team_name: Optional[str] = None
    ) -> None:
        await interaction.response.defer()
        try:
            ctx = await self._taxi_context()
            eligible = eligible_additions(ctx["records"], ctx["season"])

            by_owner: dict[str, list[str]] = {}
            for record in eligible:
                owner = ctx["owners"].get(
                    record.roster_id, f"Team {record.roster_id}"
                )
                if team_name and team_name.lower() not in owner.lower():
                    continue
                label = self._player_name(ctx["players"], record.player_id)
                if record.draft_season:
                    label += f" ({record.draft_season} rd{record.draft_round})"
                by_owner.setdefault(owner, []).append(f"• {label}")

            embed = discord.Embed(
                title="🚕 Taxi-Eligible Players",
                description=(
                    "Players still on the active roster who could legally be "
                    "moved onto a taxi slot: own draftees, never activated, "
                    f"within {TAXI_MAX_SEASONS} seasons of their draft."
                ),
                color=discord.Color.blue(),
            )
            if not await self._ledger_size():
                embed.description = f"{self.UNSEEDED_WARNING}\n\n{embed.description}"
                embed.color = discord.Color.orange()

            if by_owner:
                for owner, lines in sorted(by_owner.items()):
                    embed.add_field(
                        name=owner, value="\n".join(lines[:10]), inline=False
                    )
            else:
                embed.add_field(
                    name="Nobody",
                    value=(
                        "No eligible players. Under league rules taxi slots can "
                        "only be filled immediately after the rookie draft, from "
                        "that owner's own picks."
                    ),
                    inline=False,
                )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"taxieligible failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error checking eligibility: {e}")

    @app_commands.command(
        name="taxibackfill",
        description="Admin: seed the taxi ledger from draft history and current rosters",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def taxi_backfill(self, interaction: discord.Interaction) -> None:
        """Bootstrap the ledger so activation tracking has a starting point.

        Draft origin is recoverable from Sleeper forever, but activation
        history is not - so anyone currently on the *active* roster who was
        their own draftee is recorded as already activated. That's the
        conservative reading: under league rules they could not go back on
        taxi anyway, and it means we never wrongly re-open a slot.
        """
        await interaction.response.defer()
        try:
            ctx = await self._taxi_context()
            season = ctx["season"]

            written = activations = 0
            for record in ctx["records"]:
                if record.acquisition != Acquisition.ROOKIE_DRAFT:
                    # Only own-draftees can ever matter for taxi purposes.
                    continue

                # On the active roster and eligible by age => must have been
                # activated at some point, since taxi is the only other place
                # an own-draftee could have been.
                treat_as_activated = not record.on_taxi
                async with db.execute(
                    """
                    INSERT INTO taxi_ledger (
                        owner_id, player_id, acquisition, draft_season,
                        draft_round, activated_season, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(owner_id, player_id) DO UPDATE SET
                        acquisition=excluded.acquisition,
                        draft_season=excluded.draft_season,
                        draft_round=excluded.draft_round,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        record.owner_id,
                        record.player_id,
                        record.acquisition.value,
                        record.draft_season,
                        record.draft_round,
                        season if treat_as_activated else None,
                        "backfilled from draft history",
                    ),
                ) as cursor:
                    written += cursor.rowcount or 0
                if treat_as_activated:
                    activations += 1

            violations = audit(ctx["records"], season)
            await interaction.followup.send(
                f"✅ Taxi ledger seeded: **{written}** own-draftee record(s), "
                f"**{activations}** treated as already activated (currently on "
                "the active roster).\n"
                f"Draft origin came from Sleeper and is exact. Activations "
                f"before today aren't recoverable from Sleeper, so anyone "
                f"already off taxi is assumed activated — conservative, since "
                f"they'd be ineligible either way.\n"
                f"`/taxiaudit` currently reports **{len(violations)}** "
                f"violation(s) for {season}."
            )

        except Exception as e:
            logger.error(f"taxibackfill failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error backfilling taxi ledger: {e}")

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
