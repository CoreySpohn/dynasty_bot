"""Nickname Tagging Cog for Dynasty Bot.

Keeps owner nicknames tagged with useful league context: their current
standings rank during the season, their rookie draft pick slot once the
draft order is set, and who's currently on the clock while an actual
Sleeper draft is in progress.
"""

import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import SLEEPER_LEAGUE_ID
from lib.members import get_member_registry
from lib.nicknames import apply_tag, clear_tag, find_discord_id_with_tag
from lib.standings import compute_standings, ordinal

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.nicknames")

CLOCK_TAG = "ON THE CLOCK"


class NicknameTags(commands.Cog):
    """Tags owner nicknames with standings/draft context."""

    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID

    def cog_load(self) -> None:
        self.standings_nickname_loop.start()
        self.draft_clock_loop.start()

    def cog_unload(self) -> None:
        self.standings_nickname_loop.cancel()
        self.draft_clock_loop.cancel()

    def _get_guild(self) -> Optional[discord.Guild]:
        """This bot only ever runs in one guild (the league's server)."""
        if not self.bot.guilds:
            return None
        return self.bot.guilds[0]

    # =========================================================================
    # Standings nickname sync
    # =========================================================================

    @tasks.loop(hours=24)
    async def standings_nickname_loop(self):
        try:
            nfl_state = await self.bot.sleeper.get_nfl_state()
            if nfl_state.get("season_type") not in ("regular", "post"):
                return
            await self._sync_standings_nicknames()
        except Exception as e:
            logger.error(f"Error in standings_nickname_loop: {e}", exc_info=True)

    @standings_nickname_loop.before_loop
    async def before_standings_nickname_loop(self):
        await self.bot.wait_until_ready()

    async def _sync_standings_nicknames(self) -> int:
        """Tag every known member's nickname with their current standings rank.

        Returns the number of members successfully tagged.
        """
        guild = self._get_guild()
        if not guild:
            logger.warning("No guild available to sync standings nicknames")
            return 0

        standings = await compute_standings(self.bot.sleeper, self.league_id)
        rank_by_owner_id = {entry.owner_id: entry.rank for entry in standings}

        tagged = 0
        for league_member in get_member_registry().members:
            if not league_member.discord_id or not league_member.sleeper_id:
                continue

            rank = rank_by_owner_id.get(league_member.sleeper_id)
            if rank is None:
                continue

            member = guild.get_member(int(league_member.discord_id))
            if not member:
                continue

            if await apply_tag(member, f"{ordinal(rank)} place"):
                tagged += 1

        return tagged

    @app_commands.command(
        name="syncstandingsnicknames",
        description="(Admin) Tag every owner's nickname with their current standings rank",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def sync_standings_nicknames(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            tagged = await self._sync_standings_nicknames()
            await interaction.followup.send(
                f"✅ Tagged {tagged} nickname(s) with standings rank."
            )
        except Exception as e:
            logger.error(f"syncstandingsnicknames failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error syncing nicknames: {e}")

    # =========================================================================
    # Draft order nickname sync (static, set once before the draft opens)
    # =========================================================================

    @app_commands.command(
        name="syncdraftnicknames",
        description="(Admin) Tag every owner's nickname with their rookie draft pick slot",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def sync_draft_nicknames(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            tagged = await self._sync_draft_order_nicknames()
            await interaction.followup.send(
                f"✅ Tagged {tagged} nickname(s) with draft pick slot."
            )
        except Exception as e:
            logger.error(f"syncdraftnicknames failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error syncing nicknames: {e}")

    async def _sync_draft_order_nicknames(self) -> int:
        """Tag every owner's nickname with their slot in the calculated rookie
        draft order (e.g. "Pick 3").

        Uses each owner's original slot from calculate_draft_order - same as
        what /draftorder displays - and doesn't reconcile traded picks.
        """
        guild = self._get_guild()
        if not guild:
            logger.warning("No guild available to sync draft nicknames")
            return 0

        from cogs.draft import calculate_draft_order, calculate_payouts

        draft_cog = self.bot.get_cog("DraftCalculator")
        if not draft_cog:
            logger.warning("DraftCalculator cog not loaded, can't compute draft order")
            return 0

        teams = await draft_cog._fetch_team_stats()
        calculate_payouts(teams)
        draft_order = calculate_draft_order(teams)

        tagged = 0
        for slot, team in enumerate(draft_order, start=1):
            league_member = get_member_registry().find_by_sleeper_id(team.owner_id)
            if not league_member or not league_member.discord_id:
                continue

            member = guild.get_member(int(league_member.discord_id))
            if not member:
                continue

            if await apply_tag(member, f"Pick {slot}"):
                tagged += 1

        return tagged

    # =========================================================================
    # Live "on the clock" tracking while an actual Sleeper draft is running
    # =========================================================================

    @tasks.loop(minutes=5)
    async def draft_clock_loop(self):
        try:
            await self._check_draft_clock()
        except Exception as e:
            logger.error(f"Error in draft_clock_loop: {e}", exc_info=True)

    @draft_clock_loop.before_loop
    async def before_draft_clock_loop(self):
        await self.bot.wait_until_ready()

    async def _check_draft_clock(self) -> None:
        guild = self._get_guild()
        if not guild:
            return

        draft = await self._get_active_draft()
        if not draft:
            # Nothing drafting right now - make sure no stale tag lingers
            # from a draft that just finished (or the loop missing a beat).
            stale_id = await find_discord_id_with_tag(CLOCK_TAG)
            if stale_id:
                member = guild.get_member(int(stale_id))
                if member:
                    await clear_tag(member)
            return

        owner_id = await self._resolve_on_the_clock_owner_id(draft)
        if not owner_id:
            return

        league_member = get_member_registry().find_by_sleeper_id(owner_id)
        if not league_member or not league_member.discord_id:
            return

        current_holder_id = await find_discord_id_with_tag(CLOCK_TAG)
        if current_holder_id == league_member.discord_id:
            return  # already tagged, nothing to do

        if current_holder_id:
            prev_member = guild.get_member(int(current_holder_id))
            if prev_member:
                await clear_tag(prev_member)

        member = guild.get_member(int(league_member.discord_id))
        if member:
            await apply_tag(member, CLOCK_TAG)
            logger.info(f"On the clock: {member}")

    async def _get_active_draft(self) -> Optional[dict]:
        drafts = await self.bot.sleeper.get_drafts(self.league_id)
        for draft in drafts:
            if draft.get("status") == "drafting":
                return draft
        return None

    async def _resolve_on_the_clock_owner_id(self, draft: dict) -> Optional[str]:
        """Figure out whose turn it is from Sleeper's own draft slot data.

        NOTE: this hasn't been exercised against a real live Sleeper draft -
        the league's rookie draft is a slow, ~24h-per-pick affair, so there's
        no live draft to test against outside draft season. Deliberately
        fails closed (returns None, so the loop just no-ops) rather than
        guessing if any expected field is missing or unfamiliar.
        """
        draft_id = draft.get("draft_id")
        if not draft_id:
            return None

        picks = await self.bot.sleeper.get_picks_in_draft(draft_id)
        next_pick_no = len(picks) + 1

        slot_to_roster = draft.get("slot_to_roster_id") or {}
        num_slots = len(slot_to_roster)
        if not num_slots:
            return None

        round_num, idx_in_round = divmod(next_pick_no - 1, num_slots)
        if draft.get("type") == "snake" and round_num % 2 == 1:
            slot = num_slots - idx_in_round
        else:
            slot = idx_in_round + 1

        roster_id = slot_to_roster.get(str(slot), slot_to_roster.get(slot))
        if roster_id is None:
            return None

        rosters = await self.bot.sleeper.get_rosters(self.league_id)
        for roster in rosters:
            if roster.get("roster_id") == int(roster_id):
                return roster.get("owner_id")

        return None

    # =========================================================================
    # Cleanup
    # =========================================================================

    @app_commands.command(
        name="clearnicknametags",
        description="(Admin) Remove all bot-applied nickname tags (standings/draft/clock)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def clear_nickname_tags(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = self._get_guild()
        if not guild:
            await interaction.followup.send("❌ No guild available.")
            return

        from database import db

        async with db.connection.execute(
            "SELECT discord_id FROM nickname_tags WHERE tag IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()

        cleared = 0
        for (discord_id,) in rows:
            member = guild.get_member(int(discord_id))
            if member and await clear_tag(member):
                cleared += 1

        await interaction.followup.send(f"✅ Cleared {cleared} nickname tag(s).")


async def setup(bot: "DynastyBot"):
    """Load the Nickname Tags cog."""
    await bot.add_cog(NicknameTags(bot))
