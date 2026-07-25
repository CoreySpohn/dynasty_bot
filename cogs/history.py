"""League history cog: all-time head-to-head records and championship rings.

Both span seasons, which is why everything here attributes by **owner**
rather than roster. Sleeper roster ids are only stable within a single
season's league, so roster_id 4 in 2023 is not necessarily the same person
as roster_id 4 today - aggregating by roster would quietly credit the wrong
owner.

Nothing is stored. Sleeper keeps matchups and playoff brackets for every
season and chains them via `previous_league_id`, so history is derived on
demand. These commands walk every season, so they're the most
API-expensive in the bot - fine for occasional use, not for a loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import SLEEPER_LEAGUE_ID
from lib.members import get_member_registry
from lib.results import (
    TeamRecord,
    get_champions,
    get_history_results,
    head_to_head_owners,
)

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.history")

RING = "🏆"


class History(commands.Cog):
    """All-time records and championship history."""

    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID

    async def cog_load(self) -> None:
        logger.info("History cog loaded")

    # =====================================================================
    # Shared plumbing
    # =====================================================================

    async def _owner_names(self) -> dict[str, str]:
        """Sleeper user id -> display name.

        Prefers the member registry (real names / league nicknames) and
        falls back to the Sleeper display name. Includes owners from prior
        seasons who have since left, so old records still read properly.
        """
        registry = get_member_registry()
        names: dict[str, str] = {}

        for member in [*registry.members, *registry.former_members]:
            if member.sleeper_id:
                names[str(member.sleeper_id)] = member.name

        try:
            for user in await self.bot.sleeper.get_users(self.league_id):
                names.setdefault(
                    user["user_id"], user.get("display_name", "Unknown")
                )
        except Exception as e:
            logger.warning(f"Could not fetch Sleeper users: {e}")

        return names

    def _resolve_owner(self, query: str) -> tuple[Optional[str], Optional[str]]:
        """Resolve a name to (sleeper_id, display name), or (None, error)."""
        registry = get_member_registry()

        member = registry.find(query)
        if member is None:
            matches = registry.find_fuzzy(query)
            if len(matches) == 1:
                member = matches[0]
            elif len(matches) > 1:
                names = ", ".join(m.name for m in matches[:5])
                return None, f'"{query}" could be any of: {names}. Be specific.'
        if member is None:
            return None, f'No league member matches "{query}".'
        if not member.sleeper_id:
            return None, f"{member.name} has no Sleeper ID on file."
        return str(member.sleeper_id), member.name

    # =====================================================================
    # /h2h
    # =====================================================================

    @app_commands.command(
        name="h2h",
        description="All-time head-to-head records across every season",
    )
    @app_commands.describe(
        owner="Whose record to show (defaults to you)",
        against="Optional: show only the record against this owner",
    )
    async def h2h(
        self,
        interaction: discord.Interaction,
        owner: Optional[str] = None,
        against: Optional[str] = None,
    ):
        await interaction.response.defer()
        try:
            names = await self._owner_names()
            registry = get_member_registry()

            if owner:
                owner_id, label = self._resolve_owner(owner)
                if owner_id is None:
                    await interaction.followup.send(f"❌ {label}")
                    return
            else:
                member = registry.find_by_discord_id(interaction.user.id)
                if member is None or not member.sleeper_id:
                    await interaction.followup.send(
                        "❌ I don't know your Sleeper account. Pass `owner` "
                        "explicitly, or get linked in `config/members.yaml`."
                    )
                    return
                owner_id, label = str(member.sleeper_id), member.name

            against_id = None
            if against:
                against_id, against_label = self._resolve_owner(against)
                if against_id is None:
                    await interaction.followup.send(f"❌ {against_label}")
                    return

            players = await self.bot.sleeper.get_all_players()
            results = await get_history_results(
                self.bot.sleeper, self.league_id, players=players, by_owner=True
            )
            pairs = head_to_head_owners(results)

            mine = {
                opponent_id: record
                for (subject_id, opponent_id), record in pairs.items()
                if subject_id == owner_id
            }
            if not mine:
                await interaction.followup.send(
                    f"📭 No completed matchups on record for **{label}**."
                )
                return

            if against_id:
                record = mine.get(against_id)
                if record is None:
                    await interaction.followup.send(
                        f"📭 **{label}** has never played "
                        f"**{names.get(against_id, against)}**."
                    )
                    return
                await interaction.followup.send(
                    embed=self._pair_embed(label, names, against_id, record)
                )
                return

            await interaction.followup.send(
                embed=self._all_opponents_embed(label, names, mine)
            )

        except Exception as e:
            logger.error(f"h2h failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error building head-to-head: {e}")

    def _pair_embed(
        self,
        label: str,
        names: dict[str, str],
        opponent_id: str,
        record: TeamRecord,
    ) -> discord.Embed:
        opponent = names.get(opponent_id, "Unknown")
        embed = discord.Embed(
            title=f"⚔️ {label} vs {opponent}",
            description=f"**{record.record_text}** all-time",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Points",
            value=(
                f"{record.points_for:,.1f} scored\n"
                f"{record.points_against:,.1f} allowed"
            ),
            inline=True,
        )
        if record.games:
            embed.add_field(
                name="Per game",
                value=(
                    f"{record.points_for / record.games:,.1f} scored\n"
                    f"{record.points_against / record.games:,.1f} allowed"
                ),
                inline=True,
            )
        return embed

    def _all_opponents_embed(
        self, label: str, names: dict[str, str], mine: dict[str, TeamRecord]
    ) -> discord.Embed:
        ranked = sorted(
            mine.items(),
            key=lambda item: (item[1].win_pct, item[1].games),
            reverse=True,
        )

        table = "```\n"
        table += f"{'Opponent':<16} {'Record':>9} {'Win%':>6}\n"
        table += "-" * 34 + "\n"
        for opponent_id, record in ranked:
            opponent = names.get(opponent_id, "Unknown")[:15]
            table += (
                f"{opponent:<16} {record.record_text:>9} "
                f"{record.win_pct:>5.0f}%\n"
            )
        table += "```"

        totals = TeamRecord(0)
        for record in mine.values():
            totals.wins += record.wins
            totals.losses += record.losses
            totals.ties += record.ties

        embed = discord.Embed(
            title=f"⚔️ {label} — All-Time Head-to-Head",
            description=f"**{totals.record_text}** overall\n{table}",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Every season Sleeper has on record")
        return embed

    # =====================================================================
    # /rings
    # =====================================================================

    @app_commands.command(
        name="rings",
        description="Championship history — who actually has the hardware",
    )
    async def rings(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            names = await self._owner_names()
            champions = await get_champions(self.bot.sleeper, self.league_id)

            titled = [(season, owner) for season, owner in champions if owner]
            if not titled:
                await interaction.followup.send(
                    "📭 No completed championships on record yet. Sleeper only "
                    "reports a winner once a season's playoff bracket finishes."
                )
                return

            seasons_by_owner: dict[str, list[int]] = {}
            for season, owner_id in titled:
                seasons_by_owner.setdefault(owner_id, []).append(season)

            ranked = sorted(
                seasons_by_owner.items(),
                key=lambda item: (len(item[1]), max(item[1])),
                reverse=True,
            )

            lines = []
            for owner_id, seasons in ranked:
                owner = names.get(owner_id, "Unknown")
                years = ", ".join(str(s) for s in sorted(seasons, reverse=True))
                lines.append(
                    f"{RING * len(seasons)} **{owner}** "
                    f"({len(seasons)}x — {years})"
                )

            embed = discord.Embed(
                title="🏆 Championship Rings",
                description="\n".join(lines),
                color=discord.Color.gold(),
            )

            ringless = [
                names.get(str(m.sleeper_id), m.name)
                for m in get_member_registry().members
                if m.sleeper_id and str(m.sleeper_id) not in seasons_by_owner
            ]
            if ringless:
                embed.add_field(
                    name="💸 Still shopping",
                    value=", ".join(sorted(ringless)),
                    inline=False,
                )

            embed.set_footer(
                text=f"{len(titled)} season(s) on record • Source: Sleeper brackets"
            )
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"rings failed: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error building ring counter: {e}")


async def setup(bot: "DynastyBot") -> None:
    """Load the History cog."""
    await bot.add_cog(History(bot))
